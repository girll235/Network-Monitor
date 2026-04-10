import socket
import threading
import time
from datetime import datetime
import tkinter as tk
from tkinter import ttk, scrolledtext
import queue

# ============================================================
# GLOBALS
# ============================================================
DEFAULT_PORTS = [8888, 9999, 7777]
MAX_REPORTS_PER_MINUTE = 10
TIMEOUT_VAL = 30  # seconds
T = 5             # doit correspondre au T du client
INACTIVITY_LIMIT = 3 * T  # un agent est retiré après 3*T secondes sans REPORT
lock = threading.Lock()   # protection accès concurrent à active_agents

active_agents = {}      # agents currently connected (TCP)
agents_history = {}

running = True
clients = []

# ============================================================
# Shared queues
# ============================================================
udp_active_agents = {}
udp_agents_history = {}
udp_lock = threading.Lock()
clients_lock = threading.Lock()

# Queues for GUI updates
log_queue      = queue.Queue()
tcp_tbl_queue  = queue.Queue()
udp_tbl_queue  = queue.Queue()
summary_queue  = queue.Queue()

# Stats for comparison (accumulate across the session)
tcp_stats = {"requests": 0, "total_rtt": 0.0, "errors": 0}
udp_stats = {"requests": 0, "total_rtt": 0.0, "errors": 0}

_server_sock_ref = [None]
_udp_sock_ref = [None]
_server_stop_lock = threading.Lock()
_server_stopping = [False]


# ============================================================
# HELPERS
# ============================================================

def _log(msg):
    """Print to terminal AND push to GUI queue (thread-safe)."""
    print(msg)
    log_queue.put(msg)


def socket_is_open(sock):
    try:
        return sock is not None and sock.fileno() != -1
    except Exception:
        return False


def safe_socket_close(sock, shutdown_first=True):
    if not socket_is_open(sock):
        return
    if shutdown_first:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
    try:
        sock.close()
    except Exception:
        pass


def add_client_socket(conn):
    with clients_lock:
        if conn not in clients:
            clients.append(conn)


def remove_client_socket(conn):
    with clients_lock:
        if conn in clients:
            clients.remove(conn)


def snapshot_clients():
    with clients_lock:
        return list(clients)


def format_time(ts=None):
    if ts:
        return datetime.fromtimestamp(int(ts)).strftime("%H:%M:%S")
    return datetime.now().strftime("%H:%M:%S")


def validate_data(agent_id, cpu, ram):
    try:
        cpu = float(cpu)
        ram = float(ram)
        if 0 <= cpu <= 100 and ram >= 0 and (' ' not in agent_id):
            return True, cpu, ram
    except ValueError:
        pass
    return False, 0, 0


def make_table_row(agent_name="", hostname="", ip="", port="", command="", response="",
                   timestamp="", rtt="", status="", cpu_usage="", ram_usage=""):
    return {
        "agent": agent_name,
        "hostname": hostname,
        "ip": ip,
        "port": port,
        "command": command,
        "response": response,
        "timestamp": timestamp,
        "rtt": rtt,
        "status": status,
        "cpu_usage": cpu_usage,
        "ram_usage": ram_usage,
    }


def make_summary_row(agent_name="", hostname="", avg_cpu="", avg_ram="", ip="", update_time=""):
    return {
        "agent": agent_name,
        "hostname": hostname,
        "avg_cpu": avg_cpu,
        "avg_ram": avg_ram,
        "ip": ip,
        "update_time": update_time,
    }


def queue_agent_summary(agent_name, hostname, ip, reports, update_time):
    if not reports:
        return
    avg_cpu = sum(r[0] for r in reports) / len(reports)
    avg_ram = sum(r[1] for r in reports) / len(reports)
    summary_queue.put(make_summary_row(
        agent_name=agent_name,
        hostname=hostname,
        avg_cpu=f"{avg_cpu:.2f}%",
        avg_ram=f"{avg_ram:.2f} MB",
        ip=ip,
        update_time=format_time(update_time),
    ))


def get_agent_summary_rows():
    rows = {}

    def _consume(history_dict):
        for key, info in history_dict.items():
            agent = info.get("id", key)
            hostname = info.get("hostname", "")
            ip = info.get("ip", "")
            reports = info.get("reports", [])
            avg_cpu = sum(r[0] for r in reports) / len(reports) if reports else 0.0
            avg_ram = sum(r[1] for r in reports) / len(reports) if reports else 0.0
            row_key = (agent, hostname, ip)
            rows[row_key] = {
                "agent": agent,
                "hostname": hostname,
                "avg_cpu": f"{avg_cpu:.2f}%",
                "avg_ram": f"{avg_ram:.2f} MB",
                "ip": ip,
                "count": len(reports),
            }

    _consume(agents_history)
    _consume(udp_agents_history)

    return sorted(rows.values(), key=lambda x: (x["agent"], x["hostname"], x["ip"]))


def _remove_tcp_agent(ip):
    with lock:
        active_agents.pop(ip, None)
        _log(f"agent {ip} deleted.")


def _remove_udp_agent(ip):
    with udp_lock:
        udp_active_agents.pop(ip, None)
        _log(f"agent {ip} deleted.")


# ============================================================
# ORIGINAL FUNCTIONS
# ============================================================

def handle_client(conn, addr):
    global running

    _log(f"[NEW CONNECTION] {addr} connected.")
    try:
        conn.settimeout(TIMEOUT_VAL)
    except Exception:
        pass

    agent_id = None
    hostname = ""
    last_report_time = 0
    disconnect_logged = False

    try:
        while running:
            try:
                data = conn.recv(1024)
            except socket.timeout:
                raise
            except ConnectionResetError:
                if running and not _server_stopping[0]:
                    _log(f"[{format_time()}] [DISCONNECTED] IP: {addr[0]} {agent_id or ''} disconnected.")
                disconnect_logged = True
                break
            except OSError:
                if running and not _server_stopping[0]:
                    _log(f"[{format_time()}] [DISCONNECTED] IP: {addr[0]} {agent_id or ''} connection closed.")
                disconnect_logged = True
                break

            if not data:
                if running and not _server_stopping[0]:
                    _log(f"[{format_time()}] [DISCONNECTED] IP: {addr[0]} {agent_id or ''} disconnected normally.")
                disconnect_logged = True
                break

            recv_time = time.time()
            data = data.decode('utf-8', errors='ignore').strip()
            parts = data.split()
            if not parts:
                continue
            command = parts[0].upper()

            # ── HELLO ──
            if command == "HELLO" and len(parts) >= 3:
                _log(f"[{addr[0]},{addr[1]}] Received HELLO from {parts[1]} {parts[2]}")
                agent_id = parts[1]
                hostname = parts[2]
                time_seen = time.time()
                with lock:
                    active_agents[addr[0]] = {
                        "id": agent_id,
                        "hostname": hostname,
                        "last_seen": time_seen,
                        "cpu": 0,
                        "ram": 0,
                        "port": addr[1],
                        "addr": addr,
                    }
                add_client_socket(conn)

                if agent_id not in agents_history:
                    agents_history[agent_id] = {
                        "id": agent_id,
                        "hostname": hostname,
                        "ip": addr[0],
                        "reports": []
                    }
                else:
                    agents_history[agent_id]["hostname"] = hostname
                    agents_history[agent_id]["ip"] = addr[0]

                _log(f"[{format_time(time_seen)}] [CONNECTED] {addr[0]} {agent_id} {hostname}")
                _log(f"[INFO] Active agents: {len(active_agents)}")
                try:
                    conn.sendall(b"OK\n")
                except Exception:
                    pass

                rtt = (time.time() - recv_time) * 1000
                tcp_stats["requests"] += 1
                tcp_stats["total_rtt"] += rtt
                tcp_tbl_queue.put(make_table_row(
                    agent_name=agent_id,
                    hostname=hostname,
                    ip=addr[0],
                    port=addr[1],
                    command="HELLO",
                    response="OK",
                    timestamp=format_time(time_seen),
                    rtt=f"{rtt:.2f}ms",
                    status="Connected"
                ))

            # ── REPORT ──
            elif command == "REPORT" and len(parts) >= 5:
                _log(f"[{addr[0]},{addr[1]}] Received REPORT from {parts[1]} "
                     f"{format_time(parts[2])} {parts[3]}, {parts[4]}")

                current_time = time.time()
                if current_time - last_report_time < (60 / MAX_REPORTS_PER_MINUTE):
                    try:
                        conn.sendall(b"ERROR: Too many requests\n")
                        remove_client_socket(conn)
                    except Exception:
                        pass
                    rtt = (time.time() - recv_time) * 1000
                    tcp_stats["errors"] += 1
                    tcp_tbl_queue.put(make_table_row(
                        agent_name=agent_id or parts[1],
                        hostname=hostname,
                        ip=addr[0],
                        port=addr[1],
                        command="REPORT",
                        response="Rate Limited",
                        timestamp=format_time(),
                        rtt=f"{rtt:.2f}ms",
                        status="Blocked"
                    ))
                    continue

                is_valid, cpu, ram = validate_data(parts[1], parts[3], parts[4])
                if is_valid:
                    if not agent_id:
                        agent_id = parts[1]
                    last_report_time = current_time
                    with lock:
                        if addr[0] in active_agents:
                            active_agents[addr[0]]["last_seen"] = current_time
                            active_agents[addr[0]]["cpu"] = cpu
                            active_agents[addr[0]]["ram"] = ram
                            active_agents[addr[0]]["port"] = addr[1]
                            hostname = active_agents[addr[0]].get("hostname", hostname)

                    if agent_id not in agents_history:
                        agents_history[agent_id] = {
                            "id": agent_id,
                            "hostname": hostname,
                            "ip": addr[0],
                            "reports": []
                        }
                    agents_history[agent_id]["hostname"] = hostname
                    agents_history[agent_id]["ip"] = addr[0]
                    agents_history[agent_id]["reports"].append((cpu, ram, current_time))

                    _log(f"[{format_time(current_time)}] [REPORT] IP: {addr[0]} | "
                         f"{agent_id} | CPU: {cpu:.2f}% | RAM: {ram:.2f} MB")
                    try:
                        conn.sendall(b"OK\n")
                    except Exception:
                        pass

                    rtt = (time.time() - recv_time) * 1000
                    tcp_stats["requests"] += 1
                    tcp_stats["total_rtt"] += rtt
                    tcp_tbl_queue.put(make_table_row(
                        agent_name=agent_id,
                        hostname=hostname,
                        ip=addr[0],
                        port=addr[1],
                        command="REPORT",
                        response=f"CPU {cpu:.2f}% | RAM {ram:.2f} MB | OK",
                        timestamp=format_time(current_time),
                        rtt=f"{rtt:.2f}ms",
                        status="Active",
                        cpu_usage=f"{cpu:.2f}%",
                        ram_usage=f"{ram:.2f} MB"
                    ))
                    queue_agent_summary(agent_id, hostname, addr[0], agents_history[agent_id]["reports"], current_time)
                else:
                    try:
                        conn.sendall(b"ERROR: Invalid data or Unregistered\n")
                    except Exception:
                        pass
                    rtt = (time.time() - recv_time) * 1000
                    tcp_stats["errors"] += 1
                    tcp_tbl_queue.put(make_table_row(
                        agent_name=agent_id or parts[1],
                        hostname=hostname,
                        ip=addr[0],
                        port=addr[1],
                        command="REPORT",
                        response="ERROR",
                        timestamp=format_time(),
                        rtt=f"{rtt:.2f}ms",
                        status="Invalid"
                    ))

            # ── BYE ──
            elif command == "BYE":
                bye_name = parts[1] if len(parts) > 1 else (agent_id or addr[0])
                _log(f"[BYE] {bye_name} disconnected gracefully.")
                _remove_tcp_agent(addr[0])
                _log(f"[INFO] Active agents: {len(active_agents)}")
                tcp_tbl_queue.put(make_table_row(
                    agent_name=bye_name,
                    hostname=hostname,
                    ip=addr[0],
                    port=addr[1],
                    command="BYE",
                    response="Connection closed",
                    timestamp=format_time(),
                    rtt="-",
                    status="Disconnected"
                ))
                disconnect_logged = True
                break

            # ── STOP ──
            else:
                try:
                    conn.sendall(b"ERROR: Unknown command\n")
                except Exception:
                    pass

    except socket.timeout:
        _log(f"[{format_time()}] [TIMEOUT] Agent {agent_id} stopped responding.")
    except Exception as e:
        if running and not _server_stopping[0]:
            _log(f"[ERROR] {e}")
    finally:
        _remove_tcp_agent(addr[0])
        remove_client_socket(conn)
        safe_socket_close(conn)
        if disconnect_logged:
            _log(f"[INFO] Active agents: {len(active_agents)}")


def stop_server(server_socket=None):
    global running
    with _server_stop_lock:
        if _server_stopping[0]:
            return
        _server_stopping[0] = True
        running = False

    _log("[SERVER] STOPPING...")

    # Notify TCP clients first
    for c in snapshot_clients():
        if socket_is_open(c):
            try:
                c.sendall(b"STOP\n")
            except Exception:
                pass

    # Notify UDP clients if any are active
    udp_sock = _udp_sock_ref[0]
    if socket_is_open(udp_sock):
        with udp_lock:
            udp_targets = [info.get("addr") for info in udp_active_agents.values() if info.get("addr")]
        for target in udp_targets:
            try:
                udp_sock.sendto(b"STOP\n", target)
            except Exception:
                pass

    time.sleep(0.4)

    for c in snapshot_clients():
        safe_socket_close(c)
        remove_client_socket(c)

    safe_socket_close(server_socket or _server_sock_ref[0])
    safe_socket_close(_udp_sock_ref[0], shutdown_first=False)

    _server_sock_ref[0] = None
    _udp_sock_ref[0] = None
    _log("[SERVER] All sockets closed cleanly.")


def command_listener(server_socket):
    """Original terminal command listener """
    while True:
        try:
            cmd = input().strip().lower()
            if cmd == "stop":
                stop_server(server_socket)
                break
        except Exception:
            break


def check_inactive_agents():
    """Retire les agents qui n'ont pas envoyé de REPORT depuis 3*T secondes."""
    while running:
        time.sleep(T)
        now = time.time()
        with lock:
            inactive = [
                aid for aid, info in active_agents.items()
                if now - info["last_seen"] > INACTIVITY_LIMIT
            ]
            for aid in inactive:
                del active_agents[aid]
                _log(f"[INACTIVE] Agent {aid} retiré (inactif depuis {INACTIVITY_LIMIT}s).")

        with udp_lock:
            udp_inactive = [
                aid for aid, info in udp_active_agents.items()
                if now - info["last_seen"] > INACTIVITY_LIMIT
            ]
            for aid in udp_inactive:
                del udp_active_agents[aid]
                _log(f"[UDP] [INACTIVE] Agent {aid} retiré (inactif depuis {INACTIVITY_LIMIT}s).")


def stats_loop():
    while running:
        time.sleep(10)
        if not active_agents and not udp_active_agents:
            continue

        if active_agents:
            total_cpu = sum(a["cpu"] for a in active_agents.values())
            total_ram = sum(a["ram"] for a in active_agents.values())
            count = len(active_agents)
            _log(f"[STATS] TCP Active Agents: {count} | "
                 f"Avg CPU: {total_cpu / count:.2f}% | Avg RAM: {total_ram / count:.2f}MB")

        if udp_active_agents:
            total_cpu = sum(a["cpu"] for a in udp_active_agents.values())
            total_ram = sum(a["ram"] for a in udp_active_agents.values())
            count = len(udp_active_agents)
            _log(f"[UDP STATS] UDP Active Agents: {count} | "
                 f"Avg CPU: {total_cpu / count:.2f}% | Avg RAM: {total_ram / count:.2f}MB")


# ============================================================
# UDP Handler
# ============================================================

def handle_udp(udp_sock):
    """Receive and process UDP datagrams – mirrors the TCP protocol."""
    global running
    udp_last_report = {}

    while running:
        try:
            data, addr = udp_sock.recvfrom(1024)
        except socket.timeout:
            continue
        except OSError:
            if running and not _server_stopping[0]:
                _log("[UDP] Connection closed.")
            break
        except Exception as e:
            if running and not _server_stopping[0]:
                _log(f"[UDP ERROR] {e}")
            break

        recv_time = time.time()
        if not data:
            continue

        message = data.decode('utf-8', errors='ignore').strip()
        parts = message.split()
        if not parts:
            continue

        command = parts[0].upper()
        _log(f"[UDP] [{addr[0]}:{addr[1]}] Received: {message}")

        # ── HELLO ──
        if command == "HELLO" and len(parts) >= 3:
            agent_id = parts[1]
            hostname = parts[2]
            time_seen = time.time()
            with udp_lock:
                udp_active_agents[addr[0]] = {
                    "id": agent_id,
                    "hostname": hostname,
                    "last_seen": time_seen,
                    "cpu": 0,
                    "ram": 0,
                    "port": addr[1],
                    "addr": addr,
                }
            if agent_id not in udp_agents_history:
                udp_agents_history[agent_id] = {
                    "id": agent_id,
                    "hostname": hostname,
                    "ip": addr[0],
                    "reports": []
                }
            else:
                udp_agents_history[agent_id]["hostname"] = hostname
                udp_agents_history[agent_id]["ip"] = addr[0]

            _log(f"[UDP] [{format_time(time_seen)}] [CONNECTED] {addr[0]} {agent_id} {hostname}")
            try:
                udp_sock.sendto(b"OK\n", addr)
            except Exception:
                pass

            rtt = (time.time() - recv_time) * 1000
            udp_stats["requests"] += 1
            udp_stats["total_rtt"] += rtt
            udp_tbl_queue.put(make_table_row(
                agent_name=agent_id,
                hostname=hostname,
                ip=addr[0],
                port=addr[1],
                command="HELLO",
                response="OK",
                timestamp=format_time(time_seen),
                rtt=f"{rtt:.2f}ms",
                status="Connected"
            ))

        # ── REPORT ──
        elif command == "REPORT" and len(parts) >= 5:
            current_time = time.time()
            last_t = udp_last_report.get(addr[0], 0)
            agent_id = parts[1]
            hostname = udp_active_agents.get(addr[0], {}).get("hostname", "")

            if current_time - last_t < (60 / MAX_REPORTS_PER_MINUTE):
                try:
                    udp_sock.sendto(b"ERROR: Too many requests\n", addr)
                    _remove_udp_agent(addr[0])
                except Exception:
                    pass
                udp_stats["errors"] += 1
                udp_tbl_queue.put(make_table_row(
                    agent_name=agent_id,
                    hostname=hostname,
                    ip=addr[0],
                    port=addr[1],
                    command="REPORT",
                    response="Rate Limited",
                    timestamp=format_time(),
                    rtt="-",
                    status="Blocked"
                ))
                continue

            is_valid, cpu, ram = validate_data(parts[1], parts[3], parts[4])
            if is_valid:
                udp_last_report[addr[0]] = current_time
                with udp_lock:
                    if addr[0] in udp_active_agents:
                        udp_active_agents[addr[0]]["last_seen"] = current_time
                        udp_active_agents[addr[0]]["cpu"] = cpu
                        udp_active_agents[addr[0]]["ram"] = ram
                        udp_active_agents[addr[0]]["port"] = addr[1]
                        hostname = udp_active_agents[addr[0]].get("hostname", hostname)
                    else:
                        udp_active_agents[addr[0]] = {
                            "id": agent_id,
                            "hostname": hostname,
                            "last_seen": current_time,
                            "cpu": cpu,
                            "ram": ram,
                            "port": addr[1],
                            "addr": addr,
                        }

                if agent_id not in udp_agents_history:
                    udp_agents_history[agent_id] = {
                        "id": agent_id,
                        "hostname": hostname,
                        "ip": addr[0],
                        "reports": []
                    }
                udp_agents_history[agent_id]["hostname"] = hostname
                udp_agents_history[agent_id]["ip"] = addr[0]
                udp_agents_history[agent_id]["reports"].append((cpu, ram, current_time))

                _log(f"[UDP] [{format_time(current_time)}] [REPORT] IP: {addr[0]} | "
                     f"{agent_id} | CPU: {cpu:.2f}% | RAM: {ram:.2f} MB")
                try:
                    udp_sock.sendto(b"OK\n", addr)
                except Exception:
                    pass

                rtt = (time.time() - recv_time) * 1000
                udp_stats["requests"] += 1
                udp_stats["total_rtt"] += rtt
                udp_tbl_queue.put(make_table_row(
                    agent_name=agent_id,
                    hostname=hostname,
                    ip=addr[0],
                    port=addr[1],
                    command="REPORT",
                    response=f"CPU {cpu:.2f}% | RAM {ram:.2f} MB | OK",
                    timestamp=format_time(current_time),
                    rtt=f"{rtt:.2f}ms",
                    status="Active",
                    cpu_usage=f"{cpu:.2f}%",
                    ram_usage=f"{ram:.2f} MB"
                ))
                queue_agent_summary(agent_id, hostname, addr[0], udp_agents_history[agent_id]["reports"], current_time)
            else:
                try:
                    udp_sock.sendto(b"ERROR: Invalid data or Unregistered\n", addr)
                except Exception:
                    pass
                udp_stats["errors"] += 1
                udp_tbl_queue.put(make_table_row(
                    agent_name=agent_id,
                    hostname=hostname,
                    ip=addr[0],
                    port=addr[1],
                    command="REPORT",
                    response="ERROR",
                    timestamp=format_time(),
                    rtt="-",
                    status="Invalid"
                ))

        # ── BYE ──
        elif command == "BYE":
            aid = parts[1] if len(parts) > 1 else addr[0]
            hostname = udp_active_agents.get(addr[0], {}).get("hostname", "")
            _log(f"[UDP] [BYE] {aid} disconnected normally.")
            _remove_udp_agent(addr[0])
            udp_tbl_queue.put(make_table_row(
                agent_name=aid,
                hostname=hostname,
                ip=addr[0],
                port=addr[1],
                command="BYE",
                response="Connection closed",
                timestamp=format_time(),
                rtt="-",
                status="Disconnected"
            ))
        else:
            try:
                udp_sock.sendto(b"ERROR: Unknown command\n", addr)
            except Exception:
                pass


# ============================================================
# start_server
# ============================================================

def start_server():
    global running

    with _server_stop_lock:
        _server_stopping[0] = False
    running = True

    # ── TCP socket (original logic preserved) ──
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _server_sock_ref[0] = s

    bound_port = None
    for port in DEFAULT_PORTS:
        try:
            s.bind(('0.0.0.0', port))
            _log(f"[OK] TCP Server listening on port {port}")
            bound_port = port
            break
        except Exception:
            _log(f"[BUSY] Port {port} unavailable")
    else:
        _log("[CRITICAL] No available port")
        safe_socket_close(s)
        _server_sock_ref[0] = None
        return

    # ── UDP socket ──
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _udp_sock_ref[0] = udp_sock
    try:
        udp_sock.bind(('0.0.0.0', bound_port))
        _log(f"[OK] UDP Server listening on port {bound_port}")
    except Exception as e:
        _log(f"[WARNING] UDP bind failed on port {bound_port}: {e}")

    s.listen(10)
    s.settimeout(1)
    udp_sock.settimeout(1)

    threading.Thread(target=stats_loop, daemon=True).start()
    threading.Thread(target=check_inactive_agents, daemon=True).start()
    threading.Thread(target=handle_udp, args=(udp_sock,), daemon=True).start()
    threading.Thread(target=command_listener, args=(s,), daemon=True).start()

    while running:
        try:
            conn, addr = s.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
        except socket.timeout:
            continue
        except OSError:
            if running and not _server_stopping[0]:
                _log("[SERVER] TCP listener closed.")
            break
        except Exception as e:
            if running and not _server_stopping[0]:
                _log(f"[CRITICAL] {e}")
            break

    safe_socket_close(udp_sock, shutdown_first=False)


# ============================================================
# GUI
# ============================================================

class ServerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Network Monitor Server  –  TCP & UDP")
        self.root.geometry("1300x820")
        self.root.configure(bg="#1e1e2e")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_scrollable_root()

        self.connection_columns = [
            ("agent", "Agent name", 130, 2, tk.CENTER),
            ("hostname", "Hostname", 140, 2, tk.CENTER),
            ("ip", "IP address", 120, 2, tk.CENTER),
            ("port", "Port", 70, 1, tk.CENTER),
            ("command", "Command", 90, 1, tk.CENTER),
            ("response", "Response", 180, 3, tk.CENTER),
            ("cpu_usage", "CPU usage", 100, 1, tk.CENTER),
            ("ram_usage", "RAM usage", 110, 1, tk.CENTER),
            ("timestamp", "Times", 90, 1, tk.CENTER),
            ("rtt", "RTT", 80, 1, tk.CENTER),
            ("status", "Status", 100, 1, tk.CENTER),
        ]
        self.summary_columns = [
            ("agent", "Agent name", 130, 2, tk.CENTER),
            ("hostname", "Hostname", 140, 2, tk.CENTER),
            ("avg_cpu", "Average CPU usage", 140, 2, tk.CENTER),
            ("avg_ram", "Average RAM usage", 150, 2, tk.CENTER),
            ("ip", "IP address", 120, 2, tk.CENTER),
            ("update_time", "Time of update", 120, 2, tk.CENTER),
        ]

        self._apply_styles()
        self._build_ui()
        self._poll_queues()
        self._refresh_comparison()

    def _build_scrollable_root(self):
        shell = tk.Frame(self.root, bg="#1e1e2e")
        shell.pack(fill=tk.BOTH, expand=True)

        self.page_canvas = tk.Canvas(shell, bg="#1e1e2e", highlightthickness=0)
        self.page_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.page_scrollbar = ttk.Scrollbar(shell, orient=tk.VERTICAL, command=self.page_canvas.yview)
        self.page_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.page_canvas.configure(yscrollcommand=self.page_scrollbar.set)

        self.page = tk.Frame(self.page_canvas, bg="#1e1e2e")
        self.page_window = self.page_canvas.create_window((0, 0), window=self.page, anchor="nw")
        self.page.bind("<Configure>", lambda e: self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all")))
        self.page_canvas.bind("<Configure>", self._on_canvas_configure)

    def _on_canvas_configure(self, event):
        self.page_canvas.itemconfigure(self.page_window, width=event.width)

    # ── Styling ─────────────────────────────────────────────
    def _apply_styles(self):
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("Treeview",
                     background="#313244", foreground="#cdd6f4",
                     fieldbackground="#313244", font=("Consolas", 9),
                     rowheight=22)
        st.configure("Treeview.Heading",
                     background="#45475a", foreground="#cdd6f4",
                     font=("Consolas", 9, "bold"))
        st.map("Treeview", background=[("selected", "#585b70")])
        st.configure("TScrollbar", background="#313244", troughcolor="#1e1e2e",
                     arrowcolor="#cdd6f4")

    # ── UI layout ───────────────────────────────────────────
    def _build_ui(self):
        # ── TOP BAR ──────────────────────────────────────────
        top = tk.Frame(self.page, bg="#181825", pady=8, padx=12)
        top.pack(fill=tk.X)

        tk.Label(top, text="Network Monitor Server",
                 bg="#181825", fg="#89b4fa",
                 font=("Consolas", 14, "bold")).pack(side=tk.LEFT)

        self.status_lbl = tk.Label(top, text="● STOPPED",
                                   bg="#181825", fg="#f38ba8",
                                   font=("Consolas", 11, "bold"))
        self.status_lbl.pack(side=tk.LEFT, padx=18)

        self.stop_btn = tk.Button(
            top, text="⏹  Stop Server",
            bg="#f38ba8", fg="#00001c", font=("Consolas", 10, "bold"),
            padx=10, pady=4, relief=tk.FLAT, bd=0,
            activebackground="#eba0ac", state=tk.DISABLED,
            command=self._stop_server)
        self.stop_btn.pack(side=tk.RIGHT, padx=5)

        self.start_btn = tk.Button(
            top, text="▶  Start Server",
            bg="#099b15", fg="#00001c", font=("Consolas", 10, "bold"),
            padx=10, pady=4, relief=tk.FLAT, bd=0,
            activebackground="#94e2d5",
            command=self._start_server)
        self.start_btn.pack(side=tk.RIGHT, padx=5)

        # ── MAIN VERTICAL PANE ───────────────────────────────
        vp = tk.PanedWindow(self.page, orient=tk.VERTICAL,
                            bg="#1e1e2e", sashwidth=6,
                            sashrelief=tk.FLAT, sashpad=2)
        vp.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        # ── LOG BOX ──────────────────────────────────────────
        log_frm = tk.Frame(vp, bg="#1e1e2e")
        vp.add(log_frm, minsize=300)

        tk.Label(log_frm, text="🟣 Server Log",
                 bg="#1e1e2e", fg="#89b4fa",
                 font=("Consolas", 11, "bold")).pack(anchor=tk.W, padx=2)

        self.log_box = scrolledtext.ScrolledText(
            log_frm, bg="#11111b", fg="#cdd6f4",
            font=("Consolas", 9), relief=tk.FLAT,
            insertbackground="#cdd6f4")
        self.log_box.pack(fill=tk.BOTH, expand=True)
        self.log_box.configure(state=tk.DISABLED)
        self.log_box.tag_config("udp", foreground="#cba6f7")
        self.log_box.tag_config("tcp", foreground="#89dceb")
        self.log_box.tag_config("warn", foreground="#fab387")

        # ── CONNECTION TABLES ROW ────────────────────────────
        tables_row = tk.Frame(vp, bg="#1e1e2e")
        vp.add(tables_row, minsize=220)

        tables_row1 = tk.Frame(vp, bg="#1e1e2e")
        vp.add(tables_row1, minsize=160)

        tcp_frm = tk.Frame(tables_row, bg="#1e1e2e")
        tcp_frm.pack(fill=tk.BOTH, expand=True, pady=(0, 2))
        tk.Label(tcp_frm, text="🔵 TCP Connections",
                 bg="#1e1e2e", fg="#89b4fa",
                 font=("Consolas", 11, "bold")).pack(anchor=tk.W, padx=2)
        self.tcp_tree = self._make_table(tcp_frm, self.connection_columns)

        udp_frm = tk.Frame(tables_row1, bg="#1e1e2e")
        udp_frm.pack(fill=tk.BOTH, expand=True)
        tk.Label(udp_frm, text="🟣 UDP Connections",
                 bg="#1e1e2e", fg="#cba6f7",
                 font=("Consolas", 11, "bold")).pack(anchor=tk.W, padx=2)
        self.udp_tree = self._make_table(udp_frm, self.connection_columns)

        # ── LOWER ROW: COMPARISON + NEW AGENT TABLE ──────────
        lower_row = tk.Frame(vp, bg="#1e1e2e")
        vp.add(lower_row, minsize=220)

        hp_lower = tk.PanedWindow(lower_row, orient=tk.HORIZONTAL,
                                  bg="#1e1e2e", sashwidth=6,
                                  sashrelief=tk.FLAT, sashpad=2)
        hp_lower.pack(fill=tk.BOTH, expand=True)

        cmp_frm = tk.Frame(hp_lower, bg="#1e1e2e")
        hp_lower.add(cmp_frm, minsize=320)
        tk.Label(cmp_frm, text="🟣 TCP vs UDP Comparison",
                 bg="#1e1e2e", fg="#fab387",
                 font=("Consolas", 11, "bold")).pack(anchor=tk.W, padx=2)
        self.cmp_box = scrolledtext.ScrolledText(
            cmp_frm, bg="#11111b", fg="#cdd6f4",
            font=("Consolas", 9), relief=tk.FLAT)
        self.cmp_box.pack(fill=tk.BOTH, expand=True)
        self.cmp_box.configure(state=tk.DISABLED)

        agent_frm = tk.Frame(hp_lower, bg="#1e1e2e")
        hp_lower.add(agent_frm, minsize=520)
        tk.Label(agent_frm, text="🟣 Agent Resource Summary",
                 bg="#1e1e2e", fg="#94e2d5",
                 font=("Consolas", 11, "bold")).pack(anchor=tk.W, padx=2)
        self.agent_tree = self._make_table(agent_frm, self.summary_columns)

    def _make_table(self, parent, columns):
        wrapper = tk.Frame(parent, bg="#1e1e2e")
        wrapper.pack(fill=tk.BOTH, expand=True)
        wrapper.grid_rowconfigure(0, weight=1)
        wrapper.grid_columnconfigure(0, weight=1)

        col_ids = [c[0] for c in columns]
        tree = ttk.Treeview(wrapper, columns=col_ids, show="headings")
        for cid, title, width, _weight, anchor in columns:
            minw = max(55, int(width * 0.45))
            tree.heading(cid, text=title)
            tree.column(cid, width=width, minwidth=minw, anchor=anchor, stretch=True)

        ysb = ttk.Scrollbar(wrapper, orient=tk.VERTICAL, command=tree.yview)
        xsb = ttk.Scrollbar(wrapper, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)

        tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")

        tree._column_specs = columns
        tree.bind("<Configure>", lambda e, t=tree: self._fit_columns(t))
        wrapper.bind("<Configure>", lambda e, t=tree: self._fit_columns(t))
        return tree

    def _fit_columns(self, tree):
        columns = getattr(tree, "_column_specs", None)
        if not columns:
            return
        base_width = max(tree.winfo_width() - 20, 200)
        min_total = sum(max(55, int(c[2] * 0.45)) for c in columns)
        target_width = max(base_width, min_total)
        total_nominal = sum(c[2] for c in columns)
        scale = target_width / total_nominal if total_nominal else 1.0
        for cid, _title, width, _weight, anchor in columns:
            minw = max(55, int(width * 0.45))
            calc = max(minw, int(width * scale))
            tree.column(cid, width=calc, minwidth=minw, anchor=anchor, stretch=True)

    # ── Log helper ──────────────────────────────────────────
    def _gui_log(self, msg):
        tag = None
        if "[UDP]" in msg:
            tag = "udp"
        elif "[NEW CONNECTION]" in msg or "[CONNECTED]" in msg or "[REPORT]" in msg:
            tag = "tcp"
        elif "[WARN" in msg or "[INACTIVE]" in msg or "[TIMEOUT]" in msg:
            tag = "warn"

        self.log_box.configure(state=tk.NORMAL)
        if tag:
            self.log_box.insert(tk.END, msg + "\n", tag)
        else:
            self.log_box.insert(tk.END, msg + "\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)

    # ── Comparison refresh ──────────────────────────────────
    def _refresh_comparison(self):
        t_req = tcp_stats["requests"]
        u_req = udp_stats["requests"]
        t_avg = (tcp_stats["total_rtt"] / t_req) if t_req > 0 else 0.0
        u_avg = (udp_stats["total_rtt"] / u_req) if u_req > 0 else 0.0
        t_err = tcp_stats["errors"]
        u_err = udp_stats["errors"]
        t_rel = (t_req / (t_req + t_err) * 100) if (t_req + t_err) > 0 else 100.0
        u_rel = (u_req / (u_req + u_err) * 100) if (u_req + u_err) > 0 else 100.0

        sep = "─" * 34
        lines = [
            sep,
            f"  {'Metric':<17} {'TCP':>7}  {'UDP':>7}",
            sep,
            f"  {'Requests':<17} {t_req:>7}  {u_req:>7}",
            f"  {'Avg RTT (ms)':<17} {t_avg:>7.2f}  {u_avg:>7.2f}",
            f"  {'Errors':<17} {t_err:>7}  {u_err:>7}",
            f"  {'Reliability %':<17} {t_rel:>6.1f}%  {u_rel:>6.1f}%",
            sep,
            f"  TCP Agents  : {len(active_agents)}",
            f"  UDP Agents  : {len(udp_active_agents)}",
            sep,
        ]

        if t_req > 0 and u_req > 0:
            if t_avg < u_avg:
                lines.append(f"  ✔ TCP faster by {u_avg - t_avg:.2f} ms")
            elif u_avg < t_avg:
                lines.append(f"  ✔ UDP faster by {t_avg - u_avg:.2f} ms")
            else:
                lines.append("  ≈ Similar RTT for both")
        else:
            lines.append("  (Waiting for data...)")

        lines += [
            "",
            "  ℹ TCP: connection-oriented,",
            "    reliable, ordered",
            "  ℹ UDP: connectionless,",
            "    low-overhead, fast",
        ]

        txt = "\n".join(lines)
        self.cmp_box.configure(state=tk.NORMAL)
        self.cmp_box.delete("1.0", tk.END)
        self.cmp_box.insert("1.0", txt)
        self.cmp_box.configure(state=tk.DISABLED)

        self.root.after(3000, self._refresh_comparison)

    def _insert_summary_row(self, row):
        self.agent_tree.insert("", tk.END, values=(
            row.get("agent", ""), row.get("hostname", ""), row.get("avg_cpu", ""),
            row.get("avg_ram", ""), row.get("ip", ""), row.get("update_time", "")
        ))
        self.agent_tree.yview_moveto(1)

    # ── Queue polling (100 ms) ──────────────────────────────
    def _poll_queues(self):
        try:
            while True:
                self._gui_log(log_queue.get_nowait())
        except queue.Empty:
            pass

        try:
            while True:
                self._insert_row(self.tcp_tree, tcp_tbl_queue.get_nowait())
        except queue.Empty:
            pass

        try:
            while True:
                self._insert_row(self.udp_tree, udp_tbl_queue.get_nowait())
        except queue.Empty:
            pass

        try:
            while True:
                self._insert_summary_row(summary_queue.get_nowait())
        except queue.Empty:
            pass

        self.root.after(100, self._poll_queues)

    def _insert_row(self, tree, row):
        tree.insert("", tk.END, values=(
            row.get("agent", ""), row.get("hostname", ""), row.get("ip", ""),
            row.get("port", ""), row.get("command", ""), row.get("response", ""),
            row.get("cpu_usage", ""), row.get("ram_usage", ""), row.get("timestamp", ""),
            row.get("rtt", ""), row.get("status", "")
        ))
        tree.yview_moveto(1)

    # ── Server control ──────────────────────────────────────
    def _start_server(self):
        global running
        running = True
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.status_lbl.configure(text="● RUNNING", fg="#a6e3a1")
        threading.Thread(target=start_server, daemon=True).start()
        self._gui_log("[GUI] Server started.")

    def _stop_server(self):
        global running
        running = False
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.status_lbl.configure(text="● STOPPED", fg="#f38ba8")
        stop_server(_server_sock_ref[0])
        self._gui_log("[GUI] Server stopped.")

    def _on_close(self):
        if self.stop_btn.cget("state") == tk.NORMAL:
            self._stop_server()
        self.root.destroy()


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    root = tk.Tk()
    app = ServerGUI(root)
    root.mainloop()

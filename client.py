import socket
import time
import psutil
import threading
import signal
import sys
import tkinter as tk
from tkinter import ttk, scrolledtext
import queue

# ============================================================
# Global variables
# ============================================================
SERVER_IP   = "192.168.226.1"  # غيّرها للـ IP الصحيح
SERVER_PORT = 8888
AGENT_ID    = sys.argv[1] if len(sys.argv) > 1 else "agent_win11"
T = 5  # report interval in seconds
running = True

client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

# ============================================================
# Shared queues & metrics (GUI comms)
# ============================================================
log_queue         = queue.Queue()   # add message to the log queue
tcp_result_queue  = queue.Queue()   # detailed table rows
udp_result_queue  = queue.Queue()
control_queue     = queue.Queue()

tcp_metrics = {"requests": 0, "total_rtt": 0.0, "failures": 0, "rtts": []}
udp_metrics = {"requests": 0, "total_rtt": 0.0, "failures": 0, "rtts": []}

shared_net_state = {
    "lock": threading.Lock(),
    "tcp": {"sock": None, "bye_sent": False, "expected_close": False, "server_stop": False},
    "udp": {"sock": None, "bye_sent": False, "expected_close": False, "server_stop": False},
}


# ============================================================
# HELPERS
# ============================================================

def _log(msg, tag=None):
    """Print to terminal AND push to GUI log queue."""
    print(msg)
    log_queue.put((msg, tag))


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


def local_hostname():
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


def make_result_row(agent, hostname, ip, port, command, response, timestamp, rtt, status):
    return {
        "agent": agent,
        "hostname": hostname,
        "ip": ip,
        "port": port,
        "command": command,
        "response": response,
        "timestamp": timestamp,
        "rtt": rtt,
        "status": status,
    }


def register_socket(proto, sock):
    with shared_net_state["lock"]:
        shared_net_state[proto]["sock"] = sock
        shared_net_state[proto]["bye_sent"] = False
        shared_net_state[proto]["expected_close"] = False
        shared_net_state[proto]["server_stop"] = False


def clear_socket(proto, sock=None):
    with shared_net_state["lock"]:
        current = shared_net_state[proto]["sock"]
        if sock is None or current is sock:
            shared_net_state[proto]["sock"] = None


def mark_expected_close(proto, bye_sent=False, server_stop=False):
    with shared_net_state["lock"]:
        shared_net_state[proto]["expected_close"] = True
        if bye_sent:
            shared_net_state[proto]["bye_sent"] = True
        if server_stop:
            shared_net_state[proto]["server_stop"] = True


def expected_close(proto):
    with shared_net_state["lock"]:
        return shared_net_state[proto]["expected_close"]


def server_stop_marked(proto):
    with shared_net_state["lock"]:
        return shared_net_state[proto]["server_stop"]


def send_bye_now(proto, agent_id, server_ip, server_port):
    with shared_net_state["lock"]:
        sock = shared_net_state[proto]["sock"]
        if not socket_is_open(sock) or shared_net_state[proto]["bye_sent"]:
            return False
        shared_net_state[proto]["bye_sent"] = True
        shared_net_state[proto]["expected_close"] = True

    try:
        if proto == "tcp":
            sock.sendall(f"BYE {agent_id}\n".encode())
        else:
            sock.sendto(f"BYE {agent_id}\n".encode(), (server_ip, server_port))
        return True
    except Exception:
        return False


def close_registered_socket(proto):
    with shared_net_state["lock"]:
        sock = shared_net_state[proto]["sock"]
        shared_net_state[proto]["sock"] = None
    if proto == "udp":
        safe_socket_close(sock, shutdown_first=False)
    else:
        safe_socket_close(sock)


def reset_protocol_state(proto):
    with shared_net_state["lock"]:
        shared_net_state[proto]["sock"] = None
        shared_net_state[proto]["bye_sent"] = False
        shared_net_state[proto]["expected_close"] = False
        shared_net_state[proto]["server_stop"] = False


def reset_all_protocol_states():
    reset_protocol_state("tcp")
    reset_protocol_state("udp")


def push_control_event(event_name, proto, reason=""):
    control_queue.put((event_name, proto, reason))


# ============================================================
# Main Function
# ============================================================

def send_manual_messages(sock):
    while True:
        try:
            msg = input()
            if msg.lower() == "exit":
                sock.sendall(f"BYE {AGENT_ID}\n".encode())
                break
            sock.sendall((msg + "\n").encode())
        except Exception:
            break


def handle_exit(sig, frame):
    global running
    running = False
    try:
        client_socket.sendall(f"BYE {AGENT_ID}\n".encode())
    except Exception:
        pass
    safe_socket_close(client_socket)
    print("\n[EXIT] Clean shutdown.")
    sys.exit(0)


signal.signal(signal.SIGINT, handle_exit)


def start_agent():
    """TCP-only agent"""
    global running

    try:
        client_socket.connect((SERVER_IP, SERVER_PORT))

        # HELLO
        client_socket.sendall(f"HELLO {AGENT_ID} {local_hostname()}\n".encode())
        response = client_socket.recv(1024).decode('utf-8').strip()
        print(f"[SERVER RESPONSE]: {response}")

        # Start manual input thread
        threading.Thread(target=send_manual_messages, args=(client_socket,), daemon=True).start()

        # Auto REPORT loop
        while running:

            cpu = psutil.cpu_percent(interval=1)
            ram = psutil.virtual_memory().used / (1024 * 1024)
            timestamp = int(time.time())

            report = f"REPORT {AGENT_ID} {timestamp} {cpu} {ram}\n"


            try:
                client_socket.sendall(report.encode('utf-8'))
            except Exception:
                break

            try:
                data = client_socket.recv(1024)

                if not data:
                    print("[SERVER CLOSED CONNECTION]")
                    break

                ack = data.decode().strip()
                if ack == "STOP":
                    print("[SERVER STOPPED CLEANLY]")
                    running = False
                    break

                print(f"[SENT] CPU: {cpu}%, RAM: {ram:.2f}MB | Status: {ack}")

            except ConnectionResetError:
                if running:
                    print("[SERVER CLOSED CONNECTION]")
                break
            except Exception:
                if running:
                    print("[CONNECTION LOST]")
                break
            time.sleep(T)
    except Exception as e:
        print(f"[ERROR]: {e}")
    finally:
        running = False
        print("[SERVER CLOSED CONNECTION]")
        safe_socket_close(client_socket)


# ============================================================
# GUI- TCP agent
# ============================================================

def start_agent_tcp_gui(server_ip, server_port, agent_id, flag):
    """
    Connects via TCP, sends HELLO, auto-sends REPORT every T seconds.
    Send ALL terminal output to _log() so the GUI picks it up.
    flag[0] = False stops the loop.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    register_socket("tcp", sock)
    hostname = local_hostname()
    close_reason = ""
    hello_ok = False

    try:
        sock.settimeout(5)
        try:
            sock.connect((server_ip, server_port))
        except Exception:
            close_reason = "Server unavailable"
            _log("[TCP] Server unavailable.", "err")
            return

        _log(f"[TCP] Connected to {server_ip}:{server_port}", "tcp")

        hello = f"HELLO {agent_id} {hostname}\n"
        t0 = time.time()
        sock.sendall(hello.encode())
        data = sock.recv(1024)


        if not data:
            print(f"[ERREUR]: {data}")
            close_reason = "Connection closed"
            _log("[TCP] Connection closed by server.", "tcp")
            return

        resp = data.decode('utf-8').strip()
        rtt = (time.time() - t0) * 1000
        hello_ok = True
        push_control_event("connected", "tcp", "")
        _log(f"[TCP] Server: {resp}  (RTT {rtt:.2f} ms)", "tcp")
        tcp_result_queue.put(make_result_row(
            agent_id, hostname, server_ip, server_port,
            "HELLO", resp, time.strftime("%H:%M:%S"), f"{rtt:.2f}", "Connected"
        ))

        while flag[0]:
            cpu = psutil.cpu_percent(interval=1)
            ram = psutil.virtual_memory().used / (1024 * 1024)
            ts = int(time.time())
            if not flag[0]:
                break

            report = f"REPORT {agent_id} {ts} {cpu} {ram}\n"
            t0 = time.time()
            try:
                sock.sendall(report.encode('utf-8'))

                # delay of 4*T
                #time.sleep(4 * T)
            except Exception:
                if not expected_close("tcp"):
                    close_reason = "Connection closed"
                    _log("[TCP] Connection closed.", "tcp")
                    tcp_metrics["failures"] += 1
                break

            try:
                data = sock.recv(1024)
                if not data:
                    if server_stop_marked("tcp"):
                        close_reason = "Disconnected"
                        _log("[TCP] Server unavailable. Connection closed.", "warn")
                    elif not expected_close("tcp"):
                        close_reason = "Connection closed"
                        _log("[TCP] Connection closed by server.", "tcp")
                    break

                ack = data.decode().strip()
                rtt = (time.time() - t0) * 1000

                if ack == "STOP":
                    mark_expected_close("tcp", server_stop=True)
                    flag[0] = False
                    close_reason = "Disconnected"
                    _log("[TCP] Server unavailable. Connection closed.", "warn")
                    tcp_result_queue.put(make_result_row(
                        agent_id, hostname, server_ip, server_port,
                        "STOP", "Server stopped", time.strftime("%H:%M:%S"), "-", "Disconnected"
                    ))
                    break

                _log(f"[TCP] CPU:{cpu:.1f}%  RAM:{ram:.1f}MB  → {ack}  (RTT {rtt:.2f} ms)", "tcp")
                tcp_result_queue.put(make_result_row(
                    agent_id, hostname, server_ip, server_port,
                    "REPORT", f"CPU {cpu:.1f}% | RAM {ram:.1f}MB | {ack}",
                    time.strftime("%H:%M:%S"), f"{rtt:.2f}", "Active"
                ))
                tcp_metrics["requests"] += 1
                tcp_metrics["total_rtt"] += rtt
                tcp_metrics["rtts"].append(rtt)

            except ConnectionResetError:
                if server_stop_marked("tcp"):
                    close_reason = "Disconnected"
                    _log("[TCP] Server unavailable. Connection closed.", "warn")
                elif not expected_close("tcp"):
                    close_reason = "Connection closed"
                    _log("[TCP] Connection closed by server.", "tcp")
                    tcp_metrics["failures"] += 1
                break
            except socket.timeout:
                if not expected_close("tcp"):
                    close_reason = "Server unavailable"
                    _log("[TCP] Server unavailable.", "warn")
                    tcp_metrics["failures"] += 1
                break
            except Exception:
                if server_stop_marked("tcp"):
                    close_reason = "Disconnected"
                    _log("[TCP] Server unavailable. Connection closed.", "warn")
                elif not expected_close("tcp"):
                    close_reason = "Connection closed"
                    _log("[TCP] Connection closed.", "tcp")
                    tcp_metrics["failures"] += 1
                break

            time.sleep(T)

    except Exception as e:
        if not expected_close("tcp"):
            close_reason = "Server unavailable"
            _log(f"[TCP ERROR] {e}", "err")
            tcp_metrics["failures"] += 1
    finally:
        clear_socket("tcp", sock)
        safe_socket_close(sock)
        if close_reason:
            push_control_event("closed", "tcp", close_reason)
        elif hello_ok:
            _log("[TCP] Connection closed.", "tcp")


# ============================================================
# GUI- UDP agent
# ============================================================

def start_agent_udp_gui(server_ip, server_port, agent_id, flag):
    """
    Sends HELLO/REPORT/BYE via UDP.
    Tracks timeouts as packet-loss events for comparison.
    flag[0] = False stops the loop.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5)
    register_socket("udp", sock)
    srv = (server_ip, server_port)
    hostname = local_hostname()
    close_reason = ""
    hello_ok = False
    consecutive_timeouts = 0

    try:
        hello = f"HELLO {agent_id} {hostname}\n"
        t0 = time.time()
        sock.sendto(hello.encode(), srv)
        _log(f"[UDP] Sent HELLO → {server_ip}:{server_port}", "udp")

        try:
            resp_data, _ = sock.recvfrom(1024)
            rtt = (time.time() - t0) * 1000
            resp = resp_data.decode().strip()
            hello_ok = True
            push_control_event("connected", "udp", "")
            _log(f"[UDP] Server: {resp}  (RTT {rtt:.2f} ms)", "udp")
            udp_result_queue.put(make_result_row(
                agent_id, hostname, server_ip, server_port,
                "HELLO", resp, time.strftime("%H:%M:%S"), f"{rtt:.2f}", "Connected"
            ))
        except socket.timeout:
            close_reason = "Server unavailable"
            _log("[UDP] Server unavailable.", "warn")
            udp_metrics["failures"] += 1
            return

        while flag[0]:
            cpu = psutil.cpu_percent(interval=1)
            ram = psutil.virtual_memory().used / (1024 * 1024)
            ts = int(time.time())

            report = f"REPORT {agent_id} {ts} {cpu} {ram}\n"
            t0 = time.time()

            try:
                #for i in range(0, 11):
                sock.sendto(report.encode(), srv)
            except Exception:
                if not expected_close("udp"):
                    close_reason = "Connection closed"
                    _log("[UDP] Socket closed.", "udp")
                    udp_metrics["failures"] += 1
                break

            try:
                data, _ = sock.recvfrom(1024)
                rtt = (time.time() - t0) * 1000
                ack = data.decode().strip()
                consecutive_timeouts = 0

                if ack == "STOP":
                    mark_expected_close("udp", server_stop=True)
                    flag[0] = False
                    close_reason = "Disconnected"
                    _log("[UDP] Server unavailable. Connection closed.", "warn")
                    udp_result_queue.put(make_result_row(
                        agent_id, hostname, server_ip, server_port,
                        "STOP", "Server stopped", time.strftime("%H:%M:%S"), "-", "Disconnected"
                    ))
                    break

                _log(f"[UDP] CPU:{cpu:.1f}%  RAM:{ram:.1f}MB  → {ack}  (RTT {rtt:.2f} ms)", "udp")
                udp_result_queue.put(make_result_row(
                    agent_id, hostname, server_ip, server_port,
                    "REPORT", f"CPU {cpu:.1f}% | RAM {ram:.1f}MB | {ack}",
                    time.strftime("%H:%M:%S"), f"{rtt:.2f}", "Active"
                ))
                udp_metrics["requests"] += 1
                udp_metrics["total_rtt"] += rtt
                udp_metrics["rtts"].append(rtt)

            except socket.timeout:
                consecutive_timeouts += 1
                _log("[UDP] REPORT timeout – packet likely lost!", "udp")
                udp_metrics["failures"] += 1
                udp_result_queue.put(make_result_row(
                    agent_id, hostname, server_ip, server_port,
                    "REPORT", "TIMEOUT", time.strftime("%H:%M:%S"), "LOST", "Waiting"
                ))
                if consecutive_timeouts >= 2:
                    close_reason = "Server unavailable"
                    _log("[UDP] Server unavailable. Connection closed.", "warn")
                    break
            except Exception:
                if server_stop_marked("udp"):
                    close_reason = "Disconnected"
                    _log("[UDP] Server unavailable. Connection closed.", "warn")
                elif not expected_close("udp"):
                    close_reason = "Connection closed"
                    _log("[UDP] Socket closed.", "udp")
                    udp_metrics["failures"] += 1
                break

            time.sleep(T)

    except Exception as e:
        if not expected_close("udp"):
            close_reason = "Server unavailable"
            _log(f"[UDP ERROR] {e}", "err")
            udp_metrics["failures"] += 1
    finally:
        clear_socket("udp", sock)
        safe_socket_close(sock, shutdown_first=False)
        if close_reason:
            push_control_event("closed", "udp", close_reason)
        elif hello_ok:
            _log("[UDP] Socket closed.", "udp")


# ============================================================
# Manual command helpers
# ============================================================

def send_cmd_tcp(server_ip, server_port, agent_id, cmd):
    """
    Open a TCP connection, send HELLO, send cmd, return response.
    Used for the manual 'Send' button so the server has HELLO context.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    hostname = local_hostname()
    try:
        sock.connect((server_ip, server_port))
        # HELLO handshake
        sock.sendall(f"HELLO {agent_id}_cmd {hostname}\n".encode())
        sock.recv(1024)  # discard "OK"
        # Actual command
        if not cmd.endswith("\n"):
            cmd += "\n"
        t0 = time.time()
        sock.sendall(cmd.encode())
        resp = sock.recv(1024).decode().strip()
        rtt = (time.time() - t0) * 1000
        # exit
        try:
            sock.sendall(f"BYE {agent_id}_cmd\n".encode())
        except Exception:
            pass
        row = make_result_row(
            f"{agent_id}_cmd", hostname, server_ip, server_port,
            cmd.strip().split()[0].upper() if cmd.strip() else "CMD",
            resp, time.strftime("%H:%M:%S"), f"{rtt:.2f}", "Manual"
        )
        return resp, rtt, row
    except Exception as e:
        row = make_result_row(
            f"{agent_id}_cmd", hostname, server_ip, server_port,
            cmd.strip().split()[0].upper() if cmd.strip() else "CMD",
            f"ERROR: {e}", time.strftime("%H:%M:%S"), "-", "Failed"
        )
        return f"ERROR: {e}", -1.0, row
    finally:
        safe_socket_close(sock)


def send_cmd_udp(server_ip, server_port, agent_id, cmd):
    """Send a single command via UDP and return (response, rtt, row)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    hostname = local_hostname()
    try:
        if not cmd.endswith("\n"):
            cmd += "\n"
        t0 = time.time()
        sock.sendto(cmd.encode(), (server_ip, server_port))
        resp, _ = sock.recvfrom(1024)
        rtt = (time.time() - t0) * 1000
        row = make_result_row(
            agent_id, hostname, server_ip, server_port,
            cmd.strip().split()[0].upper() if cmd.strip() else "CMD",
            resp.decode().strip(), time.strftime("%H:%M:%S"), f"{rtt:.2f}", "Manual"
        )
        return resp.decode().strip(), rtt, row
    except socket.timeout:
        row = make_result_row(
            agent_id, hostname, server_ip, server_port,
            cmd.strip().split()[0].upper() if cmd.strip() else "CMD",
            "TIMEOUT", time.strftime("%H:%M:%S"), "LOST", "Waiting"
        )
        return "TIMEOUT", -1.0, row
    except Exception as e:
        row = make_result_row(
            agent_id, hostname, server_ip, server_port,
            cmd.strip().split()[0].upper() if cmd.strip() else "CMD",
            f"ERROR: {e}", time.strftime("%H:%M:%S"), "-", "Failed"
        )
        return f"ERROR: {e}", -1.0, row
    finally:
        safe_socket_close(sock, shutdown_first=False)


# ============================================================
# GUI
# ============================================================

class ClientGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Network Agent Client  –  TCP & UDP")
        self.root.geometry("1280x840")
        self.root.configure(bg="#1e1e2e")
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self._build_scrollable_root()

        self.server_ip = tk.StringVar(value=SERVER_IP)
        self.server_port = tk.IntVar(value=SERVER_PORT)
        self.agent_id = tk.StringVar(value=AGENT_ID)
        self.proto_var = tk.StringVar(value="TCP")

        self.running_flag = [False]
        self.active_protocols = set()
        self.closed_protocols = set()
        self.result_columns = [
            ("ip", "IP address", 130, 2, tk.CENTER),
            ("port", "Port", 75, 1, tk.CENTER),
            ("command", "Command", 95, 1, tk.CENTER),
            ("response", "Response", 220, 3, tk.CENTER),
            ("timestamp", "Times", 95, 1, tk.CENTER),
            ("rtt", "RTT", 85, 1, tk.CENTER),
            ("status", "Status", 105, 1, tk.CENTER),
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

    # ── Full UI layout ───────────────────────────────────────
    def _build_ui(self):

        # ── TOP BAR ──────────────────────────────────────────
        top = tk.Frame(self.page, bg="#181825", pady=8, padx=10)
        top.pack(fill=tk.X)

        tk.Label(top, text="Network Agent Client",
                 bg="#181825", fg="#89b4fa",
                 font=("Consolas", 14, "bold")).pack(side=tk.LEFT)

        self.status_lbl = tk.Label(top, text="● IDLE",
                                   bg="#181825", fg="#6c7086",
                                   font=("Consolas", 11, "bold"))
        self.status_lbl.pack(side=tk.LEFT, padx=15)

        # Connection config
        cfg = tk.Frame(top, bg="#181825")
        cfg.pack(side=tk.LEFT, padx=18)

        def _lbl(txt, col):
            tk.Label(cfg, text=txt, bg="#181825", fg="#cdd6f4",
                     font=("Consolas", 9)).grid(row=0, column=col, padx=2)

        def _ent(var, col, w):
            tk.Entry(cfg, textvariable=var, bg="#313244", fg="#cdd6f4",
                     font=("Consolas", 9), width=w,
                     insertbackground="white", relief=tk.FLAT
                     ).grid(row=0, column=col, padx=2, ipady=2)

        _lbl("Server IP:", 0); _ent(self.server_ip, 1, 15)
        _lbl("Port:", 2); _ent(self.server_port, 3, 6)
        _lbl("Agent ID:", 4); _ent(self.agent_id, 5, 14)

        self.bye_btn = tk.Button(
            top, text="⏹ Bye",
            bg="#f38ba8", fg="#00001c", font=("Consolas", 10, "bold"),
            padx=10, pady=4, relief=tk.FLAT, bd=0,
            activebackground="#eba0ac", state=tk.DISABLED,
            command=self._send_bye)
        self.bye_btn.pack(side=tk.RIGHT, padx=4)

        self.start_btn = tk.Button(
            top, text="▶  Start",
            bg="#099b15", fg="#00001c", font=("Consolas", 10, "bold"),
            padx=10, pady=4, relief=tk.FLAT, bd=0,
            activebackground="#94e2d5",
            command=self._on_start)
        self.start_btn.pack(side=tk.RIGHT, padx=4)

        # ── PROTOCOL SELECTOR ────────────────────────────────
        proto_bar = tk.Frame(self.page, bg="#181825", pady=5, padx=10)
        proto_bar.pack(fill=tk.X)

        tk.Label(proto_bar, text="Protocol:",
                 bg="#181825", fg="#cba6f7",
                 font=("Consolas", 10, "bold")).pack(side=tk.LEFT)

        for proto in ["TCP", "UDP", "Both"]:
            tk.Radiobutton(
                proto_bar, text=proto, variable=self.proto_var, value=proto,
                bg="#181825", fg="#cdd6f4",
                selectcolor="#313244", activebackground="#181825",
                activeforeground="#cdd6f4",
                font=("Consolas", 10, "bold")
            ).pack(side=tk.LEFT, padx=14)

        # ── COMMAND BAR ──────────────────────────────────────
        cmd_bar = tk.Frame(self.page, bg="#313244", pady=5, padx=10)
        cmd_bar.pack(fill=tk.X)

        tk.Label(cmd_bar, text="Command:",
                 bg="#313244", fg="#cdd6f4",
                 font=("Consolas", 10)).pack(side=tk.LEFT)

        self.cmd_entry = tk.Entry(
            cmd_bar, bg="#45475a", fg="#cdd6f4",
            font=("Consolas", 10), insertbackground="white",
            relief=tk.FLAT, width=55)
        self.cmd_entry.pack(side=tk.LEFT, padx=8, ipady=4)
        self.cmd_entry.bind("<Return>", lambda _: self._on_send())

        self.send_btn = tk.Button(
            cmd_bar, text="Send",
            bg="#89b4fa", fg="#00001c", font=("Consolas", 10, "bold"),
            padx=10, pady=3, relief=tk.FLAT, bd=0,
            activebackground="#74c7ec", state=tk.DISABLED,
            command=self._on_send)
        self.send_btn.pack(side=tk.LEFT, padx=4)

        # ── MAIN CONTENT ─────────────────────────────────────
        vp = tk.PanedWindow(self.page, orient=tk.VERTICAL,
                            bg="#1e1e2e", sashwidth=6,
                            sashrelief=tk.FLAT, sashpad=2)
        vp.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        # Middle row: comparison left, terminal log right
        middle = tk.Frame(vp, bg="#1e1e2e")
        vp.add(middle, minsize=300)

        mid_hp = tk.PanedWindow(middle, orient=tk.HORIZONTAL,
                                bg="#1e1e2e", sashwidth=6,
                                sashrelief=tk.FLAT, sashpad=2)
        mid_hp.pack(fill=tk.BOTH, expand=True)

        cmp_frm = tk.Frame(mid_hp, bg="#1e1e2e")
        mid_hp.add(cmp_frm, minsize=330)
        tk.Label(cmp_frm, text="🔵 TCP vs UDP Comparison",
                 bg="#1e1e2e", fg="#fab387",
                 font=("Consolas", 11, "bold")).pack(anchor=tk.W)
        self.cmp_box = scrolledtext.ScrolledText(
            cmp_frm, bg="#11111b", fg="#cdd6f4",
            font=("Consolas", 9), relief=tk.FLAT)
        self.cmp_box.pack(fill=tk.BOTH, expand=True)
        self.cmp_box.configure(state=tk.DISABLED)

        log_frm = tk.Frame(mid_hp, bg="#1e1e2e")
        mid_hp.add(log_frm, minsize=420)
        tk.Label(log_frm, text="🔵 Terminal Log",
                 bg="#1e1e2e", fg="#89b4fa",
                 font=("Consolas", 11, "bold")).pack(anchor=tk.W)
        self.log_box = scrolledtext.ScrolledText(
            log_frm, bg="#11111b", fg="#cdd6f4",
            font=("Consolas", 9), relief=tk.FLAT,
            insertbackground="#cdd6f4")
        self.log_box.pack(fill=tk.BOTH, expand=True)
        self.log_box.configure(state=tk.DISABLED)
        self.log_box.tag_config("tcp", foreground="#89dceb")
        self.log_box.tag_config("udp", foreground="#cba6f7")
        self.log_box.tag_config("err", foreground="#f38ba8")
        self.log_box.tag_config("ok", foreground="#a6e3a1")
        self.log_box.tag_config("warn", foreground="#fab387")

        # Bottom row: TCP above UDP
        bottom = tk.Frame(vp, bg="#1e1e2e")
        vp.add(bottom, minsize=160)

        bottom1 = tk.Frame(vp, bg="#1e1e2e")
        vp.add(bottom1, minsize=160)

        tcp_frm = tk.Frame(bottom, bg="#1e1e2e")
        tcp_frm.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        tk.Label(tcp_frm, text="🔵 TCP Connection Section",
                 bg="#1e1e2e", fg="#89b4fa",
                 font=("Consolas", 11, "bold")).pack(anchor=tk.W)
        self.tcp_tree = self._make_result_table(tcp_frm)

        udp_frm = tk.Frame(bottom1, bg="#1e1e2e")
        udp_frm.pack(fill=tk.BOTH, expand=True)
        tk.Label(udp_frm, text="🟣 UDP Connection Section",
                 bg="#1e1e2e", fg="#cba6f7",
                 font=("Consolas", 11, "bold")).pack(anchor=tk.W)
        self.udp_tree = self._make_result_table(udp_frm)

    def _make_result_table(self, parent):
        wrapper = tk.Frame(parent, bg="#1e1e2e")
        wrapper.pack(fill=tk.BOTH, expand=True)
        wrapper.grid_rowconfigure(0, weight=1)
        wrapper.grid_columnconfigure(0, weight=1)

        col_ids = [c[0] for c in self.result_columns]
        tree = ttk.Treeview(wrapper, columns=col_ids, show="headings")
        for cid, title, width, _weight, anchor in self.result_columns:
            minw = max(55, int(width * 0.45))
            tree.heading(cid, text=title)
            tree.column(cid, width=width, minwidth=minw, anchor=anchor, stretch=True)

        ysb = ttk.Scrollbar(wrapper, orient=tk.VERTICAL, command=tree.yview)
        xsb = ttk.Scrollbar(wrapper, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)

        tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")

        tree._column_specs = self.result_columns
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
    def _gui_log(self, msg, tag=None):
        self.log_box.configure(state=tk.NORMAL)
        if tag:
            self.log_box.insert(tk.END, msg + "\n", tag)
        else:
            self.log_box.insert(tk.END, msg + "\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)

    # ── Result-table row insertion ───────────────────────────
    def _ins_tcp(self, row):
        self.tcp_tree.insert("", tk.END, values=(
            row["ip"], row["port"], row["command"], row["response"],
            row["timestamp"], row["rtt"], row["status"]
        ))
        self.tcp_tree.yview_moveto(1)

    def _ins_udp(self, row):
        self.udp_tree.insert("", tk.END, values=(
            row["ip"], row["port"], row["command"], row["response"],
            row["timestamp"], row["rtt"], row["status"]
        ))
        self.udp_tree.yview_moveto(1)

    # ── Queue polling (100 ms) ──────────────────────────────
    def _poll_queues(self):
        try:
            while True:
                msg, tag = log_queue.get_nowait()
                self._gui_log(msg, tag)
        except queue.Empty:
            pass

        try:
            while True:
                self._ins_tcp(tcp_result_queue.get_nowait())
        except queue.Empty:
            pass

        try:
            while True:
                self._ins_udp(udp_result_queue.get_nowait())
        except queue.Empty:
            pass

        try:
            while True:
                event_name, proto, reason = control_queue.get_nowait()
                self._handle_protocol_event(event_name, proto, reason)
        except queue.Empty:
            pass

        self.root.after(100, self._poll_queues)

    def _handle_protocol_event(self, event_name, proto, reason):
        if event_name == "connected":
            self.closed_protocols.discard(proto)
            return

        if event_name != "closed":
            return

        if not self.active_protocols:
            return

        self.closed_protocols.add(proto)
        remaining = self.active_protocols - self.closed_protocols
        if not remaining:
            message = reason or "Disconnected"
            self._finalize_disconnect(message, tag="warn")
        elif self.running_flag[0]:
            pretty = ", ".join(sorted(p.upper() for p in remaining))
            self.status_lbl.configure(text=f"● {pretty}", fg="#f9e2af")
            self._gui_log(f"[GUI] {proto.upper()} disconnected. Remaining active: {pretty}.", "warn")

    def _refresh_comparison(self):
        t_req = tcp_metrics["requests"]
        u_req = udp_metrics["requests"]
        t_avg = (tcp_metrics["total_rtt"] / t_req) if t_req > 0 else 0.0
        u_avg = (udp_metrics["total_rtt"] / u_req) if u_req > 0 else 0.0
        t_fail = tcp_metrics["failures"]
        u_fail = udp_metrics["failures"]
        t_rel = (t_req / (t_req + t_fail) * 100) if (t_req + t_fail) > 0 else 100.0
        u_rel = (u_req / (u_req + u_fail) * 100) if (u_req + u_fail) > 0 else 100.0
        t_min = min(tcp_metrics["rtts"]) if tcp_metrics["rtts"] else 0.0
        t_max = max(tcp_metrics["rtts"]) if tcp_metrics["rtts"] else 0.0
        u_min = min(udp_metrics["rtts"]) if udp_metrics["rtts"] else 0.0
        u_max = max(udp_metrics["rtts"]) if udp_metrics["rtts"] else 0.0

        sep = "─" * 34
        rows = [
            sep,
            f"  {'Metric':<17} {'TCP':>7}  {'UDP':>7}",
            sep,
            f"  {'Requests':<17} {t_req:>7}  {u_req:>7}",
            f"  {'Avg RTT (ms)':<17} {t_avg:>7.2f}  {u_avg:>7.2f}",
            f"  {'Min RTT (ms)':<17} {t_min:>7.2f}  {u_min:>7.2f}",
            f"  {'Max RTT (ms)':<17} {t_max:>7.2f}  {u_max:>7.2f}",
            f"  {'Packet Loss':<17} {t_fail:>7}  {u_fail:>7}",
            f"  {'Reliability %':<17} {t_rel:>6.1f}%  {u_rel:>6.1f}%",
            sep,
        ]

        notes = []
        if t_req > 0 and u_req > 0:
            if t_avg < u_avg:
                notes.append(f"✔ TCP faster by {u_avg - t_avg:.2f} ms")
            elif u_avg < t_avg:
                notes.append(f"✔ UDP faster by {t_avg - u_avg:.2f} ms")
            else:
                notes.append("≈ Similar RTT for both")
        if u_fail > t_fail:
            notes.append(f"⚠ UDP lost {u_fail - t_fail} more packets than TCP")
        elif t_fail > u_fail:
            notes.append(f"⚠ TCP had {t_fail - u_fail} more failures than UDP")

        proto = self.proto_var.get()
        notes += [
            "",
            f"  Active mode : {proto}",
            "  TCP  : reliable, ordered stream",
            "  UDP  : fast, low-overhead dgram",
        ]

        txt = "\n".join(rows) + "\n" + "\n".join(notes)
        self.cmp_box.configure(state=tk.NORMAL)
        self.cmp_box.delete("1.0", tk.END)
        self.cmp_box.insert("1.0", txt)
        self.cmp_box.configure(state=tk.DISABLED)

        self.root.after(3000, self._refresh_comparison)

    # ── Button handlers ──────────────────────────────────────
    def _on_start(self):
        if self.running_flag[0]:
            self._gui_log("[GUI] Agent is already running.", "warn")
            return

        reset_all_protocol_states()
        self.running_flag[0] = True
        self.start_btn.configure(state=tk.DISABLED)
        self.bye_btn.configure(state=tk.NORMAL)
        self.send_btn.configure(state=tk.NORMAL)

        proto = self.proto_var.get()
        ip = self.server_ip.get()
        port = self.server_port.get()
        aid = self.agent_id.get()

        self.active_protocols = set()
        if proto in ("TCP", "Both"):
            self.active_protocols.add("tcp")
        if proto in ("UDP", "Both"):
            self.active_protocols.add("udp")
        self.closed_protocols = set()

        self.status_lbl.configure(text=f"● {proto.upper()}", fg="#a6e3a1")
        self._gui_log(f"[GUI] Starting in [{proto}] mode  →  {ip}:{port}", "ok")

        if proto in ("TCP", "Both"):
            threading.Thread(
                target=start_agent_tcp_gui,
                args=(ip, port, aid, self.running_flag),
                daemon=True
            ).start()

        if proto in ("UDP", "Both"):
            threading.Thread(
                target=start_agent_udp_gui,
                args=(ip, port, aid, self.running_flag),
                daemon=True
            ).start()

    def _finalize_disconnect(self, message, tag="ok", from_window=False):
        self.running_flag[0] = False
        close_registered_socket("tcp")
        close_registered_socket("udp")
        reset_all_protocol_states()
        self.active_protocols = set()
        self.closed_protocols = set()

        self.start_btn.configure(state=tk.NORMAL)
        self.bye_btn.configure(state=tk.DISABLED)
        self.send_btn.configure(state=tk.DISABLED)
        self.status_lbl.configure(text="● IDLE", fg="#6c7086")
        self._gui_log(f"[GUI] {message}.", tag)

        if from_window:
            self.root.after(100, self.root.destroy)

    def _send_bye(self, from_window=False):
        if not self.running_flag[0] and not socket_is_open(shared_net_state["tcp"]["sock"]) and not socket_is_open(shared_net_state["udp"]["sock"]):
            if from_window:
                self.root.destroy()
            return

        aid = self.agent_id.get()
        ip = self.server_ip.get()
        port = self.server_port.get()

        self.running_flag[0] = False
        tcp_sent = send_bye_now("tcp", aid, ip, port)
        udp_sent = send_bye_now("udp", aid, ip, port)

        message = "BYE – agent disconnected normally" if (tcp_sent or udp_sent) else "Connection closed"
        self._finalize_disconnect(message, tag="ok", from_window=from_window)

    def _handle_manual_bye_command(self, cmd):
        parts = cmd.strip().split()
        if not parts or parts[0].lower() != "bye":
            return False

        if len(parts) != 2:
            self._gui_log("[GUI] Syntax error: use 'bye <agent_name>'.", "err")
            return True

        target = parts[1]
        current = self.agent_id.get().strip()
        if current and target != current:
            self._gui_log(f"[GUI] BYE requested for {target}; disconnecting current agent {current}.", "warn")
        else:
            self._gui_log(f"[GUI] Manual BYE command received for {current or target}.", "ok")
        self._send_bye()
        return True

    def _on_send(self):
        cmd = self.cmd_entry.get().strip()
        if not cmd:
            return
        self.cmd_entry.delete(0, tk.END)

        if self._handle_manual_bye_command(cmd):
            return

        proto = self.proto_var.get()
        ip = self.server_ip.get()
        port = self.server_port.get()
        aid = self.agent_id.get()

        self._gui_log(f"[CMD] ► {cmd}", "ok")

        if proto in ("TCP", "Both"):
            def _tcp():
                resp, rtt, row = send_cmd_tcp(ip, port, aid, cmd)
                rtt_s = f"{rtt:.2f} ms" if rtt >= 0 else "ERROR"
                _log(f"[TCP] ← {resp}  ({rtt_s})", "tcp")
                tcp_result_queue.put(row)
            threading.Thread(target=_tcp, daemon=True).start()

        if proto in ("UDP", "Both"):
            def _udp():
                resp, rtt, row = send_cmd_udp(ip, port, aid, cmd)
                rtt_s = f"{rtt:.2f} ms" if rtt >= 0 else "TIMEOUT"
                _log(f"[UDP] ← {resp}  ({rtt_s})", "udp")
                udp_result_queue.put(row)
            threading.Thread(target=_udp, daemon=True).start()

    def _on_window_close(self):
        self._gui_log("[GUI] Closing client window...", "warn")
        self._send_bye(from_window=True)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    root = tk.Tk()
    app = ClientGUI(root)
    root.mainloop()

# Distributed Network Monitoring System 🚀

This project is a network performance monitoring application based on a **Client-Server** architecture using **Sockets**. The system allows real-time monitoring of CPU and RAM consumption for multiple machines simultaneously through an advanced Graphical User Interface (GUI).

## 📌 Project Features
- **Dual Protocol Support:** Operates using both **TCP** (for reliable connection) and **UDP** (for speed and comparison).
- **Graphical User Interface (GUI):** Interactive interfaces for both the Server and the Client using the `Tkinter` library.
- **Multi-threading:** Capability to handle a large number of clients (Agents) at the same time.
- **Performance Analysis:** Calculates consumption averages and tracks device status (active/inactive) in real-time.
- **Attack Simulation:** Built-in feature to test the server's resilience against high message loads (Flood).
- **Smart Error Handling:** Automatic detection of malformed messages or sudden disconnections.

## 🏗️ System Architecture
The project follows the **Client-Server** model:
- **Agent (Client):** Collects local system metrics and sends them according to a specific protocol format.
- **Collector (Server):** Receives data, processes it, displays it in tables, and monitors the activity status of each client.

## 📝 Protocol Specification
Communication is performed via the following literal messages:
- `HELLO <agent_id> <hostname>`: To register the client with the server.
- `REPORT <agent_id> <timestamp> <cpu> <ram>`: To send periodic performance reports.
- `BYE <agent_id>`: To close the connection gracefully.
- The server responds with `OK` on success or `ERROR` if there is a formatting issue.

## 🛠️ Technical Prerequisites
- **Language:** Python 3.x
- **Standard Libraries:** `socket`, `threading`, `time`, `tkinter`, `json`.
- **External Libraries (to be installed):**
  ```bash
  pip install psutil
  ```

---

## 🚀 User Manual (Important Instructions)

### 1. Starting the Server
Run the server file first to open the reception channels:
```bash
python server.py
```
The server will open a GUI displaying the status of clients and reports received via TCP and UDP ports.

### 2. Starting the Client (Agent)
You can launch the client using the following command:
```bash
python client.py
```

**⚠️ Important alerts before clicking the "Start" button:**
1.  **Changing the Agent Name (Agent ID):** The application allows the user to modify the device identifier (**Agent ID**) directly via the settings interface **before** launching the connection. This allows you to customize the device display and easily identify it on the server's central dashboard.
2.  **Server IP Verification:** It is **imperative** to verify that the server's **IP address** is correctly entered in the dedicated field before clicking the **Start** button. Without a correct IP configuration, the client will not be able to establish a connection with the server.

---

## 🧪 Test Cases
The system has been successfully tested in the following scenarios:
- **Single Client Connection:** Verification of successful HELLO and REPORT message exchange.
- **Multiple Simultaneous Clients:** Multiple agents running at the same time with the server correctly separating their data.
- **Inactivity Detection:** The server stops considering a client "active" if it fails to send a report for more than $3 \times T$ seconds (15 seconds).
- **Malformed Messages:** Sending incorrect data to verify that the server responds with `ERROR`.
- **Stress Simulation:** Testing the UDP protocol under intensive transmission to compare packet loss.

## 👥 Project Team
* [SABRINE BEN TILI](https://www.linkedin.com/in/sabrine-ben-tili/) [@girll235](https://github.com/girll235/)
* [MOHAMED AMMOUS](https://www.facebook.com/mouhamed.ammous) [@MohamedAmmous](https://github.com/MohamedAmmous)

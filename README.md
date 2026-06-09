# RDP Session Tracker

Monitor who is connected via Remote Desktop to which machine, with live duration,
start time, end time, and session status — from a central web dashboard.

```
rdp_tracker/
├── backend/
│   ├── main.py            ← FastAPI server (central machine)
│   ├── requirements.txt
│   └── rdp_sessions.db    ← SQLite DB (auto-created on first run)
├── collector/
│   └── rdp_collector.ps1  ← Runs on EACH target Windows machine
└── dashboard/
    └── index.html         ← Open in any browser
```

---

## Architecture

```
  [Target Machine A]  →  rdp_collector.ps1 ──┐
  192.168.24.126                               │   POST /api/event
                                               ▼
  [Target Machine B]  →  rdp_collector.ps1 ──► FastAPI Server :8000
  192.168.24.260                               │
                                               ▼
  [Target Machine C]  →  rdp_collector.ps1 ──► SQLite DB
                                               │
                                               ▼
                                           dashboard/index.html
                                           (polls GET /api/sessions)
```

---

## Step 1 — Set Up the Backend Server

Pick any always-on machine (can be one of the target machines or a separate server).

### Install Python dependencies
```bash
pip install fastapi uvicorn aiofiles pydantic
```

### Start the server
```bash
cd rdp_tracker/backend
uvicorn main:app --host 0.0.0.0 --port 8000
```

The server will:
- Create `rdp_sessions.db` automatically on first run
- Serve the dashboard at  http://YOUR_SERVER_IP:8000/
- Expose REST API at      http://YOUR_SERVER_IP:8000/docs

### Run as a Windows Service (optional)
Install `pywin32` and use NSSM or Windows Task Scheduler:
```
nssm install RDPTracker "python" "-m uvicorn main:app --host 0.0.0.0 --port 8000"
nssm set RDPTracker AppDirectory C:\rdp_tracker\backend
nssm start RDPTracker
```

---

## Step 2 — Deploy Collector to Each Target Machine

Copy `rdp_collector.ps1` to every Windows machine you want to monitor.

### Edit the BackendUrl
Open `rdp_collector.ps1` and change:
```powershell
[string]$BackendUrl = "http://192.168.24.10:8000"   # <-- your server IP
```

### Run manually (for testing)
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\rdp_collector.ps1 -BackendUrl "http://192.168.24.10:8000" -PollIntervalSec 10
```

### Install as a Scheduled Task (runs every minute, SYSTEM account)
Open PowerShell as Administrator on the target machine:

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
             -Argument "-NonInteractive -File C:\rdp_tracker\rdp_collector.ps1 -BackendUrl http://192.168.24.10:8000 -RunOnce"
$trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 1) -Once -At (Get-Date)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "RDP-Collector" -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Highest -User "SYSTEM"
```

> The `-RunOnce` flag exits after one poll cycle — the Task Scheduler re-runs it every minute,
> keeping the lookback window (default 2 min) larger than the poll interval to avoid gaps.

---

## Step 3 — Open the Dashboard

Navigate to:
```
http://YOUR_SERVER_IP:8000
```

Or open `dashboard/index.html` directly in a browser — it will auto-connect to the backend.

### What the dashboard shows
| Column      | Description                                        |
|-------------|----------------------------------------------------|
| User        | Windows account name of the remote user            |
| Target Host | IP + hostname of the machine being connected to    |
| Source IP   | IP of the machine the user is connecting FROM      |
| Start Time  | When the RDP session began                         |
| End Time    | When the session ended (— if still active)         |
| Duration    | Live ticking timer for active sessions             |
| Status      | Active / Disconnected / Ended                      |
| Event ID    | The Windows Event ID that last updated this row    |

---

## API Reference

| Method | Endpoint              | Description                          |
|--------|-----------------------|--------------------------------------|
| POST   | /api/event            | Receive event from collector         |
| GET    | /api/sessions         | List sessions (filterable)           |
| GET    | /api/stats            | Summary counts                       |
| GET    | /api/hosts            | All unique target IPs                |
| GET    | /api/events/raw       | Raw event audit log                  |
| DELETE | /api/sessions/{id}    | Delete a session record              |
| GET    | /health               | Health check                         |
| GET    | /docs                 | Swagger API docs (auto-generated)    |

### Query parameters for /api/sessions
```
?status=active          filter by status (active | disconnected | ended)
?target_ip=192.168.x.x  filter by target host
?user=rahul             partial username match
?limit=200              max rows returned (default 200, max 1000)
```

---

## Windows Event IDs Used

| Event ID | Log                          | Meaning                          |
|----------|------------------------------|----------------------------------|
| 4624     | Security                     | Logon (Type 10 = RDP)            |
| 4634     | Security                     | Logoff                           |
| 4647     | Security                     | User-initiated logoff            |
| 4778     | Security                     | Session reconnected              |
| 4779     | Security                     | Session disconnected             |
| 21       | TerminalServices-LocalSM     | RDP session logon succeeded      |
| 23       | TerminalServices-LocalSM     | RDP session logoff succeeded     |
| 24       | TerminalServices-LocalSM     | RDP session disconnected         |
| 25       | TerminalServices-LocalSM     | RDP session reconnection succeeded|

---

## Firewall

Allow inbound on the backend server:
```powershell
New-NetFirewallRule -DisplayName "RDP Tracker API" -Direction Inbound `
    -Protocol TCP -LocalPort 8000 -Action Allow
```

---

## Troubleshooting

| Problem                        | Fix                                                        |
|--------------------------------|------------------------------------------------------------|
| No sessions appearing          | Run collector manually and check console output            |
| "Access denied" on event log   | Run PowerShell as Administrator / SYSTEM                   |
| Backend unreachable            | Check firewall rule on server, verify IP in collector      |
| Sessions not closing           | Ensure both 4634/23 events are being generated on the host |
| Duplicate sessions             | Check logon_id extraction in collector output              |

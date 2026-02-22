# Two-Laptop Setup Guide

> **Roboflow Annotation Batch Automation — Cross-Machine Deployment**
>
> This guide walks you through deploying the automation across two laptops so they process annotation jobs in parallel without collisions.

---

## Architecture Overview

```
┌─────────────────────────────┐         ┌─────────────────────────────┐
│        LAPTOP A (Server)    │         │        LAPTOP B (Worker)    │
│                             │  HTTP   │                             │
│  coordination_server.py ◄───┼─────────┼──► main.py (automation)    │
│  (port 8099)                │  :8099  │     ↳ HTTPCoordinator       │
│                             │         │     ↳ RemoteLogHandler      │
│  main.py (automation)       │         │     ↳ Heartbeat thread      │
│    ↳ HTTPCoordinator        │         │                             │
│    ↳ RemoteLogHandler       │         │                             │
│    ↳ Heartbeat thread       │         │                             │
│                             │         │                             │
│  Dashboard: localhost:8099  │         │                             │
│  /dashboard                 │         │                             │
└─────────────────────────────┘         └─────────────────────────────┘
```

**Laptop A** runs both the coordination server AND the automation.
**Laptop B** runs only the automation and connects to Laptop A's server.

---

## Prerequisites (Both Laptops)

| Requirement     | Version  | Check command               |
|-----------------|----------|-----------------------------|
| Python          | ≥ 3.11   | `python --version`          |
| pip             | latest   | `pip --version`             |
| Network         | Same LAN | Both on same Wi-Fi/Ethernet |

---

## Step 0: Prepare the ZIP on Laptop A

Before compressing, **exclude** these files/folders that are machine-specific or unnecessary:

| Exclude              | Reason                                          |
|----------------------|-------------------------------------------------|
| `session.json`       | Auth cookies — different per browser/machine     |
| `coordination.json`  | Runtime state — will be recreated by the server  |
| `logs/`              | Old logs/screenshots — not needed on Laptop B    |
| `__pycache__/`       | Compiled bytecode — Python regenerates it        |
| `.env`               | Environment secrets (if any)                     |

### How to compress (Windows):

1. Open the `automation-project` folder
2. Select **all files and folders** EXCEPT: `session.json`, `coordination.json`, `logs/`, `__pycache__/`
3. Right-click → **Send to → Compressed (zipped) folder**
4. Name it `automation-project.zip`

**Or use PowerShell:**

```powershell
cd d:\
$exclude = @('session.json', 'coordination.json')
Compress-Archive -Path "automation-project\*" -DestinationPath "automation-project.zip" -Force
# Note: manually delete session.json, coordination.json, logs/, __pycache__/ from the zip
# OR use the selective approach below:

# Cleaner approach — copy first, then zip:
$temp = "$env:TEMP\automation-project-clean"
if (Test-Path $temp) { Remove-Item $temp -Recurse -Force }
Copy-Item "automation-project" $temp -Recurse
Remove-Item "$temp\session.json" -ErrorAction SilentlyContinue
Remove-Item "$temp\coordination.json" -ErrorAction SilentlyContinue
Remove-Item "$temp\logs" -Recurse -ErrorAction SilentlyContinue
Remove-Item "$temp\__pycache__" -Recurse -ErrorAction SilentlyContinue
Get-ChildItem "$temp" -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Compress-Archive -Path "$temp\*" -DestinationPath "d:\automation-project.zip" -Force
Remove-Item $temp -Recurse -Force
Write-Host "Done: d:\automation-project.zip"
```

5. Transfer the ZIP to Laptop B (USB, cloud drive, network share, etc.)

---

## Step 1: Laptop A — Find Your IP Address

You need Laptop A's local IP so Laptop B can connect to it.

```powershell
ipconfig
```

Look for your **Wi-Fi** or **Ethernet** adapter and note the `IPv4 Address`.
Example: `192.168.1.50`

> **Keep this IP handy** — you'll use it in Step 4.

---

## Step 2: Laptop A — Install Dependencies & Playwright

Open a terminal in the `automation-project` folder:

```powershell
cd d:\automation-project

# Create virtual environment (recommended)
python -m venv venv
.\venv\Scripts\Activate

# Install Python packages
pip install -r requirements.txt

# Install Playwright browsers (first time only — downloads ~400MB)
python -m playwright install chromium
```

---

## Step 3: Laptop A — Configure `config.yaml`

The default config should already work for Laptop A. Verify these key settings:

```yaml
# --- Coordination ---
enable_coordination: true
coordination_mode: "http"
coordination_server_url: "http://localhost:8099"   # ← localhost because server runs here

# --- Remote Monitoring ---
remote_logging: true
heartbeat_interval: 30
auto_update: false          # No need — this IS the source of truth

# --- Browser ---
headless: false             # Set to true for unattended runs
```

No changes needed if these are already set.

---

## Step 4: Laptop B — Extract, Install & Configure

### 4a. Extract the ZIP

Extract `automation-project.zip` to a convenient location, e.g. `D:\automation-project\`.

### 4b. Install dependencies

```powershell
cd d:\automation-project

# Create virtual environment
python -m venv venv
.\venv\Scripts\Activate

# Install Python packages
pip install -r requirements.txt

# Install Playwright browsers
python -m playwright install chromium
```

### 4c. Edit `config.yaml` — **Critical Changes**

Open `config.yaml` and change **only** these values:

```yaml
# Point to Laptop A's IP address (from Step 1)
coordination_server_url: "http://192.168.1.50:8099"    # ← Replace with Laptop A's actual IP

# Enable auto-update so Laptop B pulls code changes from the server
auto_update: true

# Everything else stays the same
```

> **Important:** The `email` must be the same Roboflow account on both laptops.
> Both laptops log into the **same** Roboflow workspace.

---

## Step 5: Laptop A — Start the Coordination Server

**This must run FIRST, before any automation on either laptop.**

Open a **new terminal** (keep it running):

```powershell
cd d:\automation-project
.\venv\Scripts\Activate

# Start the server (--reset wipes previous coordination state)
python -m src.coordination_server --reset
```

Expected output:
```
INFO — Starting with empty state (--reset)
INFO — Auto-save every 30s to coordination.json
INFO — Code manifest: 10 deployable files
============================================================
  Coordination server running on 0.0.0.0:8099
  Stale timeout:  1800s
  Save interval:  30s
  Data file:      coordination.json
============================================================
```

### Verify the dashboard:

Open a browser on Laptop A and go to:
```
http://localhost:8099/dashboard
```

You should see the dark-themed monitoring dashboard with empty worker list and log stream.

### Verify from Laptop B (optional):

On Laptop B, open a browser and go to:
```
http://192.168.1.50:8099/dashboard
```
(Replace with Laptop A's actual IP)

If this doesn't load, see [Firewall Troubleshooting](#firewall-troubleshooting) below.

---

## Step 6: Laptop A — Start the Automation

Open a **second terminal** on Laptop A (keep the server running in the first):

```powershell
cd d:\automation-project
.\venv\Scripts\Activate

python main.py
```

**First run:** You'll need to authenticate via magic link:
1. The script will ask if you have a magic link
2. Choose `[2] No` → it opens the Roboflow login page and enters your email
3. Check your email (Outlook) for the magic link
4. Paste the magic link URL into the terminal
5. The session is saved to `session.json` for future runs

After authentication, the automation starts Phase 2 (or whichever phase is configured).

---

## Step 7: Laptop B — Start the Automation

Open a terminal on Laptop B:

```powershell
cd d:\automation-project
.\venv\Scripts\Activate

python main.py
```

**First run on Laptop B:** You'll need to authenticate here too (separate browser session):
1. Same magic-link flow as Step 6
2. Use the **same email** — check your email for a new magic link
3. Paste it into the terminal
4. Session is saved locally to `session.json` on Laptop B

> **Note:** Each laptop maintains its own `session.json`. The auth session is
> independent per machine. You may need to request two magic links (one per laptop).

---

## Step 8: Monitor from the Dashboard

On **any device** on the same network (even a phone), open:

```
http://192.168.1.50:8099/dashboard
```

### Dashboard Features:

| Section              | What it shows                                            |
|----------------------|----------------------------------------------------------|
| **Coordination Summary** | Count of held / done / failed URLs across both laptops |
| **Workers**          | Each laptop's hostname, online/stale/offline status      |
| **Live Log Stream**  | Real-time SSE logs from both laptops, filterable         |
| **Diagnostics**      | Screenshots and HTML dumps uploaded by workers           |
| **Code Manifest**    | SHA-256 hashes of all deployable files on the server     |

### Log Filters:
- **Worker filter**: Show logs from one specific laptop
- **Level filter**: Show only WARNING/ERROR to spot problems
- **Auto-scroll**: Toggle to pause/resume following new entries

---

## Running Order Checklist

```
Step  What                        Where        Terminal
────  ──────────────────────────  ───────────  ────────
 1    Start coordination server   Laptop A     Terminal 1
 2    Verify dashboard loads      Any browser  —
 3    Start automation            Laptop A     Terminal 2
 4    Authenticate (if needed)    Laptop A     Terminal 2
 5    Start automation            Laptop B     Terminal 1
 6    Authenticate (if needed)    Laptop B     Terminal 1
 7    Monitor dashboard           Any browser  —
```

> **Always start the server (Step 1) before any automation.**
> The order of Laptop A vs Laptop B automation doesn't matter.

---

## Stopping & Resuming

### To stop gracefully:
- Press `Ctrl+C` in the automation terminal on either laptop
- The interactive prompt appears: choose `[q]` to quit or `[r]` to retry

### To resume later (same session):
```powershell
# Laptop A — start server WITHOUT --reset (preserves state)
python -m src.coordination_server

# Then start automation on both laptops
python main.py
```

### To start fresh:
```powershell
# Laptop A — start server WITH --reset
python -m src.coordination_server --reset
```

---

## Config Reference — What to Change on Each Laptop

| Setting                      | Laptop A (Server)          | Laptop B (Worker)                  |
|------------------------------|----------------------------|------------------------------------|
| `coordination_mode`          | `"http"`                   | `"http"`                           |
| `coordination_server_url`    | `"http://localhost:8099"`  | `"http://<LAPTOP-A-IP>:8099"`      |
| `auto_update`                | `false`                    | `true`                             |
| `collection_strategy`        | `"top_down"` or `"bottom_up"` | Opposite of Laptop A (recommended) |
| `headless`                   | Your choice                | Your choice                        |
| `email`                      | Same account               | Same account                       |
| All other settings           | Same                       | Same                               |

> **Tip:** Using opposite `collection_strategy` values (one `top_down`, one `bottom_up`)
> maximizes throughput since they start from different ends of the annotation list.

---

## Firewall Troubleshooting

If Laptop B can't reach the server on Laptop A:

### Option 1: Windows Firewall Rule (Recommended)

Run this **on Laptop A** as Administrator:

```powershell
New-NetFirewallRule -DisplayName "Roboflow Coordination Server" `
    -Direction Inbound -Protocol TCP -LocalPort 8099 `
    -Action Allow -Profile Private
```

### Option 2: Temporarily disable firewall (quick test)

```powershell
# On Laptop A (admin PowerShell) — TEMPORARY, re-enables on restart
Set-NetFirewallProfile -Profile Private -Enabled False
```

### Option 3: Use `ping` to verify connectivity

```powershell
# On Laptop B — ping Laptop A
ping 192.168.1.50

# On Laptop B — test the port directly
Test-NetConnection -ComputerName 192.168.1.50 -Port 8099
```

If `TcpTestSucceeded` is `True`, the connection works.

---

## Troubleshooting Common Issues

### "Connection refused" on Laptop B
- **Cause:** Server not running or wrong IP
- **Fix:** Ensure `python -m src.coordination_server` is running on Laptop A. Verify the IP with `ipconfig`.

### "Session expired" during automation
- **Cause:** Roboflow magic-link sessions expire after a few hours
- **Fix:** When prompted, choose `[r]` to retry. The automation will re-authenticate.

### Worker shows "offline" on dashboard
- **Cause:** Heartbeat not reaching the server
- **Fix:** Check network connectivity. `heartbeat_interval` default is 30s — wait up to 60s before it appears.

### Dashboard shows no logs
- **Cause:** `remote_logging: false` in config, or automation hasn't started yet
- **Fix:** Ensure `remote_logging: true` in `config.yaml` on both laptops.

### Code auto-update downloaded files but nothing changed
- **Cause:** Updated files take effect only after restart
- **Fix:** Stop and restart `python main.py` on Laptop B after code updates are pulled.

### Both laptops processing the same cards
- **Cause:** `enable_coordination: false` or using `coordination_mode: "file"` (local only)
- **Fix:** Both laptops must have `enable_coordination: true` and `coordination_mode: "http"`.

---

## Folder Structure After Setup

```
LAPTOP A                              LAPTOP B
────────                              ────────
automation-project/                   automation-project/
├── config.yaml        (server URL=localhost)    ├── config.yaml        (server URL=<IP>:8099)
├── main.py                           ├── main.py
├── requirements.txt                  ├── requirements.txt
├── coordination.json  (created by server)       ├── (no coordination.json)
├── session.json       (Laptop A auth)           ├── session.json       (Laptop B auth)
├── logs/              (local logs)              ├── logs/              (local logs)
│   ├── screenshots/                  │   ├── screenshots/
│   └── htmldumps/                    │   └── htmldumps/
├── src/                              ├── src/
│   ├── auth.py                       │   ├── auth.py
│   ├── batch_creator.py              │   ├── batch_creator.py
│   ├── coordination_server.py        │   ├── coordination_server.py
│   ├── coordinator.py                │   ├── coordinator.py
│   ├── dataset_mover.py              │   ├── dataset_mover.py
│   ├── navigator.py                  │   ├── navigator.py
│   ├── utils.py                      │   ├── utils.py
│   └── templates/                    │   └── templates/
│       └── dashboard.html            │       └── dashboard.html
├── venv/              (created locally)         ├── venv/              (created locally)
└── docs/                             └── docs/
    └── two-laptop-setup-guide.md         └── two-laptop-setup-guide.md
```

---

## Quick-Start Cheat Sheet

```
═══════════════════════════════════════════════════════════
  LAPTOP A (Server + Worker)
═══════════════════════════════════════════════════════════

  Terminal 1:  python -m src.coordination_server --reset
  Terminal 2:  python main.py
  Dashboard:   http://localhost:8099/dashboard

═══════════════════════════════════════════════════════════
  LAPTOP B (Worker only)
═══════════════════════════════════════════════════════════

  Edit config.yaml:
    coordination_server_url: "http://<LAPTOP-A-IP>:8099"
    auto_update: true

  Terminal 1:  python main.py
  Dashboard:   http://<LAPTOP-A-IP>:8099/dashboard

═══════════════════════════════════════════════════════════
```

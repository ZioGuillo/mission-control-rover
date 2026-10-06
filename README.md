# RoboControl

**[Leer en español →](README.es.md)**

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.100%2B-009688?style=flat&logo=fastapi&logoColor=white)
![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-3B%2B%20%7C%204%20%7C%205-C51A4A?style=flat&logo=raspberrypi&logoColor=white)
![Jetson Nano](https://img.shields.io/badge/Jetson-Nano-76B900?style=flat&logo=nvidia&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS-lightgrey?style=flat)
![License](https://img.shields.io/badge/license-MIT-green?style=flat)
![Hardware](https://img.shields.io/badge/hardware-RRB3-orange?style=flat)
![Tunnel](https://img.shields.io/badge/Cloudflare-Tunnel-F38020?style=flat&logo=cloudflare&logoColor=white)

RoboControl turns a Raspberry Pi (or a Jetson Nano) and a RaspiRobot Board V3 into a small robot car you drive from a web browser. Point your phone at the Pi's IP address and you get a live camera feed, a D-pad, pan/tilt controls, and basic telemetry — no app to install, no cables, nothing to solder beyond the motor board itself.

Under the hood it's a FastAPI app with a small hardware abstraction layer, so the same codebase runs the real thing on a Pi or Jetson and a fully simulated version in Docker when you just want to poke at the dashboard. It's a one-person hobby project, not a commercial product, and the README below tries to reflect that: what it actually does, what it doesn't do yet, and what you'd need to check yourself before trusting it with real hardware.

---

## Gallery

| Front | Top | Angle |
| :---: | :---: | :---: |
| ![Front](images/robot_front.jpg) | ![Top](images/robot_top.jpg) | ![Angle](images/robot_angle.jpg) |

---

## In plain language

If you don't write code, here's what this actually is: a small robot car — motors, wheels, a camera, a distance sensor — that you drive from a web page, like you'd use any app. No special software to install. You open a browser, you see what the car sees, and you drive it.

What makes it worth a second look isn't the robot itself — plenty of hobby kits do that. It's *why* it's built the way it is. A NASA rover on Mars can't be joysticked in real time: a radio signal takes between 3 and 22 minutes to cross the distance from Earth to Mars, one way, no matter how good your hardware is — that's the speed of light, not an engineering limitation. So the rover gets one batch of commands a day, executes them mostly on its own, and has to make its own calls about what's a hazard and what isn't. This robot, on the same WiFi network as the phone driving it, has a round-trip of well under a second — but it still has to answer a smaller version of the same question every real remote system faces: *what does it do when it can't reach you?* If the sonar sees an obstacle, it stops itself before a human can react. If it can't find a known WiFi network, it opens its own and asks for help instead of going dark. Different scale, same underlying idea.

You don't need a NASA budget to start thinking this way — you need about $100 in parts and a free weekend. That's really the point of this project: the ideas behind serious remote systems are learnable, not exclusive, and a Raspberry Pi is a perfectly good place to start practicing them.

If you want to see it running before reading any further, jump to [Section 5 (Using the Controller)](#5-using-the-controller) for screenshots, or [`docker/README.md`](docker/README.md) to try the dashboard yourself with no hardware at all.

---

## Table of Contents

1. [What You Need](#1-what-you-need)
2. [Install](#2-install)
3. [WiFi Setup](#3-wifi-setup)
4. [First Login](#4-first-login)
5. [Using the Controller](#5-using-the-controller)
6. [Internet Access — Optional](#6-internet-access--optional)
7. [Day-to-day Commands](#7-day-to-day-commands)
8. [Configuration Reference](#8-configuration-reference)
9. [API Reference](#9-api-reference)
10. [Security](#10-security)
11. [Monitoring](#11-monitoring)
12. [Developer Guide](#12-developer-guide)

---

## 1. What You Need

### Required

| Part | Details |
| ---- | ------- |
| Raspberry Pi 3B+ / 4 / 5, or Jetson Nano | Any RAM size. GPIO/camera backend is auto-detected — see [Supported Hardware](#supported-hardware) |
| RaspiRobot Board V3 (RRB3) | Motor driver HAT — plugs directly onto the GPIO header |
| HC-SR04 ultrasonic sensor | Plugs into the RRB3 sonar header |
| 2× DC gear motors + chassis | Any TT-motor compatible pair |
| 7.4V LiPo battery (2S) | Powers the motors via the RRB3 |
| MicroSD card (16 GB+) | For Raspberry Pi OS |

### Optional

| Part | What it adds |
| ---- | ------------ |
| Pi Camera Module (CSI) or USB webcam | Live video stream while driving |
| 2× SG90 servos + pan/tilt bracket | Aim the camera remotely |
| Passive buzzer | Audio alerts on obstacles |

### Wiring at a glance

```text
Raspberry Pi GPIO header
        │
  RaspiRobot Board V3
  ├── Motor A terminals  → Left motor
  ├── Motor B terminals  → Right motor
  ├── Sonar header       → HC-SR04
  └── Power input        → 7.4V LiPo

GPIO 12 → Pan servo
GPIO 13 → Tilt servo
GPIO 18 → Buzzer
CSI     → Pi Camera ribbon   (if using)
USB     → USB webcam         (if using)
```

> Power the Pi from its own micro-USB supply. Sharing power with the motors causes reboots.

### Supported Hardware

`app/hardware/` auto-detects your board at runtime — no config flag to set, the same install works unmodified across boards:

| Board | GPIO backend | Camera backend |
| ----- | ------------ | --------------- |
| Raspberry Pi 3B+ / 4 | `RPi.GPIO` | `picamera2` (CSI) |
| Raspberry Pi 5 | `rpi-lgpio` (drop-in `RPi.GPIO` replacement for the RP1 chip) | `picamera2` (CSI) |
| Jetson Nano / other Jetson | `Jetson.GPIO` | OpenCV (`opencv-python-headless`) — USB webcam or V4L2 |
| Anything else (USB webcam, no GPIO) | — (drives disable themselves) | OpenCV |

`scripts/install.sh` reads `/proc/device-tree/model` and installs the matching GPIO package automatically; `app/hardware/gpio_compat.py` then picks up whichever backend is importable. `app/hardware/camera_driver.py` tries `picamera2` first and falls back to OpenCV.

**Moving the RRB3 board to a Jetson Nano:** the motor driver in `app/hardware/rrb3_driver.py` talks to the RRB3 over the same BCM pin numbers it uses on a Raspberry Pi, via whichever GPIO backend was detected — no code changes needed. What I can't verify for you is the physical wiring: Jetson Nano's 40-pin header matches a Raspberry Pi's layout closely enough for `Jetson.GPIO`'s BCM-compatible mode to work for most digital I/O, but **check your specific pins against a Jetson pinout diagram with a multimeter before powering the board** — a wrong assumption here is the kind of mistake that damages hardware, not just software.

**WiFi setup** ([Section 3](#3-wifi-setup)) depends on NetworkManager being the active network manager. That's the Raspberry Pi OS Bookworm default, matching what this whole hardware layer assumes; JetPack's Ubuntu-based images typically ship it too, but I can't confirm that for your specific Jetson image — check with `systemctl status NetworkManager` before relying on it.

**Python version on an original Jetson Nano:** this project requires Python 3.10+ (see `pyproject.toml`). JetPack 6 (Ubuntu 22.04 — Jetson Orin Nano/NX) ships Python 3.10 as `python3` by default, so there's nothing to do. The **original Jetson Nano's** last supported release, **JetPack 4.6 (Ubuntu 18.04), ships Python 3.6** — too old for this project's dependencies (numpy, pydantic-settings, etc. all require 3.8+/3.9+). `scripts/install.sh` now detects this and refuses to proceed with a clear error instead of silently building a broken venv. To fix it: install a newer Python **alongside** the system one — don't repoint `/usr/bin/python3`, since `apt` and other system tools on Ubuntu 18.04 depend on it staying 3.6:

```bash
curl https://pyenv.run | bash
# add pyenv's init lines to ~/.bashrc per its own install output, then:
sudo apt install -y build-essential libssl-dev zlib1g-dev libbz2-dev \
  libreadline-dev libsqlite3-dev libffi-dev liblzma-dev
pyenv install 3.10.14   # compiles on-device — budget 45-90 min on a Nano, consider adding swap first
```

Re-run `scripts/install.sh` afterward — it picks up the pyenv-installed interpreter automatically.

**Testing without any hardware at all:** see [`docker/README.md`](docker/README.md) — `docker compose -f docker/docker-compose.yml up --build` runs the full dashboard with simulated motors/sonar/camera so you can try changes before touching a real board.

---

## 2. Install

### Step 1 — Flash the SD card

Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/) and flash **Raspberry Pi OS Lite (64-bit, Bookworm)**. In the imager's advanced settings, enable SSH and set a username/password before writing.

### Step 2 — Boot and connect

Insert the card, power on the Pi, and find its IP address from your router's device list. Then open a terminal:

```bash
ssh pi@<pi-ip-address>
```

### Step 3 — Clone the repo

```bash
git clone https://github.com/ZioGuillo/robocar.git ~/robocontrol
cd ~/robocontrol
```

### Step 4 — Create your config file

```bash
cp .env.example .env
```

Generate a secret key and paste it into `.env`:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Open `.env` with `nano .env` and set:

```ini
SESSION_SECRET_KEY=<paste the key here>
```

Save and close (`Ctrl+O`, `Enter`, `Ctrl+X`).

### Step 5 — Run the install script

```bash
bash scripts/install.sh
```

This will:

- Create the Python virtual environment and install all dependencies
- Detect your board (`/proc/device-tree/model`) and install the matching GPIO backend — see [Supported Hardware](#supported-hardware)
- Install the camera driver (`picamera2` on a Pi; otherwise nothing — install the OpenCV fallback yourself if this board has a non-CSI camera)
- Register and start the `robocontrol` systemd service (auto-starts on every boot)

At the end you will see something like:

```text
  Local URL  : http://192.168.x.x:8000
  Public URL : not configured (local-only mode)
```

### Step 6 — Open the controller

On any device connected to the same WiFi, open a browser and go to:

```text
http://<pi-ip-address>:8000
```

---

## 3. WiFi Setup

Step 1 already got you online once, using the WiFi credentials you set in Raspberry Pi Imager's advanced settings. This section is for everything after that: moving the rover to a different network, setting up a second one somewhere you don't have Imager access, or connecting it without a screen or SD card reader at all.

### Option A — `wifi.txt` on the boot partition

The deterministic way. Before boot, place a `wifi.txt` file on the boot partition (`/boot/firmware/` on Bookworm):

```ini
SSID=NetworkName
PASSWORD=NetworkPassword
```

`wifi-provision.service` reads it on boot and deletes the file once connected, so the password doesn't sit on the SD card indefinitely. To switch networks later, drop a new `wifi.txt` on the boot partition and reboot.

### Option B — the captive portal (no SD card reader needed)

If the device has no working network after a short wait on boot, `wifi-connect-fallback.service` opens its own WiFi network — default SSID `RoboCar-Setup`, no passphrase — with a captive portal:

1. Connect your phone or laptop to `RoboCar-Setup`.
2. Your device should open the setup page on its own (captive portal detection); if it doesn't, open any URL and it'll redirect there.
3. Pick the real network from the scanned list and enter its password.

The portal shuts down as soon as it connects, and `robocontrol` becomes reachable at its usual URL on the new network. Set `PORTAL_SSID` / `PORTAL_PASSPHRASE` in `.env` if you want the setup network renamed or passphrase-protected — worth doing if you're setting up somewhere the real WiFi password shouldn't cross an open network, even briefly.

This uses [WiFi Connect](https://github.com/balena-os/wifi-connect) (Apache-2.0), installed automatically by `scripts/install.sh`. It only ever *offers* to help: if `wifi.txt` or a previously-saved network already connects, the portal never appears. It also isn't available until `scripts/install.sh` has run once — it can't help with the very first boot, only every one after.

---

## 4. First Login

Default credentials:

| Username | Password |
| -------- | -------- |
| `admin` | `admin` |

**Change the password immediately** — go to the **Settings tab → Change password**.

---

## 5. Using the Controller

### Drive tab

![Drive tab](images/ui_dash.png)

| Control | What it does |
| ------- | ------------ |
| Camera preview | Live stream while you drive |
| D-pad | Forward / reverse / turn left / turn right / stop (red centre) |
| Speed slider | Motor power 0–100% (default 75%) |

When the sonar detects an obstacle closer than 20 cm, the car stops automatically and the UI shows a warning.

### Camera tab

![Camera tab](images/ui_camera.png)

Full-size live stream. The 3×3 grid below it nudges the pan/tilt servos; the centre button re-centres both.

Detection order: **CSI camera → USB webcam → `CAMERA_STREAM_URL` → placeholder image**.

### Missions tab

![Missions tab](images/ui_missions.png)

| Mission | Description |
| ------- | ----------- |
| Path Following | Drive a pre-defined waypoint route automatically |
| Visual Search | Roam until the camera finds a target image, then alert |
| Auto-Drive | Free-roam with sonar-based obstacle avoidance |

All missions are currently **coming soon**.

### Telemetry tab

![Telemetry tab](images/ui_telemetry.png)

Live stats for the current session (resets on restart):

| Metric | Description |
| ------ | ----------- |
| Battery | Whether the motor driver board is detected (the RRB3 has no voltage-sensing circuit, so this isn't a live voltage reading) |
| Uptime | Time since the server started |
| Last CMD Latency | Round-trip time of the last motor command |
| Commands Sent | Total motor commands this session |
| Obstacles Hit | Times the sonar triggered an automatic stop |
| Est. Distance | Approximate distance driven |

### Settings tab *(admin only)*

- **User management** — approve or revoke GitHub OAuth users
- **GitHub OAuth** — paste your GitHub app credentials to enable social login
- **Change password** — update the admin password

---

## 6. Internet Access — Optional

By default the rover is only reachable on your local network. This section is for when you want to control it **from anywhere in the world** using a permanent public URL like `https://rover01.yourdomain.com`.

> Skip this entirely if local control is enough — the rover works fine without it.

### What you need

- A free [Cloudflare account](https://cloudflare.com)
- A domain added to that account (works with any registrar)

### How it works

The install script runs `cloudflared_provision.py` which:

1. Reads your Cloudflare credentials
2. Auto-detects your domain (or you can specify one)
3. Creates a tunnel and assigns the next free `roverXX` subdomain
4. Adds the DNS record automatically
5. Saves everything — the tunnel starts on every boot from then on

### Setup

**On your computer** — create a `cloudflared.env` file:

```ini
CF_API_TOKEN=your_cloudflare_api_token
CF_ACCOUNT_ID=your_cloudflare_account_id

# Optional — auto-detected if omitted
# CF_ZONE_ID=your_zone_id
# CF_ZONE_NAME=mydomain.com
```

| Variable | Required | Where to find it |
| -------- | :------: | ---------------- |
| `CF_API_TOKEN` | Yes | Dashboard → My Profile → API Tokens → Create Token. Permissions: **Tunnel:Edit + DNS:Edit + Zone:Read** |
| `CF_ACCOUNT_ID` | Yes | Dashboard → any domain page → right sidebar |
| `CF_ZONE_ID` | No | Dashboard → your domain → right sidebar. Auto-detected if omitted. |
| `CF_ZONE_NAME` | No | e.g. `mydomain.com`. Picks a specific domain when your account has more than one. |

**Recommended — encrypt the file before putting it on the SD card:**

```bash
# On your Mac / Linux machine:
bash scripts/encrypt_env.sh cloudflared.env
# → produces cloudflared.env.enc  (safe to share)
```

Copy `cloudflared.env.enc` to the SD card boot partition (`/boot/firmware/`). Then add the passphrase to `~/robocontrol/.env` on the Pi:

```ini
CLOUDFLARED_ENV_PASSPHRASE=<the passphrase you chose>
```

If you prefer plain text (trusted environment only), place `cloudflared.env` directly on the boot partition and skip the passphrase step.

**Run provisioning:**

```bash
python3 scripts/cloudflared_provision.py
```

On success:

```text
============================================================
  rover01 is LIVE
============================================================

  Public URL : https://rover01.yourdomain.com
  Local URL  : http://192.168.x.x:8000
```

### Rover numbering

The script queries your Cloudflare account for existing `roverXX` tunnels and picks the first free slot. Rover01 already set up? The next one becomes rover02 automatically.

### Service startup order

WiFi connects before any of this runs — see [Section 3, WiFi Setup](#3-wifi-setup) if the rover isn't on your network yet. Once it is, the rest of the boot chain looks like this:

```text
wifi-provision             ← connects WiFi from wifi.txt (skipped if absent)
        ↓
wifi-connect-fallback       ← no connection after ~20s? opens the setup portal (skipped once connected)
        ↓
cloudflared-provision      ← provisions tunnel (skipped if no credentials or already done)
        ↓
cloudflared                ← tunnel is live
        ↓
robocontrol                ← app is live at :8000 regardless of tunnel
```

### Checking status

```bash
journalctl -u wifi-provision -n 20          # WiFi provisioning log
journalctl -u wifi-connect-fallback -n 20   # Captive-portal WiFi setup log
journalctl -u cloudflared-provision -n 20   # Tunnel provisioning log
sudo systemctl status cloudflared           # Tunnel runtime status
journalctl -u robocontrol -f                # App live logs
```

### Re-provisioning (fresh start)

```bash
rm ~/.cloudflared/config.yml ~/.cloudflared/rover_id ~/.cloudflared/rover_url
sudo systemctl restart cloudflared-provision
```

### GitHub OAuth

To let team members log in with their GitHub accounts:

1. Go to **GitHub → Settings → Developer Settings → OAuth Apps → New OAuth App**
2. Set the callback URL to `https://rover01.yourdomain.com/auth/callback`
3. Copy the **Client ID** and generate a **Client Secret**
4. In RoboControl: **Settings tab → GitHub OAuth** — paste and enable
5. Set `BASE_URL=https://rover01.yourdomain.com` in `.env` so redirects match

New GitHub users land in `pending` until you approve them in the Settings tab.

---

## 7. Day-to-day Commands

### On the Pi

```bash
sudo systemctl status robocontrol    # is it running?
sudo systemctl restart robocontrol   # restart after a config change
journalctl -u robocontrol -f         # live logs
journalctl -u robocontrol -n 50      # last 50 lines
```

### From your computer (Makefile)

```bash
make help                            # list all commands
make deploy                          # push latest code to Pi and restart
make restart                         # restart without deploying
make logs                            # stream live logs
make status                          # check if running
make open                            # open the web UI in your browser
make connect                         # SSH into the Pi
make set-password ADMIN_PASS=NewPass # change admin password
```

Override the Pi address for one command:

```bash
make deploy PI_HOST=pi@192.168.1.50
```

---

## 8. Configuration Reference

All settings live in `~/robocontrol/.env`:

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `SESSION_SECRET_KEY` | *(auto-generated)* | Signs session cookies — 32+ random chars. Setting it explicitly is still recommended; if omitted, a random key is generated once and persisted to `data_dir/session_secret_key` |
| `PORT` | `8000` | HTTP port |
| `BASE_URL` | *(empty)* | Public URL when behind Cloudflare or a proxy — required for GitHub OAuth |
| `CAMERA_STREAM_URL` | *(empty)* | Fallback MJPEG URL if no local camera is detected |
| `MOTOR_SPEED_DEFAULT` | `0.75` | Default motor power (0.0–1.0) |
| `OBSTACLE_THRESHOLD_CM` | `20` | Sonar distance (cm) that triggers auto-stop |
| `PAN_SERVO_PIN` | `12` | GPIO pin for pan servo |
| `TILT_SERVO_PIN` | `13` | GPIO pin for tilt servo |
| `BUZZER_PIN` | `18` | GPIO pin for buzzer |
| `MOTOR_RATE_LIMIT` | `20` | Max motor commands/second per IP (0 = unlimited) |
| `CLOUDFLARED_ENV_PASSPHRASE` | *(empty)* | Decrypts `cloudflared.env.enc` on the boot partition |
| `CF_API_TOKEN` | *(empty)* | Cloudflare API token — leave blank for local-only |
| `CF_ACCOUNT_ID` | *(empty)* | Cloudflare Account ID |
| `CF_ZONE_ID` | *(empty)* | Zone ID — auto-detected if omitted |
| `CF_ZONE_NAME` | *(empty)* | Domain name — selects a zone when account has multiple |

---

## 9. API Reference

| Method | Path | Auth | Description |
| ------ | ---- | :--: | ----------- |
| `GET` | `/api/ping` | — | Liveness check — returns `{"ok": true}` |
| `GET` | `/api/status` | Yes | Hardware availability (motors, camera, servos) |
| `GET` | `/api/camera/stream` | Yes | Live MJPEG stream |
| `POST` | `/api/camera/{action}` | Yes | Pan/tilt: `up` `down` `left` `right` `center` |
| `POST` | `/api/motors/{action}` | Yes | `forward` `reverse` `left` `right` `stop` |
| `POST` | `/api/motors/auto` | Yes | Not implemented (501) |
| `POST` | `/api/missions/path` | Yes | Not implemented (501) |
| `POST` | `/api/missions/search` | Yes | Not implemented (501) |

Motor commands accept an optional body: `{"speed": 0.75}` (0.0–1.0).

---

## 10. Security

This isn't an audited product, it's a hobby project that happens to control a physical thing over the internet, so it gets treated more carefully than a typical side project. Here's what's actually in place, plainly:

**Passwords and sessions.** Local passwords are hashed with PBKDF2-HMAC-SHA256 (260,000 iterations, random salt per user) — not bcrypt, in case you go looking for it in the code. A session is a random 32-byte token stored in SQLite, referenced by a signed cookie (`itsdangerous`); the cookie can't be forged without the server's secret key, and that key is either the one you set in `.env` or, if you didn't set one, a random value generated on first run and saved to disk rather than falling back to a value baked into the source. Sessions expire after 7 days.

**Roles.** Users are `admin`, `approved`, `pending`, or `revoked`. Revoking someone kills their active session immediately — that wasn't always true (a role change used to leave existing sessions valid until they expired on their own), and it's the kind of bug that matters more here than in most apps, since a revoked user could otherwise still be driving the car.

**GitHub OAuth**, if you turn it on, uses a signed `state` parameter to stop the login-CSRF trick where someone else's OAuth callback gets completed in your browser. New GitHub logins land in `pending` until an admin approves them.

**Rate limiting** applies to login attempts (5/second per IP) and to motor commands (configurable, default 20/second per IP) — mostly to keep a stuck client or runaway script from hammering the motors, not as a defense against a serious attacker.

**Everything else that's just good hygiene**: all SQL is parameterized (nothing string-built), API request bodies are validated with Pydantic, secrets live in `.env` and are never committed, and the app only talks HTTPS at all if you put it behind the Cloudflare Tunnel — on your local network it's plain HTTP, same as most home IoT devices.

What this doesn't claim to be: independently audited, resistant to a determined attacker with local network access, or built against a specific compliance framework. If you find something wrong, open an issue.

---

## 11. Monitoring

RoboControl exports a Prometheus-compatible `/metrics` endpoint that any Prometheus server can scrape.

```text
GET http://<pi-ip>:8000/metrics
```

The path requires no authentication so Prometheus can scrape without a session token.
Metrics are refreshed every **5 seconds** by the background telemetry thread.

### Scrape config

```yaml
scrape_configs:
  - job_name: robocar
    static_configs:
      - targets: ['<pi-ip>:8000']
    metrics_path: /metrics
    scrape_interval: 5s
```

### Metrics reference

| Category | Metric | Type | Description |
| -------- | ------ | ---- | ----------- |
| **System** | `robocar_cpu_percent` | Gauge | CPU usage (0–100) |
| | `robocar_cpu_temperature_celsius` | Gauge | SoC temperature in °C (Pi only — 0 on other hw) |
| | `robocar_ram_used_bytes` | Gauge | RAM used in bytes |
| | `robocar_ram_total_bytes` | Gauge | Total RAM in bytes |
| | `robocar_disk_used_bytes` | Gauge | Disk used bytes (/) |
| | `robocar_disk_total_bytes` | Gauge | Disk total bytes (/) |
| | `robocar_uptime_seconds` | Gauge | Server uptime in seconds |
| **Hardware** | `robocar_hardware_available{component}` | Gauge | Component status — 1=up, 0=down; labels: `motors`, `camera`, `servo` |
| **Motors** | `robocar_motor_commands_total{action}` | Counter | Motor commands dispatched; labels: `forward`, `reverse`, `left`, `right`, `stop` |
| | `robocar_motor_blocked_total` | Counter | Forward commands blocked by obstacle detection |
| | `robocar_estimated_distance_meters` | Gauge | Estimated distance driven this session in meters |
| **Sensor** | `robocar_sonar_distance_cm` | Gauge | Last ultrasonic reading in cm (0 = no reading yet) |
| | `robocar_obstacles_total` | Counter | Obstacle detections triggering auto-stop |
| **Camera** | `robocar_camera_frames_total` | Counter | MJPEG frames captured by the camera driver |
| **HTTP** | `robocar_http_requests_total{method,path,status}` | Counter | HTTP requests handled |
| | `robocar_http_request_duration_seconds{path}` | Histogram | Request latency; buckets 5 ms → 2.5 s |
| **Auth** | `robocar_login_attempts_total{result}` | Counter | Login attempts; labels: `success`, `failure` |
| | `robocar_rate_limit_hits_total{endpoint}` | Counter | Rate limit rejections; labels: `login`, `motors` |
| | `robocar_active_sessions_total` | Gauge | Currently active (non-expired) user sessions |
| **Build** | `robocar_build_info` | Info | Static labels: `version="2.0"`, `hardware="rpi4"` |

### Grafana PromQL examples

```promql
# CPU usage live
robocar_cpu_percent

# HTTP request rate (last 5 min)
rate(robocar_http_requests_total[5m])

# P95 request latency
histogram_quantile(0.95, rate(robocar_http_request_duration_seconds_bucket[5m]))

# Motor command breakdown
rate(robocar_motor_commands_total[5m])

# Obstacle detection rate
rate(robocar_obstacles_total[5m])

# Active sessions
robocar_active_sessions_total
```

All metric definitions live in `app/metrics.py`. The `/metrics` route is registered in `app/main.py`.

---

## 12. Developer Guide

### Enabling the Pi Camera (CSI)

```bash
sudo apt install -y python3-picamera2
sudo raspi-config nonint do_camera 0
sudo reboot
```

USB webcams work out of the box — just plug in and restart the service.

### Adding a new route

The app uses **route auto-discovery** — drop a new file in `app/routes/` and it's live on restart, no changes to `main.py` needed.

```bash
cp app/routes/_template.py app/routes/lights.py
# edit lights.py: set prefix and add endpoints
sudo systemctl restart robocontrol
```

```text
app/routes/
├── _template.py    ← start here
├── camera.py
├── missions.py
├── motors.py
├── settings.py
├── status.py
└── telemetry.py
```

### Hardware helpers

```python
from app.hardware import rrb3_driver    # set_motors(), get_distance(), available
from app.hardware import servo_driver   # move(), center(), available
from app.hardware import camera_driver  # get_frame(), start(), stop(), available
from app.config import settings         # all .env values
from app import telemetry               # record_command()
```

### Running tests

No hardware required — drivers degrade gracefully when GPIO/camera backends are unavailable:

```bash
pytest -v
```

### Swapping hardware

Only `app/hardware/` is hardware-specific. Everything else is agnostic. Raspberry Pi 3/4/5 and Jetson Nano are supported out of the box — see [Supported Hardware](#supported-hardware).

**Other boards** (not auto-detected — add a case to `app/hardware/gpio_compat.py`):

| Board | Change |
| ----- | ------ |
| Orange Pi / Banana Pi | Use `OPi.GPIO` or `wiringOP` |
| BeagleBone Black | Use `Adafruit_BBIO` |

**Alternative motor drivers:**

| Driver | Notes |
| ------ | ----- |
| L298N | Direct `RPi.GPIO` PWM — no extra library |
| Adafruit Motor HAT | `adafruit-circuitpython-motorkit` |
| Cytron MDD3A / MDD10A | PWM + direction pins, same pattern as L298N |

**Sensors and extras:**

| Component | Library |
| --------- | ------- |
| Encoder wheels | `RPi.GPIO` interrupt — accurate odometry |
| IMU (MPU-6050) | `mpu6050-raspberrypi` |
| LIDAR (RPLidar A1) | `rplidar-roboticia` — 2D mapping |
| NeoPixel LEDs | `rpi_ws281x` |
| GPS module | `gpsd` + `gps3` |

### Future implementations

- Auto-drive — obstacle avoidance free-roam
- Path following — execute a waypoint route
- Visual search — roam until the camera matches a target image
- Buzzer feedback — audio confirmation for commands
- Light control — toggle RRB3 onboard LEDs
- Battery monitoring — live voltage display

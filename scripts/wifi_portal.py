#!/usr/bin/env python3
"""
wifi_portal.py

Captive-portal WiFi setup for boards balena's wifi-connect has no build for
(the Pi Zero W is ARMv6). Run as root by wifi-connect-wrapper.sh; standard
library only, so it runs on the system python3 without the app's venv.

  1. scans the networks around (before the radio becomes an access point:
     one radio can't reliably scan while it is one)
  2. opens the open network PORTAL_SSID ("RoboCar-Setup") through
     NetworkManager's shared mode, whose dnsmasq hands out addresses; a
     dnsmasq-shared.d file points every DNS name at the rover, so phones pop
     up the "sign in to network" page by themselves
  3. serves one page on port 80: pick a network (or type a hidden one), give
     its password, and the rover joins it and keeps it as a saved network
  4. if joining fails, the portal comes back with the error

Exits 0 once connected, or after PORTAL_IDLE_SECONDS without a single HTTP
request (phones on the portal keep polling, so that means nobody is there):
the watchdog then retries the saved networks and reopens the portal if they
are still out of reach.
"""
from __future__ import annotations

import html
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTAL_SSID = os.environ.get("PORTAL_SSID") or "RoboCar-Setup"
PORTAL_CON = "robocar-portal"
GATEWAY = "10.42.0.1"  # NetworkManager's default for ipv4.method=shared
DNS_CONF = "/etc/NetworkManager/dnsmasq-shared.d/robocar-portal.conf"
IDLE_SECONDS = int(os.environ.get("PORTAL_IDLE_SECONDS") or 600)
CONNECT_TIMEOUT = 45


def log(msg: str) -> None:
    print(f"[wifi-portal] {msg}", flush=True)


def nmcli(*args: str, check: bool = True, timeout: float = 60) -> str:
    r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"nmcli {args[0]} failed")
    return r.stdout


def split_terse(line: str) -> list[str]:
    """One `nmcli -t` line: fields split on ':', where '\\:' and '\\\\' are literal."""
    fields, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    fields.append("".join(cur))
    return fields


def parse_networks(listing: str) -> list[dict]:
    """`nmcli -t -f SSID,SIGNAL,SECURITY dev wifi list` → one entry per SSID
    (its strongest access point), strongest first; hidden SSIDs dropped."""
    best: dict[str, dict] = {}
    for line in listing.splitlines():
        parts = split_terse(line)
        if len(parts) < 3 or not parts[0]:
            continue
        ssid, security = parts[0], parts[2].strip()
        try:
            signal_ = int(parts[1])
        except ValueError:
            signal_ = 0
        if ssid == PORTAL_SSID:
            continue
        if ssid not in best or signal_ > best[ssid]["signal"]:
            best[ssid] = {"ssid": ssid, "signal": signal_,
                          "secure": security not in ("", "--")}
    return sorted(best.values(), key=lambda n: -n["signal"])


def parse_form(body: str) -> tuple[str, str]:
    """The submitted SSID (typed one wins over the picked one) and password."""
    form = urllib.parse.parse_qs(body, keep_blank_values=True)
    typed = form.get("other", [""])[0].strip()
    picked = form.get("ssid", [""])[0]
    return (typed or picked), form.get("password", [""])[0]


def bars(signal_: int) -> str:
    n = 1 + (signal_ >= 35) + (signal_ >= 55) + (signal_ >= 75)
    return "▂▄▆█"[:n]


def render_page(networks: list[dict], message: str = "") -> str:
    rows = "".join(
        f'<label class="net"><input type="radio" name="ssid" value="{html.escape(n["ssid"])}"'
        f'{" checked" if i == 0 else ""}><span class="name">{html.escape(n["ssid"])}</span>'
        f'<span class="sig">{"🔒 " if n["secure"] else ""}{bars(n["signal"])}</span></label>'
        for i, n in enumerate(networks)
    ) or '<p class="muted">No encontré redes. Escribe el nombre abajo.</p>'
    note = f'<p class="msg">{html.escape(message)}</p>' if message else ""
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>RoboCar · Wi-Fi</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;background:#1E1E2E;color:#fff;margin:0;padding:20px}}
main{{max-width:420px;margin:0 auto}} h1{{font-size:1.3rem;margin:0 0 4px}}
.muted{{color:#B3ABCC;font-size:.9rem}} .msg{{background:#4B2575;padding:10px 12px;border-radius:8px}}
.net{{display:flex;align-items:center;gap:10px;padding:12px;margin:6px 0;background:#2A2A40;border-radius:8px}}
.name{{flex:1;overflow-wrap:anywhere}} .sig{{color:#7AD6DF;white-space:nowrap}}
input[type=text],input[type=password]{{width:100%;box-sizing:border-box;padding:12px;margin:6px 0 12px;
border-radius:8px;border:1px solid #4B2575;background:#14141F;color:#fff;font-size:1rem}}
button{{width:100%;padding:14px;border:0;border-radius:8px;background:#3ABCC8;color:#1E1E2E;font-size:1rem;font-weight:600}}
</style></head><body><main>
<h1>Conectar el robot a Wi-Fi</h1><p class="muted">Elige la red y escribe su contraseña.</p>{note}
<form method="post" action="/connect">{rows}
<p class="muted">¿No aparece? Escribe su nombre:</p><input type="text" name="other" autocomplete="off">
<input type="password" name="password" placeholder="Contraseña (vacía si es abierta)">
<button type="submit">Conectar</button></form></main></body></html>"""


def render_connecting(ssid: str) -> str:
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>RoboCar · Wi-Fi</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;background:#1E1E2E;color:#fff;padding:20px}}</style>
</head><body><h1>Conectando a «{html.escape(ssid)}»…</h1>
<p>Si funciona, la red {html.escape(PORTAL_SSID)} va a desaparecer en un momento: vuelve a tu Wi-Fi normal.</p>
<p>Si no, {html.escape(PORTAL_SSID)} vuelve a aparecer en un minuto con el error; conéctate otra vez.</p>
</body></html>"""


# ── NetworkManager side ─────────────────────────────────────────────────
def wifi_device() -> str:
    for line in nmcli("-t", "-f", "DEVICE,TYPE", "device").splitlines():
        dev, typ = (split_terse(line) + ["", ""])[:2]
        if typ == "wifi":
            return dev
    raise RuntimeError("no WiFi device")


def scan(dev: str) -> list[dict]:
    try:
        nmcli("device", "wifi", "rescan", "ifname", dev, check=False, timeout=20)
        time.sleep(4)
        return parse_networks(nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi",
                                    "list", "ifname", dev, timeout=30))
    except Exception as exc:
        log(f"scan failed: {exc}")
        return []


def start_hotspot(dev: str) -> None:
    os.makedirs(os.path.dirname(DNS_CONF), exist_ok=True)
    with open(DNS_CONF, "w") as f:
        f.write(f"# robocar wifi_portal: every name → the portal (captive-portal popup)\n"
                f"address=/#/{GATEWAY}\n")
    stop_hotspot()
    nmcli("connection", "add", "type", "wifi", "ifname", dev, "con-name", PORTAL_CON,
          "autoconnect", "no", "ssid", PORTAL_SSID, "802-11-wireless.mode", "ap",
          "802-11-wireless.band", "bg", "ipv4.method", "shared",
          "ipv4.addresses", f"{GATEWAY}/24", "ipv6.method", "disabled")
    nmcli("connection", "up", PORTAL_CON, timeout=60)
    log(f"portal up: open network '{PORTAL_SSID}', page at http://{GATEWAY}/")


def stop_hotspot() -> None:
    nmcli("connection", "down", PORTAL_CON, check=False)
    nmcli("connection", "delete", PORTAL_CON, check=False)


def remove_dns_conf() -> None:
    try:
        os.remove(DNS_CONF)
    except FileNotFoundError:
        pass


def saved_connections_for(ssid: str) -> list[str]:
    """Saved profiles for `ssid`, preferred (highest autoconnect priority) first —
    e.g. Nibiru pinned to one access point, then Nibiru-cualquiera."""
    found = []
    for line in nmcli("-t", "-f", "NAME,TYPE,AUTOCONNECT-PRIORITY", "connection", "show").splitlines():
        name, typ, prio = (split_terse(line) + ["", "", ""])[:3]
        if typ != "802-11-wireless" or name == PORTAL_CON:
            continue
        if nmcli("-g", "802-11-wireless.ssid", "connection", "show", name, check=False).strip() == ssid:
            found.append((-int(prio or 0), name))
    return [name for _, name in sorted(found)]


def join(dev: str, ssid: str, password: str) -> None:
    """Join `ssid` and keep it saved. Raises RuntimeError with NetworkManager's reason."""
    saved = saved_connections_for(ssid)
    if saved:
        error: Exception | None = None
        for name in saved:
            try:
                if password:
                    nmcli("connection", "modify", name, "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password)
                nmcli("connection", "up", name, timeout=CONNECT_TIMEOUT)
                return
            except Exception as exc:
                error = exc
        raise error
    args = ["device", "wifi", "connect", ssid, "ifname", dev]
    if password:
        args += ["password", password]
    try:
        nmcli(*args, timeout=CONNECT_TIMEOUT)
    except Exception:
        for stale in saved_connections_for(ssid):  # a failed connect can leave its new profile behind
            nmcli("connection", "delete", stale, check=False)
        raise
    for name in saved_connections_for(ssid):  # power save off, like the rover's other profiles
        nmcli("connection", "modify", name, "802-11-wireless.powersave", "2", check=False)


# ── the portal ──────────────────────────────────────────────────────────
class Portal:
    def __init__(self) -> None:
        self.dev = wifi_device()
        self.networks: list[dict] = []
        self.message = ""
        self.last_request = time.monotonic()
        self.done = threading.Event()
        self.lock = threading.Lock()

    def open(self) -> None:
        self.networks = scan(self.dev)
        start_hotspot(self.dev)
        self.last_request = time.monotonic()

    def try_join(self, ssid: str, password: str) -> None:
        with self.lock:
            time.sleep(2)  # let the "Conectando…" page reach the phone first
            stop_hotspot()
            remove_dns_conf()
            log(f"joining '{ssid}'")
            try:
                join(self.dev, ssid, password)
            except Exception as exc:
                log(f"could not join '{ssid}': {exc}")
                self.message = f"No pude conectarme a «{ssid}»: {exc}. Revisa la contraseña e inténtalo otra vez."
                self.open()
                return
            log(f"connected to '{ssid}'")
            self.done.set()

    def close(self) -> None:
        stop_hotspot()
        remove_dns_conf()


def make_handler(portal: Portal):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # requests from phones are noise, not news
            pass

        def _send(self, body: str, status: int = 200) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # any path: captive-portal probes get the page too
            portal.last_request = time.monotonic()
            self._send(render_page(portal.networks, portal.message))

        def do_POST(self):
            portal.last_request = time.monotonic()
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            ssid, password = parse_form(self.rfile.read(length).decode("utf-8", "replace"))
            if not ssid:
                return self._send(render_page(portal.networks, "Elige una red o escribe su nombre."))
            self._send(render_connecting(ssid))
            threading.Thread(target=portal.try_join, args=(ssid, password), daemon=True).start()

    return Handler


def main() -> int:
    if os.geteuid() != 0:
        log("must run as root")
        return 1
    portal = Portal()
    signal.signal(signal.SIGTERM, lambda *_: portal.done.set())
    portal.open()
    server = ThreadingHTTPServer(("0.0.0.0", 80), make_handler(portal))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while not portal.done.wait(10):
            if not portal.lock.locked() and time.monotonic() - portal.last_request > IDLE_SECONDS:
                log(f"nobody on the portal for {IDLE_SECONDS} s — closing it")
                break
    finally:
        server.shutdown()
        if not portal.lock.locked():
            portal.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

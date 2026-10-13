#!/usr/bin/env python3
"""
cloudflared_provision.py

Auto-provisions a Cloudflare Tunnel for a new Robocontrol rover.

Detection logic:
  ~/.cloudflared/config.yml exists  →  already provisioned, print URL and exit
  No config                         →  new rover:
      1. Read CF credentials from boot partition or .env
      2. Resolve domain: use CF_ZONE_ID if set, otherwise auto-discover from account
      3. Query CF API for existing roverXX tunnels on that domain
      4. Auto-assign next available slot (rover01 → rover02 → rover03 …)
      5. Create tunnel via CF API (no browser needed)
      6. Create CNAME DNS record
      7. Write credentials JSON + config.yml
      8. Install cloudflared as systemd service

Required credentials (CF_ZONE_ID is optional — auto-discovered):
    CF_API_TOKEN=your_cloudflare_api_token
    CF_ACCOUNT_ID=your_cloudflare_account_id
    CF_ZONE_ID=   (optional — auto-picks first active domain if omitted)
    CF_ZONE_NAME= (optional — select by domain name when account has multiple zones)

API token permissions needed:
    Cloudflare Tunnel : Edit
    DNS              : Edit
    Zone             : Read   (only needed when CF_ZONE_ID is not set)

Boot partition credential files (preferred: encrypted):
    /boot/firmware/cloudflared.env.enc   ← AES-256 encrypted (safe to share)
    /boot/firmware/cloudflared.env       ← plain text (fallback)

Encrypt with: bash scripts/encrypt_env.sh cloudflared.env
Passphrase sources (in order):
    CLOUDFLARED_ENV_PASSPHRASE env var  (set in ~/robocontrol/.env)
    /boot/firmware/cloudflared.passphrase file
"""
import os
import sys
import json
import base64
import secrets
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

# ── constants ─────────────────────────────────────────────────────────
ROVER_PREFIX   = "rover"
MAX_ROVERS     = 20
CF_API_BASE    = "https://api.cloudflare.com/client/v4"

HOME           = Path.home()
CF_DIR         = HOME / ".cloudflared"
CONFIG_FILE    = CF_DIR / "config.yml"
ROVER_ID_FILE  = CF_DIR / "rover_id"
ROVER_URL_FILE = CF_DIR / "rover_url"   # full public URL written after provisioning

# ── env loader ────────────────────────────────────────────────────────
def _parse_env_text(text: str) -> dict:
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _get_passphrase(search_dir: Path) -> str:
    passphrase = os.environ.get("CLOUDFLARED_ENV_PASSPHRASE", "").strip()
    if passphrase:
        return passphrase
    pf = search_dir / "cloudflared.passphrase"
    if pf.exists():
        return pf.read_text().strip()
    return ""


def _decrypt_env(enc_path: Path) -> dict:
    """Decrypt an openssl AES-256-CBC + PBKDF2 .enc file in memory."""
    passphrase = _get_passphrase(enc_path.parent)
    if not passphrase:
        print(f"[cloudflared-provision] ERROR: encrypted credentials found at {enc_path}")
        print("  but no passphrase is available. Set one of:")
        print("    CLOUDFLARED_ENV_PASSPHRASE=<passphrase>  in ~/robocontrol/.env")
        print(f"    or create: {enc_path.parent / 'cloudflared.passphrase'}")
        return {}

    result = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000",
         "-in", str(enc_path), "-pass", "fd:0"],
        input=passphrase,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[cloudflared-provision] ERROR: decryption failed for {enc_path}")
        print(f"  {result.stderr.strip()}")
        print("  Wrong passphrase, or file was not encrypted with encrypt_env.sh")
        return {}

    return _parse_env_text(result.stdout)


def load_env() -> dict:
    """
    Search for CF credentials in priority order:
      1. /boot/firmware/cloudflared.env.enc  (encrypted, Bookworm)
      2. /boot/cloudflared.env.enc           (encrypted, older RPi OS)
      3. /boot/firmware/cloudflared.env      (plain, Bookworm)
      4. /boot/cloudflared.env               (plain, older RPi OS)
      5. ~/robocontrol/.env
      6. .env in cwd
    """
    boot_dirs = [Path("/boot/firmware"), Path("/boot")]

    for d in boot_dirs:
        enc = d / "cloudflared.env.enc"
        if enc.exists():
            env = _decrypt_env(enc)
            if env.get("CF_API_TOKEN"):
                return env

    plain_candidates = (
        [d / "cloudflared.env" for d in boot_dirs]
        + [Path.home() / "robocontrol" / ".env", Path.cwd() / ".env"]
    )
    for path in plain_candidates:
        if path.exists():
            env = _parse_env_text(path.read_text())
            if env.get("CF_API_TOKEN"):
                return env

    return {}

# ── Cloudflare API ────────────────────────────────────────────────────
def cf_get(path: str, token: str) -> dict:
    req = urllib.request.Request(
        f"{CF_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def cf_post(path: str, token: str, data: dict) -> dict:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{CF_API_BASE}{path}",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = json.loads(e.read())
        raise RuntimeError(f"CF API error {e.code}: {error_body}") from e


def resolve_zone(token: str, account_id: str, zone_id: str, zone_name: str) -> tuple[str, str]:
    """
    Returns (zone_id, domain_name).

    Resolution order:
      - CF_ZONE_ID set  → validate and return its domain name
      - CF_ZONE_NAME set → search account zones by name
      - Neither set      → list account zones, auto-pick first active one
    """
    if zone_id:
        resp = cf_get(f"/zones/{zone_id}", token)
        domain = resp["result"]["name"]
        print(f"[cloudflared-provision] Using zone: {domain}  (from CF_ZONE_ID)")
        return zone_id, domain

    print("[cloudflared-provision] CF_ZONE_ID not set — discovering zones...")
    resp = cf_get(f"/zones?account.id={account_id}&status=active&per_page=50", token)
    zones = [{"id": z["id"], "name": z["name"]} for z in resp.get("result", [])]

    if not zones:
        raise RuntimeError(
            "No active zones found in this Cloudflare account.\n"
            "  Add a domain at dash.cloudflare.com and make sure it is active,\n"
            "  or set CF_ZONE_ID manually."
        )

    if zone_name:
        match = next((z for z in zones if z["name"] == zone_name), None)
        if not match:
            names = ", ".join(z["name"] for z in zones)
            raise RuntimeError(
                f"CF_ZONE_NAME='{zone_name}' not found in account.\n"
                f"  Available domains: {names}"
            )
        print(f"[cloudflared-provision] Using zone: {match['name']}  (matched CF_ZONE_NAME)")
        return match["id"], match["name"]

    chosen = zones[0]
    if len(zones) == 1:
        print(f"[cloudflared-provision] Auto-selected domain: {chosen['name']}")
    else:
        others = ", ".join(z["name"] for z in zones[1:])
        print(f"[cloudflared-provision] Multiple domains found — using: {chosen['name']}")
        print(f"  Others: {others}")
        print("  Set CF_ZONE_ID or CF_ZONE_NAME in .env to choose a different one.")

    return chosen["id"], chosen["name"]


def existing_tunnel_names(token: str, account_id: str) -> set:
    resp = cf_get(f"/accounts/{account_id}/cfd_tunnel?is_deleted=false&per_page=100", token)
    return {t["name"] for t in resp.get("result", [])}


def next_rover_id(existing: set) -> int:
    for i in range(1, MAX_ROVERS + 1):
        if f"{ROVER_PREFIX}{i:02d}" not in existing:
            return i
    raise RuntimeError(
        f"All rover slots ({ROVER_PREFIX}01–{ROVER_PREFIX}{MAX_ROVERS:02d}) are taken"
    )


def create_tunnel(token: str, account_id: str, name: str) -> tuple[str, str]:
    """Returns (tunnel_id, tunnel_secret_b64)."""
    secret_b64 = base64.b64encode(secrets.token_bytes(32)).decode()
    resp = cf_post(f"/accounts/{account_id}/cfd_tunnel", token, {
        "name": name,
        "tunnel_secret": secret_b64,
        "config_src": "local",
    })
    return resp["result"]["id"], secret_b64


def create_cname(token: str, zone_id: str, hostname: str, tunnel_id: str) -> None:
    cf_post(f"/zones/{zone_id}/dns_records", token, {
        "type":    "CNAME",
        "name":    hostname,
        "content": f"{tunnel_id}.cfargotunnel.com",
        "proxied": True,
        "ttl":     1,
    })

# ── helpers ───────────────────────────────────────────────────────────
def _local_ip() -> str:
    try:
        result = subprocess.run(["hostname", "-I"], capture_output=True, text=True)
        return result.stdout.split()[0]
    except Exception:
        return "<pi-ip>"


def _print_local_only_notice() -> None:
    ip = _local_ip()
    print()
    print("=" * 60)
    print("  Cloudflare credentials not found — LOCAL-ONLY mode")
    print("=" * 60)
    print()
    print("  Your rover is reachable on this network:")
    print(f"    http://{ip}:8000")
    print()
    print("  To get a permanent public URL (e.g. https://rover01.yourdomain.com),")
    print("  add Cloudflare credentials to ~/robocontrol/.env:")
    print()
    print("    1. Create a free account at https://cloudflare.com")
    print("    2. Add your domain to the account")
    print("    3. Create an API token:")
    print("         Dashboard → My Profile → API Tokens → Create Token")
    print("         Permissions: Cloudflare Tunnel:Edit  +  DNS:Edit  +  Zone:Read")
    print("    4. Add to ~/.env:")
    print("         CF_API_TOKEN=<token>")
    print("         CF_ACCOUNT_ID=<account id from any domain page, right sidebar>")
    print("         CF_ZONE_ID=   (optional — auto-detected from your account)")
    print("    5. Re-run: python3 scripts/cloudflared_provision.py")
    print()
    print("  See README → 'Deploying to a New Rover' for full instructions.")
    print()


def _print_success_banner(rover_name: str, hostname: str, local_ip: str) -> None:
    url = f"https://{hostname}"
    print()
    print("=" * 60)
    print(f"  {rover_name} is LIVE")
    print("=" * 60)
    print()
    print(f"  Public URL : {url}")
    print(f"  Local URL  : http://{local_ip}:8000")
    print()
    print("  The tunnel starts automatically on every boot.")
    print("  Logs: journalctl -u cloudflared -f")
    print()


# ── main ──────────────────────────────────────────────────────────────
def main() -> None:
    # ── already provisioned? ─────────────────────────────────────────
    if CONFIG_FILE.exists() and ROVER_ID_FILE.exists():
        public_url = ROVER_URL_FILE.read_text().strip() if ROVER_URL_FILE.exists() else "(unknown)"
        local_ip   = _local_ip()
        print(f"[cloudflared-provision] Already configured — skipping")
        print(f"  Public URL : {public_url}")
        print(f"  Local URL  : http://{local_ip}:8000")
        print(f"  Delete {CONFIG_FILE} to re-provision.")
        return

    # ── load credentials ─────────────────────────────────────────────
    env        = load_env()
    token      = env.get("CF_API_TOKEN", "")
    account_id = env.get("CF_ACCOUNT_ID", "")
    zone_id    = env.get("CF_ZONE_ID", "")      # optional
    zone_name  = env.get("CF_ZONE_NAME", "")    # optional tie-breaker

    missing = [k for k, v in [("CF_API_TOKEN", token), ("CF_ACCOUNT_ID", account_id)] if not v]
    if missing:
        _print_local_only_notice()
        sys.exit(0)  # Not a failure — tunnel is optional

    # ── resolve domain ───────────────────────────────────────────────
    try:
        zone_id, domain = resolve_zone(token, account_id, zone_id, zone_name)
    except Exception as e:
        print(f"[cloudflared-provision] ERROR resolving zone: {e}")
        sys.exit(1)

    # ── find next available rover slot ───────────────────────────────
    print("[cloudflared-provision] Checking existing tunnels...")
    try:
        existing = existing_tunnel_names(token, account_id)
    except Exception as e:
        print(f"[cloudflared-provision] ERROR querying tunnels: {e}")
        sys.exit(1)

    taken = sorted(n for n in existing if n.startswith(ROVER_PREFIX))
    if taken:
        print(f"[cloudflared-provision] Existing rovers: {', '.join(taken)}")

    rover_num  = next_rover_id(existing)
    rover_name = f"{ROVER_PREFIX}{rover_num:02d}"
    hostname   = f"{rover_name}.{domain}"
    print(f"[cloudflared-provision] Assigning: {rover_name}  →  https://{hostname}")

    # ── create tunnel ────────────────────────────────────────────────
    print(f"[cloudflared-provision] Creating tunnel '{rover_name}'...")
    try:
        tunnel_id, secret_b64 = create_tunnel(token, account_id, rover_name)
    except Exception as e:
        print(f"[cloudflared-provision] ERROR creating tunnel: {e}")
        sys.exit(1)
    print(f"[cloudflared-provision] Tunnel ID: {tunnel_id}")

    # ── write credentials JSON ───────────────────────────────────────
    CF_DIR.mkdir(parents=True, exist_ok=True)
    creds_file = CF_DIR / f"{tunnel_id}.json"
    creds_file.write_text(json.dumps({
        "AccountTag":   account_id,
        "TunnelSecret": secret_b64,
        "TunnelID":     tunnel_id,
    }, indent=2))
    creds_file.chmod(0o600)

    # ── write config.yml ─────────────────────────────────────────────
    CONFIG_FILE.write_text(
        f"tunnel: {tunnel_id}\n"
        f"credentials-file: {creds_file}\n"
        f"\ningress:\n"
        f"  - hostname: {hostname}\n"
        f"    service: http://localhost:8000\n"
        f"  - service: http_status:404\n"
    )
    print(f"[cloudflared-provision] Written: {CONFIG_FILE}")

    # ── create DNS CNAME ─────────────────────────────────────────────
    print(f"[cloudflared-provision] Creating DNS CNAME: {hostname}")
    try:
        create_cname(token, zone_id, hostname, tunnel_id)
        print("[cloudflared-provision] DNS record created")
    except Exception as e:
        print(f"[cloudflared-provision] WARNING: DNS creation failed: {e}")
        print(f"  Create manually: CNAME  {hostname}  →  {tunnel_id}.cfargotunnel.com")

    # ── persist rover identity ───────────────────────────────────────
    ROVER_ID_FILE.write_text(f"{rover_num:02d}")
    ROVER_URL_FILE.write_text(f"https://{hostname}")

    # ── install cloudflared as systemd service ───────────────────────
    print("[cloudflared-provision] Installing cloudflared systemd service...")
    result = subprocess.run(
        ["sudo", "cloudflared", "service", "install"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[cloudflared-provision] WARNING: service install failed: {result.stderr.strip()}")
        print("  Run manually: sudo cloudflared service install")
    else:
        subprocess.run(["sudo", "systemctl", "enable", "cloudflared"], check=False)
        subprocess.run(["sudo", "systemctl", "start",  "cloudflared"], check=False)

    _print_success_banner(rover_name, hostname, _local_ip())


if __name__ == "__main__":
    main()

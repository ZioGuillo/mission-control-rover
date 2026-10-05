#!/usr/bin/env bash
# encrypt_env.sh — encrypt cloudflared.env for safe storage on the boot partition.
#
# The encrypted .enc file is safe to share electronically (email, Slack, docs).
# The passphrase travels separately (password manager, Signal, etc.).
#
# Usage (run on your Mac / Linux machine — NOT on the Pi):
#   bash scripts/encrypt_env.sh                       # encrypts cloudflared.env → cloudflared.env.enc
#   bash scripts/encrypt_env.sh path/to/other.env     # custom input file
#
# To decrypt manually (for verification):
#   openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 -in cloudflared.env.enc
set -euo pipefail

INPUT="${1:-cloudflared.env}"
OUTPUT="${INPUT%.env}.env.enc"

if [ ! -f "$INPUT" ]; then
    echo "Error: '$INPUT' not found" >&2
    exit 1
fi

echo "Encrypting '$INPUT'  →  '$OUTPUT'"
echo "You will be prompted for a passphrase — store it in your password manager."
echo ""

openssl enc -aes-256-cbc -pbkdf2 -iter 100000 -in "$INPUT" -out "$OUTPUT"

echo ""
echo "Done."
echo ""
echo "  1. Copy '$OUTPUT' to the boot partition:  /boot/firmware/cloudflared.env.enc"
echo "  2. Share the passphrase via a separate secure channel (1Password, Signal…)"
echo "  3. On the Pi, set CLOUDFLARED_ENV_PASSPHRASE in ~/robocontrol/.env"
echo "     before running install.sh (or export it in your shell first)."
echo ""
echo "  Do NOT copy the plain '$INPUT' to the boot partition."

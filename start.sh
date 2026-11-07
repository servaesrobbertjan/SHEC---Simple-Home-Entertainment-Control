#!/usr/bin/env sh
cd "$(dirname "$0")"
echo "=== SHEC startup check ==="

if ! command -v python3 >/dev/null 2>&1; then
  echo "[X] python3 not found. Install it: sudo apt install python3 python3-venv  (or your distro's equivalent)"
  exit 1
fi
echo "[OK] $(python3 --version)"

if ! command -v vlc >/dev/null 2>&1; then
  echo "[X] VLC not found. Install it: sudo apt install vlc"
  exit 1
fi

[ -f config.json ] || { cp config.example.json config.json; echo "[!] Created config.json - edit it to add your NAS folders."; }

if [ ! -x .venv/bin/python ]; then
  rm -rf .venv
  if ! python3 -m venv .venv; then
    echo "[X] Could not create a virtual environment. Install it: sudo apt install python3-venv"
    exit 1
  fi
fi
.venv/bin/python -m pip install -q --disable-pip-version-check -U -r requirements.txt \
  || echo "[!] Could not install yt-dlp - NAS playback still works, YouTube/Twitch will not."

exec .venv/bin/python -u server.py

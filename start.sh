#!/usr/bin/env sh
# SHEC start for Linux: installs what's missing, mounts the NAS, starts the server.
cd "$(dirname "$0")"
echo "=== SHEC startup check ==="

if ! command -v python3 >/dev/null 2>&1; then
  echo "[..] Installing python3 - enter your password if asked"
  sudo apt-get install -y python3 || exit 1
fi
echo "[OK] $(python3 --version)"

[ -f config.json ] || { cp config.example.json config.json; echo "[!] Created config.json - edit it to add your NAS folders."; }

# System packages + NAS mounts
python3 setup_linux.py || exit 1

# Private Python environment with the latest yt-dlp (re-created if it is broken, e.g. without pip)
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  rm -rf .venv
  python3 -m venv .venv || { echo "[X] Could not create the Python environment."; exit 1; }
fi
if .venv/bin/python -m pip install -q --disable-pip-version-check -U -r requirements.txt; then
  echo "[OK] yt-dlp $(.venv/bin/python -c 'import yt_dlp; print(yt_dlp.version.__version__)')"
else
  echo "[!] Could not install yt-dlp - NAS playback still works, YouTube/Twitch will not."
fi

echo
exec .venv/bin/python -u server.py

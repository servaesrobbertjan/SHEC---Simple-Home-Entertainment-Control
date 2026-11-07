#!/usr/bin/env python3
"""SHEC setup for Linux - run by start.sh.

1. Installs missing system packages (python3-venv, vlc, cifs-utils).
2. Mounts every NAS share from "nas_login" in config.json at /mnt/shec/<nas>/<share>
   and adds it to /etc/fstab so it comes back after a reboot.

Only asks for your sudo password when something actually needs to change.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from nas_paths import mount_point, split_unc

BASE = Path(__file__).resolve().parent
FSTAB = "/etc/fstab"
MARK = "# SHEC:"
SMB_VERSIONS = [None, "3.0", "2.1", "2.0", "1.0"]   # None = let Linux negotiate


def say(tag, msg):
    print(f"[{tag}] {msg}", flush=True)


def sudo(*cmd, stdin=None):
    r = subprocess.run(["sudo", *cmd], input=stdin, text=True,
                       stdout=subprocess.DEVNULL if stdin is not None else None)
    return r.returncode == 0


def pkg_installed(name):
    r = subprocess.run(["dpkg-query", "-W", "-f=${Status}", name], capture_output=True, text=True)
    return "install ok installed" in r.stdout


def ensure_packages(cfg):
    need = []
    if subprocess.run([sys.executable, "-c", "import ensurepip, venv"], capture_output=True).returncode != 0:
        need.append("python3-venv")
    if not shutil.which("vlc"):
        need.append("vlc")
    if cfg.get("nas_login") and not pkg_installed("cifs-utils"):
        need.append("cifs-utils")
    if not need:
        say("OK", "System packages")
        return
    if not shutil.which("apt-get"):
        say("X", "Please install these with your package manager: " + " ".join(need))
        sys.exit(1)
    say("..", f"Installing {' '.join(need)} - enter your password if asked")
    sudo("apt-get", "update", "-qq")
    if not sudo("apt-get", "install", "-y", *need):
        say("X", "Installing packages failed, see the message above.")
        sys.exit(1)
    say("OK", "System packages installed")


def resolve_host(host):
    """Return a name or IP that mount.cifs can use, or None."""
    for name in (host, host + ".local"):
        try:
            socket.gethostbyname(name)
            return name
        except OSError:
            pass
    if shutil.which("nmblookup"):  # Windows-style network name lookup
        out = subprocess.run(["nmblookup", host], capture_output=True, text=True).stdout
        m = re.search(r"^(\d+\.\d+\.\d+\.\d+) ", out, re.M)
        if m:
            return m.group(1)
    return None


def fstab_escape(s):
    return s.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")


def write_credentials(host, share, entry):
    cred = Path.home() / ".config" / "shec" / f"{host.lower()}-{share.lower()}.cred"
    cred.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(cred, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"username={entry.get('username', '')}\npassword={entry.get('password', '')}\n")
    os.chmod(cred, 0o600)
    return cred


def update_fstab(mp, line):
    """Replace our previous entry for this mount point (if any) with the new line."""
    with open(FSTAB, encoding="utf-8") as f:
        lines = f.read().splitlines()
    out, skip = [], False
    for ln in lines:
        if skip:
            skip = False
            continue
        if ln.strip() == f"{MARK} {mp}":
            skip = True  # drop the marker and the entry under it
            continue
        out.append(ln)
    out += [f"{MARK} {mp}", line]
    if out == lines:
        return True
    if not os.path.exists(FSTAB + ".shec-backup"):
        sudo("cp", FSTAB, FSTAB + ".shec-backup")
    ok = sudo("tee", FSTAB, stdin="\n".join(out) + "\n")
    subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True)
    return ok


def ensure_mount(entry):
    unc = entry.get("share", "")
    parts = split_unc(unc)
    if not parts:
        say("!", f"nas_login share {unc!r} should look like \\\\NAS\\share - skipped")
        return
    host, share, _ = parts
    mp = mount_point(host, share)
    if os.path.ismount(mp):
        say("OK", f"NAS {unc} is mounted at {mp}")
        return

    addr = resolve_host(host)
    if not addr:
        say("X", f"Cannot find '{host}' on the network. In config.json, replace '{host}' with the NAS's "
                 f"IP address (e.g. \\\\\\\\192.168.1.10\\\\{share}) and run start.sh again.")
        return

    if entry.get("username"):
        opts = f"credentials={write_credentials(host, share, entry)}"
    else:
        opts = "guest"
    opts += f",uid={os.getuid()},gid={os.getgid()},iocharset=utf8,file_mode=0644,dir_mode=0755"

    say("..", f"Mounting {unc} at {mp} - enter your password if asked")
    sudo("mkdir", "-p", mp)
    # NAS boxes differ in which SMB (Windows file sharing) version they speak. Windows picks one
    # automatically; Linux only tries new versions unless told otherwise, so try newest -> oldest.
    errors = []
    for vers in SMB_VERSIONS:
        try_opts = opts + (f",vers={vers}" if vers else "")
        r = subprocess.run(["sudo", "mount", "-t", "cifs", f"//{addr}/{share}", mp, "-o", try_opts],
                           capture_output=True, text=True)
        if r.returncode == 0 and os.path.ismount(mp):
            break
        err = (r.stderr or r.stdout).strip().splitlines()
        err = err[0] if err else f"exit code {r.returncode}"
        errors.append(f"SMB {vers or 'auto'}: {err}")
        if "error(13)" in err:  # permission denied - other versions won't help
            say("X", f"The NAS refused the login for {unc}: check username/password in config.json.")
            return
    else:
        say("X", f"Mounting {unc} failed with every SMB version:")
        for e in errors:
            print("      " + e)
        print("      Check that SMB/Windows file sharing is enabled on the NAS.")
        return

    line = f"//{fstab_escape(addr)}/{fstab_escape(share)} {fstab_escape(mp)} cifs {try_opts},nofail,_netdev 0 0"
    if not update_fstab(mp, line):
        say("!", "Mounted, but /etc/fstab could not be updated - it won't come back after a reboot.")
    say("OK", f"NAS {unc} is mounted at {mp} (SMB {vers or 'auto'}; also after reboot)")


def main():
    cfg_path = BASE / "config.json"
    try:
        with open(cfg_path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        say("X", f"Cannot read config.json: {e}")
        sys.exit(1)
    ensure_packages(cfg)
    for entry in cfg.get("nas_login", []):
        ensure_mount(entry)


if __name__ == "__main__":
    main()

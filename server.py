"""SHEC - phone-controlled VLC player.

Run this on the PC that has VLC. Open the printed URL on your phone.
"""
import base64
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from nas_paths import local_path, mount_point, split_unc

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = BASE_DIR / "config.json"

VIDEO_EXT = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg",
    ".mpeg", ".ts", ".m2ts", ".vob", ".3gp", ".ogv", ".iso",
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wav", ".opus",
}

DEFAULT_CONFIG = {
    "port": 8000,
    "pin": "",
    "vlc_path": "",
    "vlc_http_port": 8081,
    "vlc_http_password": "shec",
    "fullscreen": True,
    "media_roots": {},
    "youtube_format": "bv*[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<=1080]+ba/b",
    "twitch_format": "best",
    "autoplay_next": True,
    "nas_login": [],
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
        except json.JSONDecodeError as e:
            sys.exit(f"[X] config.json has a mistake on line {e.lineno}, column {e.colno}: {e.msg}\n"
                     f"    Tip: every \\ in a path must be written as \\\\ and entries need commas between them.")
    else:
        print(f"[!] No config.json found - copy config.example.json to config.json and edit it.")
    # \\NAS\share\... paths -> /mnt/shec/nas/share/... on Linux (see nas_paths.py)
    cfg["media_roots"] = {name: local_path(p) for name, p in cfg["media_roots"].items()}
    return cfg


CONFIG = load_config()


# --------------------------------------------------------------------------- VLC
def find_vlc():
    if CONFIG["vlc_path"]:
        return CONFIG["vlc_path"]
    found = shutil.which("vlc")
    if found:
        return found
    if platform.system() == "Windows":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                p = Path(base) / "VideoLAN" / "VLC" / "vlc.exe"
                if p.exists():
                    return str(p)
    sys.exit("[X] VLC not found. Install VLC from https://www.videolan.org/ "
             "or set \"vlc_path\" in config.json to the full path of vlc.exe / vlc.")


class VLC:
    """Starts VLC with the media and talks to its built-in HTTP interface for remote control."""

    def __init__(self):
        self.exe = find_vlc()
        self.proc = None
        self.title = ""
        self.dvd = False
        self.has_playlist = False
        self.lock = threading.Lock()

    def play(self, media, title="", audio=None, headers=None, playlist=None, dvd=False):
        """media: path/URL to play. playlist: extra items queued after it (next episodes)."""
        with self.lock:
            self._kill()
            args = [
                self.exe,
                "--extraintf=http",
                "--http-host=127.0.0.1",
                f"--http-port={CONFIG['vlc_http_port']}",
                f"--http-password={CONFIG['vlc_http_password']}",
                "--no-qt-privacy-ask",
                "--no-one-instance",
            ]
            if CONFIG["fullscreen"]:
                args.append("--fullscreen")
            headers = headers or {}
            if headers.get("User-Agent"):
                args.append(f"--http-user-agent={headers['User-Agent']}")
            if headers.get("Referer"):
                args.append(f"--http-referrer={headers['Referer']}")
            args.append(media)
            if audio:
                args.append(f":input-slave={audio}")
            if title:
                args.append(f":meta-title={title}")
            args.extend(playlist or [])
            kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            self.proc = subprocess.Popen(args, **kwargs)
            self.title = title or os.path.basename(media)
            self.dvd = dvd
            self.has_playlist = bool(playlist)

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _request(self, params=None):
        query = "?" + urllib.parse.urlencode(params) if params else ""
        url = f"http://127.0.0.1:{CONFIG['vlc_http_port']}/requests/status.json{query}"
        token = base64.b64encode(f":{CONFIG['vlc_http_password']}".encode()).decode()
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return json.loads(r.read().decode("utf-8"))

    def status(self):
        if not self.running():
            return {"state": "stopped", "title": ""}
        try:
            s = self._request()
        except Exception:
            return {"state": "starting", "title": self.title}
        title = self.title
        if self.has_playlist:  # show the episode that is actually playing now
            meta = (s.get("information") or {}).get("category", {}).get("meta", {})
            title = meta.get("filename") or meta.get("title") or title
        return {
            "state": s.get("state", "unknown"),
            "title": title,
            "dvd": self.dvd,
            "playlist": self.has_playlist,
            "time": s.get("time", 0),
            "length": s.get("length", 0),
            "volume": round(s.get("volume", 0) / 2.56),  # VLC uses 0-512, 256 = 100%
            "fullscreen": bool(s.get("fullscreen")),
        }

    def control(self, cmd, val=None):
        if cmd == "stop":
            with self.lock:
                self._kill()
            return
        if not self.running():
            raise RuntimeError("Nothing is playing")
        mapping = {
            "pause": {"command": "pl_pause"},
            "seek": {"command": "seek", "val": val},          # e.g. "+30", "-10", "50%"
            "volume": {"command": "volume", "val": val},      # e.g. "+25", "-25", "256"
            "fullscreen": {"command": "fullscreen"},
            "next": {"command": "pl_next"},
            "prev": {"command": "pl_previous"},
        }
        if cmd == "key":  # DVD menu navigation: nav-up/down/left/right/activate, disc-menu
            if val not in {"nav-up", "nav-down", "nav-left", "nav-right", "nav-activate", "disc-menu"}:
                raise ValueError("Unknown key")
            mapping["key"] = {"command": "key", "val": val}
        if cmd not in mapping:
            raise ValueError(f"Unknown command: {cmd}")
        self._request(mapping[cmd])


vlc = VLC()


# ----------------------------------------------------------------- Online sources
def need_ytdlp():
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed: pip install -U yt-dlp")


def resolve_stream(url):
    """Use yt-dlp to turn a YouTube/Twitch/etc. page URL into direct stream URL(s)."""
    need_ytdlp()
    is_twitch = "twitch.tv" in url
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": CONFIG["twitch_format"] if is_twitch else CONFIG["youtube_format"],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    title = info.get("title") or url
    if is_twitch and info.get("uploader"):
        title = f"{info['uploader']} - {title}"
    if info.get("requested_formats"):  # separate video + audio streams
        video, audio = info["requested_formats"][0], info["requested_formats"][-1]
        return video["url"], audio["url"], title, video.get("http_headers", {})
    return info["url"], None, title, info.get("http_headers", {})


def _video_entry(e):
    vid = e.get("id")
    return {
        "title": e.get("title"),
        "url": f"https://www.youtube.com/watch?v={vid}",
        "channel": e.get("channel") or e.get("uploader") or "",
        "channel_url": e.get("uploader_url") or e.get("channel_url") or "",
        "duration": e.get("duration"),
        "views": e.get("view_count"),
        "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        "live": e.get("live_status") == "is_live",
    }


def youtube_search(query, limit=15):
    need_ytdlp()
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    # ytsearch also returns channels/playlists; keep only videos (11-character ids)
    return [_video_entry(e) for e in info.get("entries") or [] if len(e.get("id") or "") == 11]


def channel_base_url(text):
    """'@name', 'name' or any youtube.com channel link -> https://www.youtube.com/<channel>"""
    text = text.strip()
    if not text.startswith("http"):
        return "https://www.youtube.com/" + (text if text.startswith("@") else "@" + text)
    parsed = urllib.parse.urlparse(text)
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        raise ValueError("Not a channel link")
    if parts[0] in ("channel", "c", "user") and len(parts) > 1:
        return f"https://www.youtube.com/{parts[0]}/{parts[1]}"
    if parts[0].startswith("@"):
        return f"https://www.youtube.com/{parts[0]}"
    raise ValueError("Not a channel link - use @name or a youtube.com/@name link")


def youtube_channel(text, tab="videos", limit=24):
    need_ytdlp()
    base = channel_base_url(text)
    if tab not in ("videos", "streams"):
        tab = "videos"
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": limit}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{base}/{tab}", download=False)
    thumbs = info.get("thumbnails") or []
    square = sorted((t for t in thumbs if t.get("width") and t.get("width") == t.get("height")),
                    key=lambda t: t["width"])
    avatar = next((t["url"] for t in thumbs if t.get("id") == "avatar_uncropped"), None)
    if square:
        avatar = square[0]["url"]
    name = info.get("channel") or info.get("uploader") or base.rsplit("/", 1)[-1]
    url = info.get("uploader_url") or info.get("channel_url") or base
    videos = []
    for e in info.get("entries") or []:
        if len(e.get("id") or "") == 11:
            v = _video_entry(e)
            v["channel"], v["channel_url"] = name, url
            videos.append(v)
    return {"name": name, "url": url, "avatar": avatar, "videos": videos}


_twitch_cache = {}          # login -> (checked_at, status dict)
TWITCH_CACHE_SECONDS = 60


def _twitch_check(login):
    hit = _twitch_cache.get(login)
    if hit and time.time() - hit[0] < TWITCH_CACHE_SECONDS:
        return hit[1]
    status = {"name": login, "display": login, "live": False, "title": "", "exists": True}
    opts = {"quiet": True, "no_warnings": True, "logger": _SilentLogger()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.twitch.tv/{login}", download=False, process=False)
        status.update(live=True, display=info.get("uploader") or login, title=info.get("description") or "",
                      preview=f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-320x180.jpg"
                              f"?t={int(time.time() // 60)}")
    except Exception as exc:
        if "does not exist" in str(exc):
            status["exists"] = False
        elif "not currently live" not in str(exc):
            status["error"] = True   # network trouble etc. - don't cache
            return status
    _twitch_cache[login] = (time.time(), status)
    return status


class _SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def twitch_status(logins):
    need_ytdlp()
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(_twitch_check, logins))


# ----------------------------------------------------------------------- Favorites
FAV_PATH = BASE_DIR / "favorites.json"
_fav_lock = threading.Lock()


def load_favorites():
    try:
        with open(FAV_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("youtube", [])
    data.setdefault("twitch", [])
    return data


def update_favorites(body):
    kind, action = body.get("kind"), body.get("action")
    with _fav_lock:
        fav = load_favorites()
        if kind == "youtube":
            if action == "add":
                ch = youtube_channel(body.get("channel", ""), limit=1)  # look up the real name/avatar
                fav["youtube"] = [c for c in fav["youtube"] if c["url"] != ch["url"]]
                fav["youtube"].append({"name": ch["name"], "url": ch["url"], "avatar": ch["avatar"]})
                fav["youtube"].sort(key=lambda c: c["name"].lower())
            elif action == "remove":
                fav["youtube"] = [c for c in fav["youtube"] if c["url"] != body.get("url")]
        elif kind == "twitch":
            name = (body.get("name") or "").strip().lower()
            if action == "add" and name and name not in fav["twitch"]:
                fav["twitch"].insert(0, name)
            elif action == "remove":
                fav["twitch"] = [c for c in fav["twitch"] if c != name]
        else:
            raise ValueError("Unknown favorites kind")
        tmp = FAV_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fav, f, indent=2, ensure_ascii=False)
        os.replace(tmp, FAV_PATH)
        return fav


# ---------------------------------------------------------------------- NAS files
def safe_path(root_name, rel=""):
    roots = CONFIG["media_roots"]
    if root_name not in roots:
        raise ValueError("Unknown media root")
    root = os.path.realpath(roots[root_name])
    target = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, target]) != root:
        raise ValueError("Path outside media root")
    return root, target


_IFO_NAMES = ["VIDEO_TS.IFO"] if platform.system() == "Windows" else ["VIDEO_TS.IFO", "video_ts.ifo"]
_IFO_SUBDIRS = ["", "VIDEO_TS"] if platform.system() == "Windows" else ["", "VIDEO_TS", "video_ts"]
_dvd_cache = {}


def find_dvd(folder):
    """Return the VIDEO_TS.IFO path if this folder is a DVD rip, else None."""
    if folder in _dvd_cache:
        return _dvd_cache[folder]
    found = None
    for sub in _IFO_SUBDIRS:
        for name in _IFO_NAMES:
            p = os.path.join(folder, sub, name)
            if os.path.isfile(p):
                found = p
                break
        if found:
            break
    _dvd_cache[folder] = found
    return found


def list_media(folder):
    """Return (sub-folder names, video file entries) in folder, sorted."""
    dirs, files = [], []
    with os.scandir(folder) as it:
        for entry in it:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    dirs.append(entry.name)
                elif os.path.splitext(entry.name)[1].lower() in VIDEO_EXT:
                    files.append({"name": entry.name, "size": entry.stat().st_size})
            except OSError:
                continue
    dirs.sort(key=str.lower)
    files.sort(key=lambda f: f["name"].lower())
    return dirs, files


_single_cache = {}


def single_video(folder):
    """If a folder holds exactly one video file (a typical film folder), return its name."""
    if folder not in _single_cache:
        try:
            files = list_media(folder)[1]
        except OSError:
            files = []
        _single_cache[folder] = files[0]["name"] if len(files) == 1 else None
    return _single_cache[folder]


def inspect_dir(path):
    if find_dvd(path):
        return {"dvd": True}
    single = single_video(path)
    return {"movie": single} if single else {}


# Folders with more sub-folders than this are listed without looking inside each one
# (each look is a network round-trip; Anime has 500+ series).
DEEP_SCAN_LIMIT = 80


def browse(root_name, rel=""):
    root, target = safe_path(root_name, rel)
    dirs, files = list_media(target)
    is_dvd = find_dvd(target) is not None
    if is_dvd:  # hide the raw .VOB chunks and DVD system folders, offer "Play DVD" instead
        files = []
        dirs = [d for d in dirs if d.lower() not in ("video_ts", "audio_ts")]
    infos = [{} for _ in dirs]
    if len(dirs) <= DEEP_SCAN_LIMIT:
        with ThreadPoolExecutor(max_workers=16) as pool:
            infos = list(pool.map(lambda d: inspect_dir(os.path.join(target, d)), dirs))
    rel_norm = os.path.relpath(target, root)
    return {"root": root_name, "path": "" if rel_norm == "." else rel_norm.replace("\\", "/"),
            "dvd": is_dvd,
            "dirs": [{"name": d, **i} for d, i in zip(dirs, infos)],
            "files": files}


def nas_login():
    """Log in to SMB shares listed in config 'nas_login' (Windows: net use)."""
    for entry in CONFIG["nas_login"]:
        share = entry.get("share", "")
        if not share:
            continue
        if platform.system() != "Windows":  # mounted by setup_linux.py (start.sh)
            unc = split_unc(share)
            where = mount_point(unc[0], unc[1]) if unc else local_path(share)
            if os.path.ismount(where):
                print(f"NAS:   {share} mounted at {where}")
            else:
                print(f"[!] {share} is NOT mounted at {where} - see the messages above")
            continue
        if not entry.get("username"):
            continue
        if os.path.isdir(share):
            print(f"NAS:   {share} already reachable")
            continue
        r = subprocess.run(
            ["net", "use", share, entry.get("password", ""), f"/user:{entry['username']}", "/persistent:no"],
            capture_output=True, text=True,
        )
        ok = r.returncode == 0 and os.path.isdir(share)
        print(f"NAS:   {share} login {'OK' if ok else 'FAILED: ' + (r.stderr or r.stdout).strip()}")


# --------------------------------------------------------------------- HTTP server
class Handler(BaseHTTPRequestHandler):
    server_version = "SHEC/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return not CONFIG["pin"] or self.headers.get("X-Pin") == str(CONFIG["pin"])

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _route(self, method):
        parsed = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        path = parsed.path

        if method == "GET" and path in ("/", "/index.html"):
            return self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if not path.startswith("/api/"):
            return self._send(404, {"error": "Not found"})
        if not self._authorized():
            return self._send(401, {"error": "Wrong PIN"})

        try:
            if method == "GET" and path == "/api/info":
                return self._send(200, {"roots": list(CONFIG["media_roots"].keys()),
                                        "ytdlp": yt_dlp is not None})
            if method == "GET" and path == "/api/browse":
                return self._send(200, browse(q.get("root", ""), q.get("path", "")))
            if method == "GET" and path == "/api/youtube/search":
                return self._send(200, {"results": youtube_search(q.get("q", ""))})
            if method == "GET" and path == "/api/youtube/channel":
                return self._send(200, youtube_channel(q.get("url", ""), q.get("tab", "videos")))
            if method == "GET" and path == "/api/twitch/status":
                return self._send(200, {"channels": twitch_status(load_favorites()["twitch"])})
            if method == "GET" and path == "/api/favorites":
                return self._send(200, load_favorites())
            if method == "POST" and path == "/api/favorites":
                return self._send(200, update_favorites(self._json_body()))
            if method == "GET" and path == "/api/status":
                return self._send(200, vlc.status())
            if method == "POST" and path == "/api/play":
                body = self._json_body()
                if body.get("type") == "dvd":
                    _, full = safe_path(body.get("root", ""), body.get("path", ""))
                    ifo = find_dvd(full)
                    if not ifo:
                        raise ValueError("No DVD (VIDEO_TS) found in this folder")
                    name = os.path.basename(full)
                    if name.lower() == "video_ts":
                        name = os.path.basename(os.path.dirname(full))
                    vlc.play(ifo, title=name, dvd=True)
                elif body.get("type") == "file":
                    _, full = safe_path(body.get("root", ""), body.get("path", ""))
                    if not os.path.isfile(full):
                        raise ValueError("File not found")
                    playlist = []
                    if CONFIG["autoplay_next"]:  # queue the following episodes in this folder
                        folder = os.path.dirname(full)
                        names = [f["name"] for f in list_media(folder)[1]]
                        me = os.path.basename(full)
                        if me in names:
                            playlist = [os.path.join(folder, n) for n in names[names.index(me) + 1:]][:200]
                    vlc.play(full, title=os.path.basename(full), playlist=playlist)
                else:
                    url = (body.get("url") or "").strip()
                    if not url.startswith(("http://", "https://")):
                        raise ValueError("Invalid URL")
                    media, audio, title, headers = resolve_stream(url)
                    vlc.play(media, title=title, audio=audio, headers=headers)
                return self._send(200, {"ok": True, "title": vlc.title})
            if method == "POST" and path == "/api/control":
                body = self._json_body()
                vlc.control(body.get("cmd"), body.get("val"))
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "Not found"})
        except Exception as exc:  # report errors to the phone UI
            msg = str(exc)
            if yt_dlp and isinstance(exc, yt_dlp.utils.DownloadError):
                msg = re.sub(r"^(ERROR: )?\[[^\]]+\] [^:]+: ", "", msg.replace("ERROR: ", "", 1))
                if "404" in msg:
                    msg = "Not found - check the channel name or link"
            return self._send(400, {"error": msg})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    nas_login()
    for name, path in CONFIG["media_roots"].items():
        if not os.path.isdir(path):
            print(f"[!] Folder '{name}' not reachable: {path}")
    port = int(CONFIG["port"])
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        sys.exit(f"[X] Cannot use port {port} ({e.strerror}). Is SHEC already running? "
                 f"Otherwise pick another \"port\" in config.json.")
    print(f"VLC:   {vlc.exe}")
    print(f"Roots: {CONFIG['media_roots'] or '(none configured)'}")
    if yt_dlp is None:
        print("[!] yt-dlp not installed - YouTube/Twitch disabled (pip install -U yt-dlp)")
    print(f"\nOpen on your phone:  http://{lan_ip()}:{port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        vlc.control("stop")


if __name__ == "__main__":
    main()

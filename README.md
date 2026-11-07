# SHEC – Simple Home Entertainment Control
# phone remote for VLC

Pick a video on your phone (from your NAS, YouTube, Twitch or any link), tap it, and the PC plays it in VLC.

```
 Phone (browser) ──Wi-Fi──▶ PC: server.py ──▶ VLC (fullscreen)
                                   │
                                   ├─ NAS share (SMB mount / mapped drive)
                                   └─ yt-dlp (YouTube / Twitch → stream URL)
```

No app on the phone: it's a web page served by the PC.

## PC setup (Windows or Linux)

1. **Windows:** nothing to install by hand. The first time you run `start.bat`, it installs whatever is
   missing: Python (for this user only), VLC, yt-dlp, and a firewall rule so the phone can connect.
   Click "Yes" when Windows asks for permission.
   **Linux:** install `python3`, `python3-venv` and `vlc` with your package manager.
2. Make the NAS reachable as a folder:
   - **Windows:** use a UNC path such as `\\NAS\video`. If the NAS needs a login, put it in `nas_login`
     in `config.json`. The server logs in (`net use`) at startup when the share isn't reachable yet:
     ```json
     "nas_login": [ { "share": "\\\\NAS\\video", "username": "me", "password": "secret" } ]
     ```
     Or leave it empty and let Windows remember the login (open the share once in Explorer and tick
     "Remember my credentials").
   - **Linux:** mount the share, with the login in a credentials file:
     ```sh
     printf 'username=me\npassword=secret\n' | sudo tee /etc/nas-cred && sudo chmod 600 /etc/nas-cred
     sudo mkdir -p /mnt/video
     echo '//NAS/video /mnt/video cifs credentials=/etc/nas-cred,uid=1000,iocharset=utf8,_netdev 0 0' | sudo tee -a /etc/fstab
     sudo mount /mnt/video
     ```
3. Copy `config.example.json` to `config.json` and set `media_roots` (display name → folder):
   ```json
   "media_roots": { "Movies": "\\\\NAS\\Movies", "Series": "/mnt/series" }
   ```
   (In JSON, every `\` must be written as `\\`.)
4. Start it:
   - Windows: double-click `start.bat`
   - Linux: `sh start.sh` (run it from the desktop session so VLC can open a window)
5. The console prints something like `Open on your phone: http://192.168.1.20:8000`. Open that on your phone
   (same Wi-Fi) and use "Add to Home Screen" so it works like an app.

The phone can only connect if the PC's network is set to **Private** (Settings → Network & Internet).
The setup warns you if it's set to Public.

## Using it

| Tab | What it does |
|---|---|
| **NAS** | Browse your configured folders (with a filter box for big folders) |
| **YouTube** | Search, paste a link, or type `@channel`. Tap a channel name in the results to open it and ☆ **Save** it. Saved channels show as round icons at the top: tap one for its latest videos, past streams, or 🔴 **Live** |
| **Twitch** | Type a channel name and tap **Play**, or ☆ to save it. Favorites show who is live (preview + stream title, sorted on top); offline channels are faded. Status refreshes every minute while the tab is open. **Edit** removes favorites |

Saved YouTube and Twitch channels are stored on the PC in `favorites.json`, so every phone sees the same list.
| **Link** | Any URL yt-dlp supports (Twitch VODs, Vimeo, …) |

How folders are shown:

- 💿 **DVD rip** (a folder with `VIDEO_TS`): tap to play the whole DVD. Menu, arrow and OK buttons appear for the DVD menu.
- 🎬 **Folder with one video** (typical film folder): tap to play it; tap 📁 to look inside (e.g. extras).
- 📁 **Other folders**: tap to open. Tapping an episode queues the rest of that folder, so the next
  episode starts automatically; ⏮/⏭ switch episodes. Turn this off with `"autoplay_next": false`.
- In folders with more than 80 sub-folders (e.g. Anime) the 💿/🎬 check is skipped to keep it fast;
  open the folder and you get the same "Play DVD" or video list.

The bar at the bottom controls the PC: play/pause, skip ±10s/±30s, tap the progress bar to jump, volume, fullscreen, stop.

## Config options

| Key | Default | Meaning |
|---|---|---|
| `port` | `8000` | Port the phone connects to |
| `pin` | `""` | If set, the phone must enter this PIN once |
| `vlc_path` | auto | Full path to `vlc`/`vlc.exe` if it isn't found automatically |
| `vlc_http_port` / `vlc_http_password` | `8081` / `shec` | VLC's internal control interface (only listens on 127.0.0.1) |
| `fullscreen` | `true` | Start VLC fullscreen |
| `autoplay_next` | `true` | Continue with the next episode in the folder |
| `nas_login` | `[]` | SMB logins to make at startup (Windows). The password is stored as plain text in `config.json` |
| `youtube_format` | ≤1080p H.264 + AAC | yt-dlp format selection |

## Troubleshooting

- **YouTube stops working**: YouTube changes often; update yt-dlp: `python -m pip install -U yt-dlp`.
- **Phone can't connect**: check the PC firewall and that both devices are on the same network.
- **Twitch "offline"/"does not exist"**: the channel isn't live right now.

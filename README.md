# LAN Clipboard & File Share

A lightweight, self-hosted clipboard sync and file sharing tool for your local network. Share text, images, and files instantly between Mac and Windows (or any device with a browser) — no cloud, no accounts, no installs on the client side.

Works great as a companion for **[GL.iNet GLKVM](https://www.gl-inet.com/campaign/kvm/)** (Comet GL-RM1) and similar KVM-over-IP hardware — when you're already controlling a remote machine, seamless clipboard and file sharing between host and target is exactly what's missing.

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License](https://img.shields.io/badge/license-CC%20BY--NC%204.0-green)
![Platform](https://img.shields.io/badge/host-macOS-lightgrey)
![Clients](https://img.shields.io/badge/clients-any%20browser-orange)

## Features

- **Bidirectional clipboard sync** — Copy on Mac, paste on Windows (and vice versa). Text and images.
- **Auto-sync mode** — Toggle continuous clipboard monitoring. Uses the Clipboard API (`clipboardchange` event on Chrome 143+) with polling fallback. Deduplicates automatically.
- **Image clipboard support** — Screenshots and copied images are detected, transferred, and can be pasted on the other machine.
- **File sharing** — Upload, download, and manage files through the web UI. Drag & drop supported. Up to 20 GB per file.
- **Folder uploads** — Upload entire directory trees, preserving structure.
- **ZIP handling** — Auto-extract ZIP files on upload (optional).
- **Large text handling** — Clipboard entries up to 10 MB with preview/expand UI for massive pastes.
- **HTTPS mode** — Optional TLS for Clipboard API auto-sync (requires secure context). Run two instances: HTTP on port 9876, HTTPS on port 9877.
- **Zero client install** — Everything runs in the browser. Just open the URL.
- **Dark/light theme** — Adapts to system preference.
- **Mobile friendly** — Responsive layout works on phones and tablets.

## How It Works

The host (macOS) runs a Flask server that watches the system clipboard and serves a web UI. Any device on the same network opens the URL in a browser to see the shared clipboard and file manager. Changes sync in both directions.

```
┌─────────────┐         HTTP/HTTPS         ┌─────────────┐
│   macOS      │◄─────────────────────────►│  Browser     │
│   (host)     │   clipboard + files       │  (any OS)    │
│   Flask app  │                           │  Web UI      │
└─────────────┘                            └─────────────┘
```

## Installation

### Prerequisites

- **Python 3.8+** (macOS ships with it, or `brew install python`)
- **Flask**: `pip3 install flask`

### Quick Start

```bash
# Clone the repo
git clone https://github.com/thomaswillner/lan-clipboard-fileshare.git
cd lan-clipboard-fileshare

# Install dependency
pip3 install flask

# Run (HTTP mode — works for everything except auto-sync on non-localhost)
python3 clipboard_share.py
```

Open the URL shown in terminal on any device on your network (e.g. `http://192.168.178.96:9876`).

### HTTPS Mode (for Clipboard API Auto-Sync)

The browser Clipboard API requires a [secure context](https://developer.mozilla.org/en-US/docs/Web/Security/Secure_Contexts) (HTTPS or localhost). To enable auto-sync from non-localhost clients:

```bash
# Generate a self-signed certificate (one time)
openssl req -x509 -newkey rsa:2048 -keyout .clipboard-share-key.pem \
  -out .clipboard-share-cert.pem -days 3650 -nodes \
  -subj "/CN=clipboard-share" \
  -addext "subjectAltName=IP:$(python3 -c 'import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(("8.8.8.8",80)); print(s.getsockname()[0])'),IP:127.0.0.1,DNS:localhost"

# Run HTTPS mode
python3 clipboard_share.py --ssl
```

On first visit, accept the self-signed certificate in your browser.

### Run Both HTTP + HTTPS (Recommended)

For maximum compatibility, run both modes simultaneously:

```bash
# Terminal 1 — HTTP on port 9876 (bookmarks, quick access)
python3 clipboard_share.py

# Terminal 2 — HTTPS on port 9877 (auto-sync enabled)
python3 clipboard_share.py --ssl
```

### macOS LaunchAgent (Auto-Start)

To run at login automatically, create LaunchAgent plist files:

```bash
# HTTP mode
cat > ~/Library/LaunchAgents/com.clipboardshare.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.clipboardshare</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/clipboard_share.py</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>
EOF

# Load it
launchctl load ~/Library/LaunchAgents/com.clipboardshare.plist
```

Repeat with `--ssl` flag and a different label for the HTTPS instance.

## Usage

### Clipboard

- **Push text** — Type or paste into the text area, click "Push to Mac"
- **Pull from Mac** — Click "Pull" or enable auto-sync toggle
- **Auto-sync** — Toggle the switch to continuously monitor and push clipboard changes (HTTPS required for non-localhost)
- **Images** — Paste an image (Ctrl+V / Cmd+V) into the text area, or take a screenshot — it appears automatically
- **Copy** — Click any clipboard entry to copy it

### Files

- **Upload** — Drag & drop files/folders onto the upload zone, or click to browse
- **Download** — Click any file to download
- **Delete** — Click the × button on a file
- **Folder upload** — Preserves directory structure
- **ZIP extract** — Toggle auto-extract for uploaded ZIP files

## Configuration

Key constants at the top of `clipboard_share.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `MAX_HISTORY` | 50 | Max clipboard entries kept |
| `MAX_CLIPBOARD_ENTRY_MB` | 10 | Max size per clipboard entry (MB) |
| `MAX_UPLOAD_MB` | 20480 | Max file upload size (20 GB) |
| `MAX_TOTAL_MEMORY_MB` | 100 | Total clipboard memory limit |
| `SHARED_DIR` | `~/SharedFiles` | Directory for shared files |
| `PREVIEW_CHARS` | 500 | Characters shown before "expand" |

## GLKVM / KVM-over-IP Companion

If you use a **[GL.iNet GLKVM](https://www.gl-inet.com/campaign/kvm/)** device (like the Comet GL-RM1) or any other KVM-over-IP solution to remotely control machines, this tool fills a gap that KVM hardware doesn't cover: **clipboard and file transfer between your local machine and the remote target**.

KVM-over-IP gives you keyboard, video, and mouse — but not clipboard sync or file sharing. Run this tool on the target machine and open its URL from your controlling machine's browser. Now you have full bidirectional clipboard sharing and file transfer alongside your KVM session.

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/clipboard` | GET | Get clipboard history |
| `/api/clipboard` | POST | Push text to clipboard |
| `/api/clipboard/<id>` | GET | Get full text of an entry |
| `/api/clipboard-image` | POST | Push image to clipboard |
| `/api/clipboard-image/<file>` | GET | Get clipboard image |
| `/api/files` | GET | List shared files |
| `/api/upload` | POST | Upload file(s) |
| `/api/upload-with-path` | POST | Upload preserving folder structure |
| `/api/delete` | POST | Delete a file |
| `/files/<path>` | GET | Download a file |

## Security Notes

- This tool is designed for **trusted local networks only**. There is no authentication.
- The HTTPS certificate is self-signed — intended for LAN use, not public internet.
- File uploads are unrestricted within the size limit. Do not expose to untrusted networks.

## License

[CC BY-NC 4.0](LICENSE) — Free for personal and non-commercial use. Attribution required. Commercial use is not permitted.

## Author

Thomas Willner — [GitHub](https://github.com/thomaswillner)

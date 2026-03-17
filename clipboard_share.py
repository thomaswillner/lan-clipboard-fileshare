#!/usr/bin/env python3
"""Clipboard + File Share v2.3 — Bidirectional Mac <-> Windows, text + image clipboard, massive text support"""
import threading, time, subprocess, socket, os, json, zipfile, io, shutil, re, uuid, hashlib, base64
from datetime import datetime
from flask import Flask, jsonify, render_template_string, send_from_directory, send_file, request
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024 * 1024  # 20GB max upload

# --- State ---
clipboard_history = []  # Each: {"id": str, "type": "text"|"image", "text"?: str, "file"?: str, "time": str, "source": str, "length": int}
clipboard_lock = threading.Lock()
SHARED_DIR = os.path.expanduser("~/SharedFiles")
CLIP_IMG_DIR = os.path.join(SHARED_DIR, ".clipboard-images")
os.makedirs(SHARED_DIR, exist_ok=True)
os.makedirs(CLIP_IMG_DIR, exist_ok=True)

MAX_HISTORY = 50
MAX_CLIPBOARD_ENTRY_MB = 10  # max 10MB per clipboard entry
MAX_UPLOAD_MB = 20480  # 20GB
MAX_ZIP_EXTRACT_MB = 1000
MAX_ZIP_FILES = 5000
PREVIEW_CHARS = 500
SIZE_THRESHOLD_MB = 200
MAX_TOTAL_MEMORY_MB = 100

UTF8_ENV = {**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}

# --- Helpers ---
def get_mac_clipboard():
    try:
        r = subprocess.run(["pbpaste"], capture_output=True, timeout=5, env=UTF8_ENV)
        return r.stdout.decode("utf-8", errors="replace")
    except: return None

def set_mac_clipboard(text):
    """Set Mac clipboard using osascript (works from launchd background processes where pbcopy hangs)."""
    import tempfile
    tmp = None
    try:
        # Write to temp file to avoid shell quoting issues with arbitrary text
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
        tmp.write(text)
        tmp.close()
        r = subprocess.run(
            ["osascript", "-e", f'set the clipboard to (read POSIX file "{tmp.name}" as «class utf8»)'],
            capture_output=True, timeout=5, env=UTF8_ENV
        )
        return r.returncode == 0
    except Exception:
        return False
    finally:
        if tmp and os.path.exists(tmp.name):
            os.unlink(tmp.name)

def get_mac_clipboard_image():
    """Check if Mac clipboard has an image in any format, return PNG bytes or None.
    Tries formats in order: PNG, TIFF, JPEG, GIF, BMP. Converts non-PNG to PNG via sips."""
    # Format map: osascript class → (file extension, needs_conversion)
    IMAGE_FORMATS = [
        ("«class PNGf»",  "png",  False),
        ("TIFF picture",    "tiff", True),
        ("«class TIFF»",  "tiff", True),
        ("JPEG picture",    "jpg",  True),
        ("«class JPEG»",  "jpg",  True),
        ("GIF picture",     "gif",  True),
        ("«class GIFf»",  "gif",  True),
        ("«class BMP »",  "bmp",  True),
    ]
    try:
        # First: get clipboard info to see what formats are available
        r = subprocess.run(["osascript", "-e", "clipboard info"],
            capture_output=True, timeout=5, text=True)
        clip_info = r.stdout if r.returncode == 0 else ""
        if not clip_info:
            return None

        # Find the best available format
        chosen_class = None
        chosen_ext = None
        needs_convert = False
        for fmt_class, ext, convert in IMAGE_FORMATS:
            # Check if this format class appears in clipboard info
            if fmt_class in clip_info:
                chosen_class = fmt_class
                chosen_ext = ext
                needs_convert = convert
                break

        if not chosen_class:
            return None

        # Extract image data via osascript to a temp file
        tmp_native = os.path.join(CLIP_IMG_DIR, f"_temp_extract.{chosen_ext}")
        tmp_png = os.path.join(CLIP_IMG_DIR, "_temp_extract.png")

        r3 = subprocess.run(["osascript", "-e",
            f'set imgFile to POSIX file "{tmp_native}"\n'
            'try\n'
            f'  set imgData to the clipboard as {chosen_class}\n'
            '  set fRef to open for access imgFile with write permission\n'
            '  set eof fRef to 0\n'
            '  write imgData to fRef\n'
            '  close access fRef\n'
            '  return "OK"\n'
            'on error e\n'
            '  try\n    close access imgFile\n  end try\n'
            '  return "ERR:" & e\n'
            'end try'],
            capture_output=True, timeout=10, text=True)

        if "OK" not in r3.stdout or not os.path.exists(tmp_native) or os.path.getsize(tmp_native) < 8:
            for f in [tmp_native, tmp_png]:
                if os.path.exists(f):
                    try: os.remove(f)
                    except: pass
            return None

        # Convert to PNG if needed
        if needs_convert:
            rc = subprocess.run(["sips", "--setProperty", "format", "png",
                tmp_native, "--out", tmp_png],
                capture_output=True, timeout=15)
            if rc.returncode != 0 or not os.path.exists(tmp_png) or os.path.getsize(tmp_png) < 8:
                for f in [tmp_native, tmp_png]:
                    if os.path.exists(f):
                        try: os.remove(f)
                        except: pass
                return None
            source = tmp_png
        else:
            source = tmp_native

        with open(source, "rb") as f:
            data = f.read()

        # Cleanup temp files
        for f in [tmp_native, tmp_png]:
            if os.path.exists(f):
                try: os.remove(f)
                except: pass
        return data

    except Exception as e:
        return None

def set_mac_clipboard_image(image_path):
    """Set Mac clipboard to an image file. Converts to PNG first if needed."""
    try:
        # Check if already PNG by magic bytes
        with open(image_path, "rb") as f:
            header = f.read(8)
        is_png = header[:4] == b"\x89PNG"

        if not is_png:
            # Convert to PNG via sips
            png_tmp = image_path + ".clipboard.png"
            rc = subprocess.run(["sips", "--setProperty", "format", "png",
                image_path, "--out", png_tmp],
                capture_output=True, timeout=15)
            if rc.returncode == 0 and os.path.exists(png_tmp):
                image_path = png_tmp
            # If conversion fails, try setting clipboard with original anyway

        r = subprocess.run(["osascript", "-e",
            'set imgFile to POSIX file "' + image_path + '"\n'
            'set the clipboard to (read imgFile as «class PNGf»)'],
            capture_output=True, timeout=10, text=True)

        # Cleanup temp conversion file
        if not is_png:
            tmp = image_path
            if os.path.exists(tmp) and tmp.endswith(".clipboard.png"):
                try: os.remove(tmp)
                except: pass

        return "error" not in r.stderr.lower()
    except:
        return False

def image_hash(data):
    """Quick hash of image bytes to detect clipboard changes."""
    return hashlib.md5(data).hexdigest()[:12]

def total_clipboard_bytes():
    total = 0
    for e in clipboard_history:
        if e.get("type") == "image" and e.get("file"):
            fp = os.path.join(CLIP_IMG_DIR, e["file"])
            if os.path.exists(fp):
                total += os.path.getsize(fp)
        else:
            total += len(e.get("text", "").encode("utf-8", errors="replace"))
    return total

def enforce_memory_limit():
    limit = MAX_TOTAL_MEMORY_MB * 1024 * 1024
    while len(clipboard_history) > 1 and total_clipboard_bytes() > limit:
        old = clipboard_history.pop()
        # Clean up image file if it's an image entry
        if old.get("type") == "image" and old.get("file"):
            fp = os.path.join(CLIP_IMG_DIR, old["file"])
            if os.path.exists(fp):
                try: os.remove(fp)
                except: pass

def add_clipboard_entry(text=None, source="mac", entry_type="text", image_file=None, image_size=0):
    entry = {
        "id": str(uuid.uuid4())[:8],
        "type": entry_type,
        "time": datetime.now().strftime("%H:%M:%S"),
        "source": source,
    }
    if entry_type == "image":
        entry["file"] = image_file
        entry["length"] = image_size
        entry["text"] = None
    else:
        entry["text"] = text
        entry["length"] = len(text) if text else 0
        entry["file"] = None
    clipboard_history.insert(0, entry)
    if len(clipboard_history) > MAX_HISTORY:
        old = clipboard_history.pop()
        if old.get("type") == "image" and old.get("file"):
            fp = os.path.join(CLIP_IMG_DIR, old["file"])
            if os.path.exists(fp):
                try: os.remove(fp)
                except: pass
    enforce_memory_limit()
    return entry

def clipboard_watcher():
    last_text = None
    last_img_hash = None
    while True:
        try:
            # --- Check for image first (higher priority for screenshot workflows) ---
            img_data = get_mac_clipboard_image()
            if img_data:
                h = image_hash(img_data)
                if h != last_img_hash:
                    last_img_hash = h
                    # Check if this image is already the latest entry
                    with clipboard_lock:
                        already = clipboard_history[0] if clipboard_history else None
                        if not already or already.get("type") != "image" or already.get("_hash") != h:
                            fname = f"clip_{datetime.now().strftime('%H%M%S')}_{h}.png"
                            fpath = os.path.join(CLIP_IMG_DIR, fname)
                            with open(fpath, "wb") as f:
                                f.write(img_data)
                            entry = add_clipboard_entry(source="mac", entry_type="image",
                                                       image_file=fname, image_size=len(img_data))
                            entry["_hash"] = h
                    # Also update last_text so we don't re-capture the text representation
                    last_text = get_mac_clipboard()
                    time.sleep(0.5)
                    continue

            # --- Check for text (existing logic, unchanged) ---
            current = get_mac_clipboard()
            if current and current != last_text and current.strip():
                already = clipboard_history[0] if clipboard_history else None
                if not already or (already.get("type") == "text" and already.get("text") != current) or already.get("type") == "image":
                    last_text = current
                    with clipboard_lock:
                        add_clipboard_entry(current, "mac", "text")
                else:
                    last_text = current
        except: pass
        time.sleep(0.5)

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except: return "127.0.0.1"

def fmt_size(size):
    if size < 1024: return f"{size} B"
    elif size < 1048576: return f"{size/1024:.1f} KB"
    else: return f"{size/1048576:.1f} MB"

def get_files():
    items = []
    try:
        for f in os.listdir(SHARED_DIR):
            if f.startswith(".clipboard-images"): continue  # Hide internal dir
            fp = os.path.join(SHARED_DIR, f)
            if os.path.isfile(fp):
                stat = os.stat(fp)
                items.append({"name": f, "size": fmt_size(stat.st_size), "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%H:%M:%S"), "type": "file"})
            elif os.path.isdir(fp):
                try:
                    total = sum(os.path.getsize(os.path.join(dp, fn)) for dp, dn, fns in os.walk(fp) for fn in fns)
                except OSError:
                    total = 0
                items.append({"name": f, "size": fmt_size(total), "modified": datetime.fromtimestamp(os.stat(fp).st_mtime).strftime("%H:%M:%S"), "type": "dir"})
        items.sort(key=lambda x: (x["type"], x["name"]))
    except: pass
    return items

# --- HTML ---
HTML = r"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clipboard + File Share</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI',sans-serif;background:#1a1a2e;color:#eee;min-height:100vh;padding:20px}
.header{text-align:center;padding:15px 0}
.header h1{font-size:1.5em;color:#e94560}
.header p{color:#888;font-size:.85em;margin-top:5px}
.copyright{position:absolute;top:12px;left:16px;font-size:.7em;color:#555;font-weight:400}
.status{text-align:center;padding:8px;margin:10px auto;border-radius:8px;font-size:.85em;max-width:900px}
.connected{background:#0f3d0f;color:#4caf50}
.disconnected{background:#3d0f0f;color:#f44336}
.tabs{display:flex;justify-content:center;gap:10px;margin:15px 0}
.tab{padding:8px 24px;border-radius:8px;cursor:pointer;font-size:.9em;font-weight:600;border:1px solid #0f3460;background:#16213e;color:#888;transition:all .2s}
.tab.active{background:#e94560;color:#fff;border-color:#e94560}
.tab:hover{border-color:#e94560}
.panel{max-width:900px;margin:0 auto;display:none}
.panel.active{display:block}
.columns{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.column{min-height:200px}
.col-header{display:flex;align-items:center;gap:8px;padding:10px 14px;border-radius:10px 10px 0 0;font-weight:700;font-size:.9em;margin-bottom:0}
.col-header-mac{background:linear-gradient(135deg,#2d1b3d,#1a1a2e);border:1px solid #9b59b6;border-bottom:none;color:#bb86fc}
.col-header-win{background:linear-gradient(135deg,#1b2d3d,#1a1a2e);border:1px solid #2196f3;border-bottom:none;color:#64b5f6}
.col-header .icon{font-size:1.2em}
.col-body{border-radius:0 0 10px 10px;padding:12px;min-height:180px;overflow-y:auto;max-height:70vh}
.col-body-mac{background:#16132e;border:1px solid #9b59b6;border-top:none}
.col-body-win{background:#131a2e;border:1px solid #2196f3;border-top:none}
.entry{border-radius:8px;padding:12px;margin-bottom:10px;position:relative}
.entry-mac{background:#221840;border:1px solid #7c3aed55}
.entry-win{background:#182840;border:1px solid #2196f355}
.entry-time{font-size:.72em;color:#888;margin-bottom:6px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.entry-text{font-family:Consolas,'Courier New',monospace;font-size:.88em;white-space:pre-wrap;word-break:break-all;max-height:150px;overflow-y:auto;color:#ddd;line-height:1.5}
.entry-text.expanded{max-height:none}
.entry-img{max-width:100%;max-height:200px;border-radius:6px;cursor:pointer;transition:max-height .3s;border:1px solid #333}
.entry-img.expanded{max-height:none}
.entry-img-wrap{margin-top:6px;text-align:center}
.entry-actions{display:flex;gap:6px;position:absolute;top:10px;right:10px}
.act-btn{color:#fff;border:none;padding:4px 10px;border-radius:5px;cursor:pointer;font-size:.72em;font-weight:600;transition:all .2s}
.act-btn-mac{background:#9b59b6}.act-btn-mac:hover{background:#8e44ad}
.act-btn-win{background:#2196f3}.act-btn-win:hover{background:#1976d2}
.act-btn.copied{background:#4caf50!important}
.size-info{font-size:.65em;color:#666;background:#0002;padding:1px 6px;border-radius:8px}
.badge{display:inline-block;border-radius:10px;padding:1px 8px;font-size:.7em;font-weight:700;letter-spacing:.3px}
.badge-mac{background:#9b59b633;color:#bb86fc;border:1px solid #9b59b655}
.badge-win{background:#2196f333;color:#64b5f6;border:1px solid #2196f355}
.badge-latest{background:#e9456033;color:#e94560;border:1px solid #e9456055;margin-left:4px}
.badge-img{background:#ff980033;color:#ffb74d;border:1px solid #ff980055;margin-left:4px}
.badge-auto{background:#00bcd433;color:#00e5ff;border:1px solid #00bcd455;margin-left:4px}
.auto-sync-wrap{display:flex;align-items:center;gap:8px;margin-left:auto;font-size:.75em}
.auto-sync-label{color:#888;font-weight:400;user-select:none}
.auto-sync-label.on{color:#00e5ff}
.toggle{position:relative;width:36px;height:20px;cursor:pointer}
.toggle input{opacity:0;width:0;height:0}
.toggle .slider{position:absolute;inset:0;background:#333;border-radius:20px;transition:all .3s}
.toggle .slider:before{content:'';position:absolute;height:14px;width:14px;left:3px;bottom:3px;background:#888;border-radius:50%;transition:all .3s}
.toggle input:checked+.slider{background:#00bcd4;box-shadow:0 0 8px #00bcd455}
.toggle input:checked+.slider:before{transform:translateX(16px);background:#fff}
.auto-sync-status{font-size:.7em;color:#555;min-width:60px;text-align:right}
.input-area{margin-bottom:14px}
.input-area textarea{width:100%;background:#0d1117;border:1px solid #333;border-radius:8px;color:#ddd;padding:10px;font-family:Consolas,'Courier New',monospace;font-size:.88em;resize:vertical;min-height:70px;outline:none;transition:border .2s}
.input-area textarea:focus{border-color:#2196f3}
.paste-hint{font-size:.7em;color:#555;margin-top:4px;text-align:center}
.send-btn{width:100%;margin-top:6px;padding:9px;border-radius:8px;border:none;font-weight:700;font-size:.85em;cursor:pointer;transition:all .2s}
.send-btn-clip{background:#2196f3;color:#fff}.send-btn-clip:hover{background:#1976d2}
.send-btn-clip:active{transform:scale(.98)}.send-btn.sent{background:#4caf50!important}
.file-row{border-radius:8px;padding:10px 12px;margin-bottom:8px;display:flex;align-items:center;gap:10px}
.file-row-mac{background:#221840;border:1px solid #7c3aed55}
.file-info{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.file-name{font-weight:600;color:#ddd;font-size:.88em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file-name .f-icon{display:inline-block;width:1.2em;text-align:center;font-size:1em;vertical-align:middle;flex-shrink:0}
.file-meta{font-size:.7em;color:#888;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file-actions{display:flex;align-items:center;gap:5px;flex-shrink:0}
.dl-btn{color:#fff;border:none;padding:5px 0;border-radius:6px;cursor:pointer;font-size:.75em;font-weight:600;text-decoration:none;transition:all .2s;min-width:52px;text-align:center;display:inline-block}
.dl-btn-mac{background:#9b59b6}.dl-btn-mac:hover{background:#8e44ad}
.del-btn{background:#c0392b;color:#fff;border:none;padding:5px 0;border-radius:6px;cursor:pointer;font-size:.75em;font-weight:600;transition:all .2s;min-width:36px;text-align:center}.del-btn:hover{background:#e74c3c}.del-btn.deleting{background:#888;pointer-events:none}
.upload-zone{border:2px dashed #2196f355;border-radius:10px;padding:24px;text-align:center;cursor:pointer;transition:all .2s;background:#131a2e;margin-bottom:12px}
.upload-zone:hover,.upload-zone.dragover{border-color:#2196f3;background:#182840}
.upload-zone .icon{font-size:2em;margin-bottom:6px}
.upload-zone p{color:#888;font-size:.85em}.upload-zone p strong{color:#64b5f6}
.upload-progress{display:none;margin-top:8px;padding:10px;background:#0d1117;border-radius:8px;font-size:.8em;color:#888}
.upload-progress.active{display:block}.upload-progress.success{color:#4caf50}.upload-progress.error{color:#f44336}
.prog-strategy{font-size:.7em;color:#64b5f6;margin-bottom:6px;font-weight:600}
.prog-file{color:#ddd;margin-bottom:4px;font-size:.82em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prog-bar-wrap{background:#1a1a2e;border-radius:4px;height:8px;overflow:hidden;margin:6px 0}
.prog-bar{height:100%;background:linear-gradient(90deg,#2196f3,#64b5f6);border-radius:4px;transition:width .3s;width:0%}
.prog-bar.done{background:linear-gradient(90deg,#4caf50,#81c784)}
.prog-stats{display:flex;justify-content:space-between;font-size:.7em;color:#666;margin-top:2px}
.prog-summary{margin-top:8px;padding-top:6px;border-top:1px solid #333;font-size:.75em}
.empty{text-align:center;color:#555;padding:30px;font-size:.95em}
.img-upload-status{padding:8px;margin-top:6px;border-radius:6px;font-size:.8em;text-align:center;display:none}
.img-upload-status.active{display:block}
.img-upload-status.ok{background:#0f3d0f;color:#4caf50}
.img-upload-status.err{background:#3d0f0f;color:#f44336}
@media(max-width:700px){.columns{grid-template-columns:1fr}}
</style><script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script></head><body>
<div class="copyright">&copy; Thomas Willner 2026</div>
<div class="header"><h1>&#128268; Clipboard + File Share</h1><p>Bidirectional sharing between Mac &amp; Windows &mdash; text &amp; images (PNG, JPEG, TIFF, GIF, BMP)</p></div>
<div id="status" class="status connected">&#9679; Connected</div>
<div class="tabs"><div class="tab active" onclick="switchTab('clipboard')">&#128203; Clipboard</div><div class="tab" onclick="switchTab('files')">&#128193; Files</div></div>
<div class="panel active" id="panel-clipboard"><div class="columns">
<div class="column"><div class="col-header col-header-mac"><span class="icon">&#63743;</span> From Mac</div><div class="col-body col-body-mac" id="clip-mac"><div class="empty">Waiting for clipboard from Mac...</div></div></div>
<div class="column"><div class="col-header col-header-win"><span class="icon">&#128187;</span> To Mac<div class="auto-sync-wrap"><span class="auto-sync-label" id="sync-label">Auto-sync</span><label class="toggle"><input type="checkbox" id="auto-sync-toggle" onchange="toggleAutoSync(this.checked)"><span class="slider"></span></label><span class="auto-sync-status" id="sync-status"></span></div></div><div class="col-body col-body-win" id="clip-win"><div class="input-area"><textarea id="win-clip-input" placeholder="Paste text or images here to send to Mac..."></textarea><button class="send-btn send-btn-clip" id="send-clip-btn" onclick="sendClipboard()">&#9654; Send to Mac Clipboard</button><p class="paste-hint">&#128248; Tip: Ctrl+V an image here to send it to Mac clipboard</p></div><div class="img-upload-status" id="img-status"></div><div id="clip-win-history"></div></div></div>
</div></div>
<div class="panel" id="panel-files"><div class="columns">
<div class="column"><div class="col-header col-header-mac"><span class="icon">&#63743;</span> From Mac</div><div class="col-body col-body-mac" id="files-mac"><div class="empty">No files in ~/SharedFiles</div></div></div>
<div class="column"><div class="col-header col-header-win"><span class="icon">&#128187;</span> To Mac</div><div class="col-body col-body-win" id="files-win"><div class="upload-zone" id="upload-zone"><div class="icon">&#128228;</div><p>Drag &amp; drop <strong>files or folders</strong> here</p><div style="display:flex;gap:8px;justify-content:center;margin-top:10px"><button class="act-btn act-btn-win" onclick="event.stopPropagation();document.getElementById('file-input').click()" style="padding:6px 16px;font-size:.82em">&#128196; Select Files</button><button class="act-btn act-btn-win" onclick="event.stopPropagation();document.getElementById('folder-input').click()" style="padding:6px 16px;font-size:.82em">&#128193; Select Folder</button></div><input type="file" id="file-input" multiple style="display:none" onchange="handleFileSelect(this.files)"><input type="file" id="folder-input" webkitdirectory multiple style="display:none" onchange="handleFolderSelect(this.files)"></div><div class="upload-progress" id="upload-progress"><div class="prog-strategy" id="prog-strategy"></div><div class="prog-file" id="prog-file"></div><div class="prog-bar-wrap"><div class="prog-bar" id="prog-bar"></div></div><div class="prog-stats"><span id="prog-detail"></span><span id="prog-speed"></span></div><div class="prog-summary" id="prog-summary"></div></div><div id="files-win-history"></div></div></div>
</div></div><script>
/* --- Globals & helpers --- */
const fullTextCache={};
function switchTab(t){document.querySelectorAll('.tab').forEach(e=>e.classList.remove('active'));document.querySelectorAll('.panel').forEach(e=>e.classList.remove('active'));document.getElementById('panel-'+t).classList.add('active');document.querySelector('.tab[onclick*="'+t+'"]').classList.add('active')}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function fmtLen(n){if(n<1000)return n+' chars';if(n<1000000)return (n/1000).toFixed(1)+'K chars';return (n/1000000).toFixed(1)+'M chars'}
function fmtImgSize(n){if(n<1024)return n+' B';if(n<1048576)return (n/1024).toFixed(1)+' KB';return (n/1048576).toFixed(1)+' MB'}

/* --- Build entry HTML --- */
function entryHTML(e,src,isLatest){
  const cls=src==='mac'?'mac':'win';
  const badge=src==='mac'?'<span class="badge badge-mac">&#63743; MAC</span>':'<span class="badge badge-win">&#128187; WIN</span>';
  const autoBadge=(e.auto)?'<span class="badge badge-auto">&#9889; AUTO</span>':'';
  const latestBadge=isLatest?'<span class="badge badge-latest">LATEST</span>':'';

  if(e.type==='image'){
    const imgBadge='<span class="badge badge-img">&#128248; IMG</span>';
    const sizeTag='<span class="size-info">'+fmtImgSize(e.length)+'</span>';
    let actions='<div class="entry-actions">';
    actions+='<button class="act-btn act-btn-'+cls+'" onclick="copyImage(\''+e.file+'\',this)">Copy</button>';
    actions+='<button class="act-btn act-btn-'+cls+'" onclick="downloadClipImg(\''+e.file+'\')">Save</button>';
    actions+='</div>';
    return '<div class="entry entry-'+cls+'" id="entry-'+e.id+'"><div class="entry-time">'+badge+' '+e.time+' '+imgBadge+' '+latestBadge+' '+sizeTag+'</div><div class="entry-img-wrap"><img class="entry-img" src="/api/clipboard-image/'+encodeURIComponent(e.file)+'" onclick="this.classList.toggle(\'expanded\')" title="Click to expand/collapse"></div>'+actions+'</div>';
  }

  /* Text entry (existing logic, unchanged) */
  const truncated=e.truncated;
  const preview=esc(e.preview||e.text||'');
  const sizeTag=truncated?'<span class="size-info">'+fmtLen(e.length)+' total</span>':'';

  let actions='<div class="entry-actions">';
  if(truncated){
    actions+='<button class="act-btn act-btn-'+cls+'" onclick="showFull(\''+e.id+'\',\''+cls+'\',this)">Show Full</button>';
  }
  actions+='<button class="act-btn act-btn-'+cls+'" onclick="copyEntry(\''+e.id+'\',\''+cls+'\',this)">Copy</button>';
  actions+='</div>';

  return '<div class="entry entry-'+cls+'" id="entry-'+e.id+'"><div class="entry-time">'+badge+' '+e.time+' '+autoBadge+' '+latestBadge+' '+sizeTag+'</div><div class="entry-text" id="text-'+e.id+'">'+preview+(truncated?'\n\n<span style="color:#888;font-style:italic">... truncated (click Show Full)</span>':'')+'</div>'+actions+'</div>';
}

/* --- Copy image to Windows clipboard --- */
async function copyImage(filename,btn){
  btn.textContent='...';btn.disabled=true;
  try{
    const r=await fetch('/api/clipboard-image/'+encodeURIComponent(filename));
    const blob=await r.blob();
    const pngBlob=new Blob([blob],{type:'image/png'});
    await navigator.clipboard.write([new ClipboardItem({'image/png':pngBlob})]);
    btn.textContent='Copied!';btn.classList.add('copied');
    setTimeout(()=>{btn.textContent='Copy';btn.classList.remove('copied');btn.disabled=false},1500);
  }catch(e){
    btn.textContent='Failed';
    setTimeout(()=>{btn.textContent='Copy';btn.disabled=false},1500);
  }
}

/* --- Download clipboard image --- */
function downloadClipImg(filename){
  const a=document.createElement('a');
  a.href='/api/clipboard-image/'+encodeURIComponent(filename);
  a.download=filename;
  a.click();
}

/* --- Fetch full text on demand --- */
async function showFull(id,cls,btn){
  btn.textContent='Loading...';btn.disabled=true;
  try{
    if(fullTextCache[id]){renderFull(id,fullTextCache[id],cls,btn);return}
    const r=await fetch('/api/clipboard/'+id);
    const d=await r.json();
    if(d.ok){fullTextCache[id]=d.text;renderFull(id,d.text,cls,btn)}
    else{btn.textContent='Error';setTimeout(()=>{btn.textContent='Show Full';btn.disabled=false},1500)}
  }catch(e){btn.textContent='Error';setTimeout(()=>{btn.textContent='Show Full';btn.disabled=false},1500)}
}
function renderFull(id,text,cls,btn){
  const el=document.getElementById('text-'+id);
  if(el){el.innerHTML=esc(text);el.classList.add('expanded')}
  btn.textContent='Collapse';btn.disabled=false;
  btn.onclick=function(){el.classList.remove('expanded');btn.textContent='Show Full';btn.onclick=function(){showFull(id,cls,btn)}};
}

/* --- Copy text entry --- */
async function copyEntry(id,cls,btn){
  btn.textContent='...';btn.disabled=true;
  let text=fullTextCache[id];
  if(!text){try{const r=await fetch('/api/clipboard/'+id);const d=await r.json();if(d.ok){text=d.text;fullTextCache[id]=text}}catch(e){}}
  if(!text){text=document.getElementById('text-'+id)?.textContent||''}
  try{await navigator.clipboard.writeText(text)}catch(e){const ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();document.execCommand('copy');document.body.removeChild(ta)}
  btn.textContent='Copied!';btn.classList.add('copied');
  setTimeout(()=>{btn.textContent='Copy';btn.classList.remove('copied');btn.disabled=false},1500);
}

/* --- Clipboard polling (preview-based) --- */
let lastMacHash='',lastWinHash='';
async function pollClipboard(){
  try{
    const r=await fetch('/api/clipboard');
    const d=await r.json();
    document.getElementById('status').className='status connected';
    document.getElementById('status').innerHTML='&#9679; Connected';
    const macEntries=d.entries.filter(e=>e.source==='mac');
    const winEntries=d.entries.filter(e=>e.source==='windows');
    const macHash=macEntries.map(e=>e.id).join(',');
    const winHash=winEntries.map(e=>e.id).join(',');
    const macEl=document.getElementById('clip-mac');
    if(macEntries.length===0){macEl.innerHTML='<div class="empty">Waiting for clipboard from Mac...</div>'}
    else if(macHash!==lastMacHash){lastMacHash=macHash;macEl.innerHTML=macEntries.map((e,i)=>entryHTML(e,'mac',i===0)).join('')}
    const winHist=document.getElementById('clip-win-history');
    if(winHash!==lastWinHash){lastWinHash=winHash;
      if(winEntries.length===0){winHist.innerHTML=''}
      else{winHist.innerHTML='<div style="font-size:.75em;color:#64b5f6;margin:8px 0 6px;font-weight:600">&#128203; Sent History</div>'+winEntries.map((e,i)=>entryHTML(e,'windows',false)).join('')}
    }
  }catch(e){document.getElementById('status').className='status disconnected';document.getElementById('status').innerHTML='&#9679; Disconnected'}
}

/* --- Send text clipboard Win->Mac --- */
async function sendClipboard(){
  const ta=document.getElementById('win-clip-input');
  const text=ta.value;
  if(!text.trim())return;
  const btn=document.getElementById('send-clip-btn');
  const len=text.length;
  btn.textContent='Sending '+(len>1000?fmtLen(len):'')+'...';btn.disabled=true;
  try{
    const r=await fetch('/api/clipboard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
    const d=await r.json();
    if(d.ok){btn.innerHTML='&#10003; Sent!';btn.classList.add('sent');ta.value='';setTimeout(()=>{btn.innerHTML='&#9654; Send to Mac Clipboard';btn.classList.remove('sent');btn.disabled=false},1500);pollClipboard()}
    else{btn.textContent='Failed: '+(d.error||'unknown');setTimeout(()=>{btn.innerHTML='&#9654; Send to Mac Clipboard';btn.disabled=false},2000)}
  }catch(e){btn.textContent='Error!';setTimeout(()=>{btn.innerHTML='&#9654; Send to Mac Clipboard';btn.disabled=false},1500)}
}

/* --- Image paste handler (Win->Mac) --- */
document.getElementById('win-clip-input').addEventListener('paste',async function(ev){
  const items=ev.clipboardData&&ev.clipboardData.items;
  if(!items)return;
  for(let i=0;i<items.length;i++){
    if(items[i].type.indexOf('image/')===0){
      ev.preventDefault();
      const blob=items[i].getAsFile();
      if(!blob)return;
      const statusEl=document.getElementById('img-status');
      statusEl.className='img-upload-status active';
      statusEl.textContent='Uploading image ('+fmtImgSize(blob.size)+')...';
      statusEl.style.color='#64b5f6';statusEl.style.background='#131a2e';
      try{
        const fd=new FormData();
        fd.append('image',blob,'clipboard.png');
        const r=await fetch('/api/clipboard-image',{method:'POST',body:fd});
        const d=await r.json();
        if(d.ok){
          statusEl.className='img-upload-status active ok';
          statusEl.textContent='&#10003; Image sent to Mac clipboard!';
          setTimeout(()=>{statusEl.className='img-upload-status'},3000);
          pollClipboard();
        }else{
          statusEl.className='img-upload-status active err';
          statusEl.textContent='Failed: '+(d.error||'unknown');
          setTimeout(()=>{statusEl.className='img-upload-status'},4000);
        }
      }catch(e){
        statusEl.className='img-upload-status active err';
        statusEl.textContent='Error: '+e.message;
        setTimeout(()=>{statusEl.className='img-upload-status'},4000);
      }
      return;
    }
  }
});

/* --- File polling --- */
async function pollFiles(){try{const r=await fetch('/api/files');const d=await r.json();const el=document.getElementById('files-mac');if(d.files.length===0){el.innerHTML='<div class="empty">No files in ~/SharedFiles</div>'}else{el.innerHTML=d.files.map(f=>{const n=esc(f.name);const en=encodeURIComponent(f.name);const icon=f.type==='dir'?'&#128193;':'&#128196;';const dlBtn=f.type==='dir'?'<a class="dl-btn dl-btn-mac" href="/files-zip/'+en+'" download>&#11015; ZIP</a>':'<a class="dl-btn dl-btn-mac" href="/files/'+en+'" download>&#11015; DL</a>';return '<div class="file-row file-row-mac"><div class="file-info"><span class="file-name" title="'+n+'"><span class="f-icon">'+icon+'</span> '+n+(f.type==='dir'?'/':'')+'</span><span class="file-meta">'+f.size+' &bull; '+f.modified+(f.type==='dir'?' &bull; folder':'')+'</span></div><div class="file-actions">'+dlBtn+'<button class="del-btn" onclick="deleteItem(\''+f.name.replace(/'/g,"\\'")+'\',\''+f.type+'\',this)">&#128465;</button></div></div>'}).join('')}}catch(e){}}

/* --- Upload engine (unchanged) --- */
const THRESHOLD=200*1024*1024;
const uploadZone=document.getElementById('upload-zone');
uploadZone.addEventListener('dragover',e=>{e.preventDefault();uploadZone.classList.add('dragover')});
uploadZone.addEventListener('dragleave',()=>uploadZone.classList.remove('dragover'));
uploadZone.addEventListener('drop',async e=>{
  e.preventDefault();uploadZone.classList.remove('dragover');
  const items=e.dataTransfer.items;
  let hasDir=false;
  if(items&&items.length>0){for(let i=0;i<items.length;i++){const entry=items[i].webkitGetAsEntry&&items[i].webkitGetAsEntry();if(entry&&entry.isDirectory){hasDir=true;break}}}
  if(hasDir){
    const allFiles=[];
    async function readEntry(entry,path){return new Promise((resolve,reject)=>{if(entry.isFile){entry.file(f=>{f._relPath=path+f.name;allFiles.push(f);resolve()},reject)}else if(entry.isDirectory){const reader=entry.createReader();const readAll=[];function readBatch(){reader.readEntries(async entries=>{if(entries.length===0){await Promise.all(readAll);resolve();return}for(const sub of entries)readAll.push(readEntry(sub,path+entry.name+'/'));readBatch()},reject)}readBatch()}else resolve()})}
    for(let i=0;i<items.length;i++){const entry=items[i].webkitGetAsEntry&&items[i].webkitGetAsEntry();if(entry)await readEntry(entry,'')}
    await smartUpload(allFiles,true);
  }else{const files=Array.from(e.dataTransfer.files);files.forEach(f=>{f._relPath=f.name});await smartUpload(files,false)}
});
function handleFileSelect(inputFiles){const files=Array.from(inputFiles);files.forEach(f=>{f._relPath=f.name});smartUpload(files,false)}
function handleFolderSelect(inputFiles){const files=Array.from(inputFiles);files.forEach(f=>{f._relPath=f.webkitRelativePath||f.name});smartUpload(files,true)}
function fmtBytes(b){if(b<1024)return b+' B';if(b<1048576)return (b/1024).toFixed(1)+' KB';if(b<1073741824)return (b/1048576).toFixed(1)+' MB';return (b/1073741824).toFixed(2)+' GB'}
function progUI(show){const el=document.getElementById('upload-progress');if(show){el.className='upload-progress active'}else{setTimeout(()=>{el.className='upload-progress'},4000)}}
function setStrategy(txt){document.getElementById('prog-strategy').textContent=txt}
function setFile(txt){document.getElementById('prog-file').textContent=txt}
function setBar(pct){const b=document.getElementById('prog-bar');b.style.width=pct+'%';if(pct>=100)b.classList.add('done');else b.classList.remove('done')}
function setDetail(txt){document.getElementById('prog-detail').textContent=txt}
function setSpeed(txt){document.getElementById('prog-speed').textContent=txt}
function setSummary(txt,ok){const el=document.getElementById('prog-summary');el.textContent=txt;el.style.color=ok?'#4caf50':'#f44336'}
async function smartUpload(files,hasPaths){
  if(!files||files.length===0)return;const totalSize=files.reduce((s,f)=>s+f.size,0);progUI(true);setBar(0);setSummary('','');setSpeed('');
  if(totalSize<THRESHOLD&&hasPaths){setStrategy('\u26A1 Strategy: ZIP ('+fmtBytes(totalSize)+' < 200MB threshold)');await zipUpload(files)}
  else if(totalSize<THRESHOLD&&!hasPaths){setStrategy('\u26A1 Strategy: Direct upload ('+fmtBytes(totalSize)+' < 200MB threshold)');await directUpload(files,false)}
  else{setStrategy('\U0001F680 Strategy: Direct file-by-file ('+fmtBytes(totalSize)+' > 200MB threshold)');await directUpload(files,hasPaths)}
  pollFiles();
}
async function zipUpload(files){
  setFile('Zipping '+files.length+' files...');
  try{const zip=new JSZip();for(const f of files)zip.file(f._relPath||f.name,f);const blob=await zip.generateAsync({type:'blob',compression:'DEFLATE'},meta=>{if(meta.currentFile){setFile('Compressing: '+meta.currentFile)}setBar(Math.round(meta.percent));setDetail(Math.round(meta.percent)+'% compressed')});
  setFile('Uploading zip ('+fmtBytes(blob.size)+')...');setBar(0);const fd=new FormData();const folderName=(files[0]._relPath||'').split('/')[0]||'upload';fd.append('file',blob,folderName+'.zip');await xhrUpload('/api/upload-folder',fd,files.length)}
  catch(e){setSummary('Error: '+e.message,false);progUI(false)}
}
async function directUpload(files,usePaths){
  const total=files.length;const totalSize=files.reduce((s,f)=>s+f.size,0);let bytesSent=0,success=0,failed=0;const startTime=Date.now();
  for(let i=0;i<files.length;i++){const f=files[i];const name=f._relPath||f.name;setFile('['+(i+1)+'/'+total+'] '+name+' ('+fmtBytes(f.size)+')');const fd=new FormData();
  if(usePaths){fd.append('file',f,f.name);fd.append('path',name);var url='/api/upload-with-path'}else{fd.append('file',f);var url='/api/upload'}
  try{await new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST',url);xhr.upload.onprogress=ev=>{if(ev.lengthComputable){const overallPct=((bytesSent+ev.loaded)/totalSize)*100;setBar(Math.round(overallPct));setDetail(fmtBytes(bytesSent+ev.loaded)+' / '+fmtBytes(totalSize)+' ('+Math.round(overallPct)+'%)');const elapsed=(Date.now()-startTime)/1000;const rate=(bytesSent+ev.loaded)/elapsed;if(rate>0){const remaining=(totalSize-bytesSent-ev.loaded)/rate;setSpeed(fmtBytes(rate)+'/s \u2022 ~'+Math.ceil(remaining)+'s left')}}};
  xhr.onload=()=>{if(xhr.status===200){try{const d=JSON.parse(xhr.responseText);if(d.ok){success++;resolve()}else{failed++;resolve()}}catch(e){failed++;resolve()}}else{failed++;resolve()}};xhr.onerror=()=>{failed++;resolve()};xhr.send(fd)});bytesSent+=f.size}catch(e){failed++}}
  setBar(100);if(failed===0){setSummary('\u2713 '+success+' file(s) uploaded successfully ('+fmtBytes(totalSize)+')',true);document.getElementById('upload-progress').classList.add('success')}else{setSummary(success+' succeeded, '+failed+' failed',false);document.getElementById('upload-progress').classList.add('error')}progUI(false);
}
function xhrUpload(url,formData,fileCount){
  return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();const startTime=Date.now();xhr.open('POST',url);xhr.upload.onprogress=ev=>{if(ev.lengthComputable){const pct=(ev.loaded/ev.total)*100;setBar(Math.round(pct));setDetail(fmtBytes(ev.loaded)+' / '+fmtBytes(ev.total)+' ('+Math.round(pct)+'%)');const elapsed=(Date.now()-startTime)/1000;const rate=ev.loaded/elapsed;if(rate>0){const remaining=(ev.total-ev.loaded)/rate;setSpeed(fmtBytes(rate)+'/s \u2022 ~'+Math.ceil(remaining)+'s left')}}};
  xhr.onload=()=>{try{const d=JSON.parse(xhr.responseText);if(d.ok){setBar(100);setSummary('\u2713 '+fileCount+' files uploaded via ZIP ('+d.extracted+' extracted)',true);document.getElementById('upload-progress').classList.add('success')}else{setSummary('Failed: '+(d.error||'unknown'),false);document.getElementById('upload-progress').classList.add('error')}}catch(e){setSummary('Parse error',false)}progUI(false);resolve()};
  xhr.onerror=()=>{setSummary('Network error',false);progUI(false);resolve()};xhr.send(formData)});
}
async function deleteItem(name,type,btn){
  const label=type==='dir'?'folder "'+name+'" and ALL its contents':'file "'+name+'"';if(!confirm('Delete '+label+'?\n\nThis cannot be undone.'))return;
  btn.classList.add('deleting');btn.textContent='...';
  try{const r=await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});const d=await r.json();if(d.ok){pollFiles()}else{alert('Delete failed: '+(d.error||'unknown'));btn.classList.remove('deleting');btn.innerHTML='&#128465;'}}
  catch(e){alert('Delete error: '+e.message);btn.classList.remove('deleting');btn.innerHTML='&#128465;'}
}
document.getElementById('win-clip-input').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();sendClipboard()}});

/* --- Auto-sync: Win→Mac clipboard push (toggle-able) --- */
/* Three modes (auto-detected, best available):
   1. clipboardchange event (Chrome 143+, secure context) — instant
   2. Clipboard API polling (secure context, older browsers) — 1.5s intervals
   3. Paste capture (ANY browser, ANY context) — Ctrl+V anywhere on page auto-sends
   Mode 3 always works on plain HTTP with zero setup. */
let lastAutoPushed='';
let autoSyncOn=false;
let autoSyncTimer=null;
let autoPushNotice=null;
let syncCount=0;
let autoSyncMode='';
function showAutoPush(text,ok){
  if(!autoPushNotice){autoPushNotice=document.createElement('div');autoPushNotice.style.cssText='position:fixed;bottom:20px;right:20px;padding:10px 18px;border-radius:10px;font-size:.82em;font-weight:600;z-index:999;transition:opacity .5s;pointer-events:none';document.body.appendChild(autoPushNotice)}
  autoPushNotice.style.background=ok?'#0f3d0f':'#3d0f0f';autoPushNotice.style.color=ok?'#4caf50':'#f44336';autoPushNotice.style.border=ok?'1px solid #4caf50':'1px solid #f44336';
  autoPushNotice.textContent=text;autoPushNotice.style.opacity='1';
  setTimeout(()=>{autoPushNotice.style.opacity='0'},2500);
}
function updateSyncStatus(msg){document.getElementById('sync-status').textContent=msg}
async function pushTextToMac(text,source){
  if(!autoSyncOn||!text||!text.trim())return;
  if(text===lastAutoPushed){return}
  /* Echo loop prevention */
  const entries=document.querySelectorAll('.entry-text');
  for(const el of entries){if(el.textContent.trim()===text.trim()){lastAutoPushed=text;return}}
  lastAutoPushed=text;
  updateSyncStatus('pushing...');
  try{
    const r=await fetch('/api/clipboard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,auto:true})});
    const d=await r.json();
    if(d.ok){syncCount++;showAutoPush('\u26A1 Auto-synced to Mac ('+fmtLen(text.length)+')',true);updateSyncStatus('\u2713 '+syncCount+' synced');pollClipboard()}
    else{showAutoPush('Sync failed: '+(d.error||'unknown'),false);updateSyncStatus('error')}
  }catch(e){updateSyncStatus('error')}
}
/* Paste event handler — works on ANY page, no permissions, no secure context */
function onPasteCapture(ev){
  if(!autoSyncOn)return;
  const text=ev.clipboardData?.getData('text/plain');
  if(text&&text.trim()){
    pushTextToMac(text,'paste');
    /* Don't preventDefault — let the textarea still receive the paste normally */
  }
}
/* Clipboard API handlers (only used in secure context) */
async function readAndPush(source){
  if(!autoSyncOn)return;
  try{
    const text=await navigator.clipboard.readText();
    if(text)pushTextToMac(text,source);
    else updateSyncStatus('watching');
  }catch(e){
    if(e.name==='NotAllowedError')updateSyncStatus('grant permission');
    else updateSyncStatus('watching');
  }
}
function onClipboardChange(ev){readAndPush('event')}
function onFocusSync(){readAndPush('focus')}
function onPollSync(){if(document.hasFocus())readAndPush('poll')}
function detectBestMode(){
  if(window.isSecureContext&&navigator.clipboard&&typeof navigator.clipboard.readText==='function'){
    const hasCCE=navigator.clipboard&&('onclipboardchange' in navigator.clipboard);
    return hasCCE?'instant':'clipboard';
  }
  return 'paste';
}
function toggleAutoSync(on){
  autoSyncOn=on;
  const label=document.getElementById('sync-label');
  if(on){
    autoSyncMode=detectBestMode();
    label.classList.add('on');
    syncCount=0;
    /* Always enable paste capture — works everywhere */
    document.addEventListener('paste',onPasteCapture,true);
    if(autoSyncMode==='instant'){
      label.textContent='Auto-sync ON \u26A1';
      updateSyncStatus('instant mode');
      navigator.clipboard.addEventListener('clipboardchange',onClipboardChange);
      window.addEventListener('focus',onFocusSync);
      readAndPush('init');
    }else if(autoSyncMode==='clipboard'){
      label.textContent='Auto-sync ON';
      updateSyncStatus('polling mode');
      autoSyncTimer=setInterval(onPollSync,1500);
      window.addEventListener('focus',onFocusSync);
      readAndPush('init');
    }else{
      label.textContent='Auto-sync ON';
      updateSyncStatus('Ctrl+V to sync');
      showAutoPush('Auto-sync ON \u2014 press Ctrl+V on this page to push clipboard to Mac',true);
    }
  }else{
    label.classList.remove('on');label.textContent='Auto-sync';
    updateSyncStatus('');
    document.removeEventListener('paste',onPasteCapture,true);
    if(navigator.clipboard&&('onclipboardchange' in navigator.clipboard)){navigator.clipboard.removeEventListener('clipboardchange',onClipboardChange)}
    if(autoSyncTimer){clearInterval(autoSyncTimer);autoSyncTimer=null}
    window.removeEventListener('focus',onFocusSync);
  }
}
setInterval(pollClipboard,1000);setInterval(pollFiles,3000);pollClipboard();pollFiles();
</script></body></html>"""

# --- Routes ---
@app.errorhandler(413)
def too_large(e):
    return jsonify({"ok": False, "error": f"File too large (max {MAX_UPLOAD_MB}MB)"}), 413

@app.route("/")
def index(): return render_template_string(HTML)

@app.route("/api/clipboard", methods=["GET", "POST"])
def api_clipboard():
    if request.method == "GET":
        with clipboard_lock:
            entries = []
            for e in clipboard_history:
                if e.get("type") == "image":
                    entries.append({
                        "id": e["id"],
                        "type": "image",
                        "file": e.get("file"),
                        "length": e.get("length", 0),
                        "time": e["time"],
                        "source": e["source"],
                        "truncated": False
                    })
                else:
                    text = e.get("text", "")
                    truncated = len(text) > PREVIEW_CHARS
                    entries.append({
                        "id": e["id"],
                        "type": "text",
                        "preview": text[:PREVIEW_CHARS] if truncated else text,
                        "text": None if truncated else text,
                        "truncated": truncated,
                        "length": e.get("length", 0),
                        "time": e["time"],
                        "source": e["source"],
                        "auto": e.get("auto", False)
                    })
        return jsonify({"entries": entries})
    else:
        data = request.get_json(silent=True)
        if not data or not data.get("text", "").strip():
            return jsonify({"ok": False, "error": "No text provided"}), 400
        text = data["text"]
        if len(text.encode("utf-8", errors="replace")) > MAX_CLIPBOARD_ENTRY_MB * 1024 * 1024:
            return jsonify({"ok": False, "error": f"Text too large (max {MAX_CLIPBOARD_ENTRY_MB}MB per entry)"}), 400
        is_auto = data.get("auto", False)
        ok = set_mac_clipboard(text)
        if ok:
            with clipboard_lock:
                entry = add_clipboard_entry(text, "windows", "text")
                if is_auto:
                    entry["auto"] = True
        return jsonify({"ok": ok})

@app.route("/api/clipboard/<entry_id>", methods=["GET"])
def api_clipboard_full(entry_id):
    with clipboard_lock:
        for e in clipboard_history:
            if e["id"] == entry_id:
                if e.get("type") == "image":
                    return jsonify({"ok": True, "type": "image", "file": e.get("file"), "length": e.get("length", 0)})
                return jsonify({"ok": True, "text": e.get("text", ""), "length": e.get("length", 0)})
    return jsonify({"ok": False, "error": "Entry not found"}), 404

@app.route("/api/clipboard-image/<filename>", methods=["GET"])
def api_clipboard_image_get(filename):
    """Serve a clipboard image."""
    safe = os.path.basename(filename)
    fpath = os.path.join(CLIP_IMG_DIR, safe)
    if not os.path.exists(fpath):
        return jsonify({"ok": False, "error": "Image not found"}), 404
    if not os.path.realpath(fpath).startswith(os.path.realpath(CLIP_IMG_DIR)):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    return send_file(fpath, mimetype="image/png")

@app.route("/api/clipboard-image", methods=["POST"])
def api_clipboard_image_post():
    """Receive an image from Windows browser, save it, set Mac clipboard."""
    if "image" not in request.files:
        return jsonify({"ok": False, "error": "No image provided"}), 400
    f = request.files["image"]
    # Save to clipboard-images dir with original extension
    orig_name = f.filename or "clipboard.png"
    orig_ext = os.path.splitext(orig_name)[1].lower() or ".png"
    ts = datetime.now().strftime('%H%M%S')
    uid = str(uuid.uuid4())[:6]
    tmp_path = os.path.join(CLIP_IMG_DIR, f"clip_{ts}_{uid}{orig_ext}")
    f.save(tmp_path)
    # Detect actual format from magic bytes and normalize to PNG
    with open(tmp_path, "rb") as fh:
        header = fh.read(8)
    is_png = header[:4] == b"\x89PNG"
    fname = f"clip_{ts}_{uid}.png"
    fpath = os.path.join(CLIP_IMG_DIR, fname)
    if is_png:
        if tmp_path != fpath:
            os.rename(tmp_path, fpath)
    else:
        # Convert to PNG via sips
        rc = subprocess.run(["sips", "--setProperty", "format", "png",
            tmp_path, "--out", fpath], capture_output=True, timeout=15)
        if rc.returncode != 0:
            # Fallback: keep original, rename to .png (browser might still display it)
            os.rename(tmp_path, fpath)
        else:
            try: os.remove(tmp_path)
            except: pass
    fsize = os.path.getsize(fpath)
    # Set Mac clipboard to this image
    ok = set_mac_clipboard_image(fpath)
    if ok:
        with clipboard_lock:
            add_clipboard_entry(source="windows", entry_type="image",
                               image_file=fname, image_size=fsize)
    return jsonify({"ok": ok, "file": fname, "size": fsize})

@app.route("/api/files", methods=["GET"])
def api_files(): return jsonify({"files": get_files()})

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "Empty filename"}), 400
    raw = os.path.basename(f.filename or "upload")
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', raw).strip('. ')
    if not filename: filename = "upload"
    dest = os.path.join(SHARED_DIR, filename)
    try:
        f.save(dest)
        return jsonify({"ok": True, "filename": filename})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/files/<path:filename>")
def download_file(filename): return send_from_directory(SHARED_DIR, filename, as_attachment=True)

@app.route("/api/delete", methods=["POST"])
def api_delete():
    data = request.get_json(silent=True)
    if not data or not data.get("name", "").strip():
        return jsonify({"ok": False, "error": "No name provided"}), 400
    name = data["name"].strip()
    target = os.path.realpath(os.path.join(SHARED_DIR, name))
    if not target.startswith(os.path.realpath(SHARED_DIR)) or target == os.path.realpath(SHARED_DIR):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    if not os.path.exists(target):
        return jsonify({"ok": False, "error": "Not found"}), 404
    try:
        if os.path.isdir(target):
            shutil.rmtree(target)
            return jsonify({"ok": True, "deleted": name, "type": "dir"})
        else:
            os.remove(target)
            return jsonify({"ok": True, "deleted": name, "type": "file"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/upload-with-path", methods=["POST"])
def api_upload_with_path():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file provided"}), 400
    f = request.files["file"]
    rel_path = request.form.get("path", "").strip()
    if not rel_path:
        rel_path = f.filename or "upload"
    parts = rel_path.replace("\\", "/").split("/")
    safe_parts = []
    for p in parts:
        p = re.sub(r'[<>:"|?*\x00-\x1f]', '_', p).strip('. ')
        if p and p != ".." and p != ".":
            safe_parts.append(p)
    if not safe_parts:
        return jsonify({"ok": False, "error": "Invalid path"}), 400
    rel_safe = os.path.join(*safe_parts)
    dest = os.path.join(SHARED_DIR, rel_safe)
    if not os.path.realpath(dest).startswith(os.path.realpath(SHARED_DIR)):
        return jsonify({"ok": False, "error": "Path traversal blocked"}), 400
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        f.save(dest)
        return jsonify({"ok": True, "path": rel_safe, "size": os.path.getsize(dest)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/files-zip/<path:dirname>")
def download_dir_zip(dirname):
    dirpath = os.path.join(SHARED_DIR, dirname)
    if not os.path.isdir(dirpath):
        return jsonify({"ok": False, "error": "Not a directory"}), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(dirpath):
            for f in files:
                fp = os.path.join(root, f)
                arcname = os.path.join(dirname, os.path.relpath(fp, dirpath))
                zf.write(fp, arcname)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"{dirname}.zip")

@app.route("/api/upload-folder", methods=["POST"])
def api_upload_folder():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename or not f.filename.endswith(".zip"):
        return jsonify({"ok": False, "error": "Must be a .zip file"}), 400
    try:
        zdata = io.BytesIO(f.read())
        with zipfile.ZipFile(zdata, "r") as zf:
            total_size = sum(i.file_size for i in zf.infolist())
            if total_size > MAX_ZIP_EXTRACT_MB * 1024 * 1024:
                return jsonify({"ok": False, "error": f"Zip too large ({total_size // 1048576}MB > {MAX_ZIP_EXTRACT_MB}MB)"}), 400
            if len(zf.namelist()) > MAX_ZIP_FILES:
                return jsonify({"ok": False, "error": f"Too many files ({len(zf.namelist())} > {MAX_ZIP_FILES})"}), 400
            extracted = 0
            for member in zf.namelist():
                target = os.path.realpath(os.path.join(SHARED_DIR, member))
                if not target.startswith(os.path.realpath(SHARED_DIR)):
                    continue
                zf.extract(member, SHARED_DIR)
                extracted += 1
        return jsonify({"ok": True, "extracted": extracted})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssl", action="store_true", help="Run HTTPS on port 9877 (for Clipboard API auto-sync)")
    args = parser.parse_args()

    ip = get_local_ip()
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    CERT_FILE = os.path.join(SCRIPT_DIR, ".clipboard-share-cert.pem")
    KEY_FILE = os.path.join(SCRIPT_DIR, ".clipboard-share-key.pem")

    if args.ssl:
        port = 9877
        print(f"\n  Clipboard + File Share v2.5 — HTTPS mode (auto-sync)")
        print(f"  Local:   https://127.0.0.1:{port}")
        print(f"  Network: https://{ip}:{port}")
        print()
        threading.Thread(target=clipboard_watcher, daemon=True).start()
        app.run(host="0.0.0.0", port=port, debug=False, ssl_context=(CERT_FILE, KEY_FILE))
    else:
        port = 9876
        print(f"\n  Clipboard + File Share v2.5 (text + images + auto-sync)")
        print(f"  Local:   http://127.0.0.1:{port}")
        print(f"  Network: http://{ip}:{port}")
        print()
        threading.Thread(target=clipboard_watcher, daemon=True).start()
        app.run(host="0.0.0.0", port=port, debug=False)

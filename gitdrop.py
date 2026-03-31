#!/usr/bin/env python3
"""
gitdrop v1.2 — self-hosted file exchange + messaging
Run: python3 gitdrop.py [--port 7070] [--host 0.0.0.0]
"""

import os, sys, json, uuid, hashlib, zipfile, argparse, mimetypes, time
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps

try:
    from flask import Flask, request, jsonify, send_file, Response, abort
except ImportError:
    print("Flask not found. Install: pip3 install flask")
    sys.exit(1)

STORAGE_DIR = Path(__file__).parent / "storage"
STORAGE_DIR.mkdir(exist_ok=True)
META_FILE = STORAGE_DIR / ".meta.json"
MSGS_FILE = STORAGE_DIR / ".messages.json"
MAX_UPLOAD_MB = 2048

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

def load_meta():
    if META_FILE.exists(): return json.loads(META_FILE.read_text())
    return {}
def save_meta(meta): META_FILE.write_text(json.dumps(meta, indent=2))

def load_msgs():
    if MSGS_FILE.exists(): return json.loads(MSGS_FILE.read_text())
    return []
def save_msgs(msgs): MSGS_FILE.write_text(json.dumps(msgs, indent=2))

def file_hash(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(8192): h.update(chunk)
    return h.hexdigest()[:7]

AUTH_TOKEN = os.environ.get("GITDROP_TOKEN", "")

def check_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if AUTH_TOKEN:
            token = request.headers.get("X-Token", "") or request.args.get("token", "")
            if token != AUTH_TOKEN: return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper

# ── File API ────────────────────────────────────────────────────────
@app.route("/api/files", methods=["GET"])
@check_auth
def list_files():
    meta = load_meta()
    files = []
    for fid, info in sorted(meta.items(), key=lambda x: x[1]["uploaded"], reverse=True):
        fpath = STORAGE_DIR / fid
        if fpath.exists():
            files.append({"id":fid,"name":info["name"],"size":fpath.stat().st_size,
                "hash":info["hash"],"mime":info.get("mime","application/octet-stream"),
                "uploaded":info["uploaded"],"is_folder":info.get("is_folder",False),
                "file_count":info.get("file_count",0)})
    return jsonify(files)

@app.route("/api/upload", methods=["POST"])
@check_auth
def upload_files():
    if "files" not in request.files: return jsonify({"error":"no files"}), 400
    meta = load_meta(); uploaded = []
    for f in request.files.getlist("files"):
        fid = uuid.uuid4().hex[:12]; fpath = STORAGE_DIR / fid; f.save(fpath)
        h = file_hash(fpath)
        mime = f.content_type or mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
        meta[fid] = {"name":f.filename,"hash":h,"mime":mime,
            "uploaded":datetime.now(timezone.utc).isoformat(),"is_folder":False}
        uploaded.append({"id":fid,"name":f.filename,"size":fpath.stat().st_size,
            "hash":h,"mime":mime,"uploaded":meta[fid]["uploaded"],"is_folder":False})
    save_meta(meta); return jsonify(uploaded), 201

@app.route("/api/upload-folder", methods=["POST"])
@check_auth
def upload_folder():
    if "files" not in request.files: return jsonify({"error":"no files"}), 400
    file_list = request.files.getlist("files")
    paths = request.form.getlist("paths")
    folder_name = request.form.get("folder_name", "folder")
    fid = uuid.uuid4().hex[:12]; fpath = STORAGE_DIR / fid
    with zipfile.ZipFile(fpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, f in enumerate(file_list):
            zf.writestr(paths[i] if i < len(paths) else f.filename, f.read())
    h = file_hash(fpath); meta = load_meta()
    meta[fid] = {"name":folder_name+".zip","original_name":folder_name,"hash":h,
        "mime":"application/zip","uploaded":datetime.now(timezone.utc).isoformat(),
        "is_folder":True,"file_count":len(file_list)}
    save_meta(meta)
    return jsonify({"id":fid,"name":folder_name+".zip","size":fpath.stat().st_size,
        "hash":h,"mime":"application/zip","uploaded":meta[fid]["uploaded"],
        "is_folder":True,"file_count":len(file_list)}), 201

@app.route("/api/files/<fid>", methods=["GET"])
@check_auth
def download_file(fid):
    meta = load_meta()
    if fid not in meta: abort(404)
    fpath = STORAGE_DIR / fid
    if not fpath.exists(): abort(404)
    return send_file(fpath, download_name=meta[fid]["name"], as_attachment=True,
        mimetype=meta[fid].get("mime","application/octet-stream"))

@app.route("/api/files/<fid>/contents", methods=["GET"])
@check_auth
def list_folder_contents(fid):
    meta = load_meta()
    if fid not in meta or not meta[fid].get("is_folder"): abort(404)
    fpath = STORAGE_DIR / fid
    if not fpath.exists(): abort(404)
    try:
        with zipfile.ZipFile(fpath,"r") as zf:
            return jsonify([{"path":i.filename,"size":i.file_size} for i in zf.infolist() if not i.is_dir()])
    except zipfile.BadZipFile: abort(400)

@app.route("/api/files/<fid>", methods=["DELETE"])
@check_auth
def delete_file(fid):
    meta = load_meta()
    if fid not in meta: abort(404)
    fpath = STORAGE_DIR / fid
    if fpath.exists(): fpath.unlink()
    name = meta.pop(fid)["name"]; save_meta(meta)
    return jsonify({"deleted": name})

@app.route("/api/files/purge", methods=["POST"])
@check_auth
def purge_all():
    meta = load_meta(); count = 0
    for fid in list(meta.keys()):
        fpath = STORAGE_DIR / fid
        if fpath.exists(): fpath.unlink()
        count += 1
    save_meta({}); return jsonify({"purged": count})

# ── Messages API ────────────────────────────────────────────────────
@app.route("/api/messages", methods=["GET"])
@check_auth
def get_messages():
    since = request.args.get("since", 0, type=float)
    msgs = load_msgs()
    if since: msgs = [m for m in msgs if m["ts"] > since]
    return jsonify(msgs)

@app.route("/api/messages", methods=["POST"])
@check_auth
def send_message():
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    device = (data.get("device") or "unknown").strip()[:32]
    if not text: return jsonify({"error":"empty message"}), 400
    msg = {
        "id": uuid.uuid4().hex[:10],
        "device": device,
        "text": text[:4096],
        "ts": time.time(),
        "time": datetime.now(timezone.utc).isoformat(),
    }
    msgs = load_msgs()
    msgs.append(msg)
    # Keep last 500 messages
    if len(msgs) > 500: msgs = msgs[-500:]
    save_msgs(msgs)
    return jsonify(msg), 201

@app.route("/api/messages", methods=["DELETE"])
@check_auth
def clear_messages():
    save_msgs([])
    return jsonify({"cleared": True})

# ── Frontend ────────────────────────────────────────────────────────
@app.route("/")
def index(): return Response(FRONTEND_HTML, mimetype="text/html")

FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>gitdrop</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0a0e17;--bg2:#0f1520;--bg3:#151d2e;--surface:#1a2338;
  --border:#1e2a42;--border-hl:#2d4a6f;
  --text:#c5cdd8;--text-dim:#5a6a80;--text-bright:#e8edf4;
  --green:#3ddc84;--green-dim:#1a3a28;
  --blue:#58a6ff;--blue-dim:#152a46;
  --orange:#f0883e;--red:#f85149;--red-dim:#3a1515;
  --purple:#bc8cff;--cyan:#56d4dd;--yellow:#e3b341;
  --font:'JetBrains Mono',monospace;
}
body{font-family:var(--font);background:var(--bg);color:var(--text);min-height:100vh}
.scanline{position:fixed;inset:0;pointer-events:none;z-index:999;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.03) 2px,rgba(0,0,0,.03) 4px)}
.header{border-bottom:1px solid var(--border);padding:16px 24px;display:flex;
  align-items:center;justify-content:space-between;background:var(--bg2);position:sticky;top:0;z-index:10}
.logo{display:flex;align-items:center;gap:12px}
.logo-icon{width:36px;height:36px;border-radius:8px;
  background:linear-gradient(135deg,var(--green),var(--cyan));
  display:flex;align-items:center;justify-content:center;
  font-size:18px;font-weight:700;color:var(--bg);letter-spacing:-1px}
.logo-text{font-size:18px;font-weight:600;color:var(--text-bright)}
.logo-tag{font-size:10px;color:var(--text-dim);background:var(--bg3);
  padding:2px 8px;border-radius:4px;border:1px solid var(--border);margin-left:4px}
.header-actions{display:flex;gap:6px}
.tab-btn{background:none;border:1px solid transparent;color:var(--text-dim);
  font-family:var(--font);font-size:12px;padding:6px 12px;border-radius:6px;cursor:pointer;transition:.2s}
.tab-btn:hover{color:var(--text);background:var(--bg3)}
.tab-btn.active{color:var(--green);border-color:var(--border-hl);background:var(--bg3)}
.badge{background:var(--green-dim);color:var(--green);font-size:10px;padding:1px 6px;border-radius:4px;margin-left:4px}
.badge.unread{background:var(--blue-dim);color:var(--blue);animation:pulse 1.5s infinite}
.stats-bar{display:flex;gap:20px;padding:10px 24px;background:var(--bg2);
  border-bottom:1px solid var(--border);font-size:11px;color:var(--text-dim)}
.stat{display:flex;align-items:center;gap:6px}
.stat-dot{width:6px;height:6px;border-radius:50%;animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.main{padding:20px 24px;max-width:960px;margin:0 auto}

/* dropzone */
.dropzone{border:2px dashed var(--border);border-radius:12px;padding:40px 24px;
  text-align:center;cursor:default;transition:.3s;background:var(--bg2);position:relative;overflow:hidden;margin-bottom:20px}
.dropzone::before{content:'';position:absolute;inset:0;background:radial-gradient(circle at 50% 50%,var(--green-dim),transparent 70%);opacity:0;transition:opacity .3s}
.dropzone.active{border-color:var(--green);background:var(--bg3);transform:scale(1.01)}
.dropzone.active::before{opacity:1}
.dropzone:hover{border-color:var(--border-hl)}.dropzone:hover::before{opacity:.5}
.drop-icon{font-size:36px;margin-bottom:12px;display:block;position:relative;z-index:1}
.drop-text{font-size:14px;color:var(--text);position:relative;z-index:1}
.drop-hint{font-size:11px;color:var(--text-dim);margin-top:8px;position:relative;z-index:1}
.drop-btns{display:flex;gap:8px;justify-content:center;margin-top:16px;position:relative;z-index:1}
.drop-btn{background:var(--bg3);border:1px solid var(--border);color:var(--text-dim);
  font-family:var(--font);font-size:11px;padding:6px 16px;border-radius:6px;cursor:pointer;transition:.15s}
.drop-btn:hover{color:var(--text-bright);border-color:var(--border-hl);background:var(--surface)}
.drop-btn.folder{color:var(--cyan);border-color:var(--cyan);border-style:dashed}
.drop-btn.folder:hover{background:rgba(86,212,221,0.08)}

/* files */
.section-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.section-title{font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:var(--text-dim)}
.clear-btn{background:none;border:1px solid var(--border);color:var(--red);
  font-family:var(--font);font-size:11px;padding:4px 12px;border-radius:5px;cursor:pointer;transition:.15s}
.clear-btn:hover{background:var(--red-dim);border-color:var(--red)}
.file-list{display:flex;flex-direction:column;gap:6px}
.file-row{display:grid;grid-template-columns:28px 1fr auto auto auto;
  align-items:center;gap:10px;padding:10px 14px;background:var(--bg2);
  border:1px solid var(--border);border-radius:8px;font-size:13px;transition:.15s;animation:slideIn .25s ease-out}
@keyframes slideIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}
.file-row:hover{border-color:var(--border-hl);background:var(--bg3)}
.file-row.is-folder{border-left:3px solid var(--cyan)}
.file-icon{font-size:18px;text-align:center}
.file-name{color:var(--text-bright);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file-name .folder-badge{font-size:10px;color:var(--cyan);background:rgba(86,212,221,0.1);padding:1px 6px;border-radius:3px;margin-left:6px;font-weight:400}
.file-hash{color:var(--yellow);font-size:11px;opacity:.7}
.file-size{color:var(--text-dim);font-size:11px;min-width:60px;text-align:right}
.file-btn{background:none;border:1px solid var(--border);color:var(--text-dim);
  font-family:var(--font);font-size:11px;padding:4px 10px;border-radius:5px;cursor:pointer;transition:.15s;white-space:nowrap}
.file-btn:hover{color:var(--text-bright);border-color:var(--border-hl);background:var(--surface)}
.file-btn.dl:hover{color:var(--green);border-color:var(--green)}
.file-btn.peek:hover{color:var(--cyan);border-color:var(--cyan)}
.file-btn.cp:hover{color:var(--blue);border-color:var(--blue)}
.file-btn.cp.copied{color:var(--green);border-color:var(--green)}
.file-btn.rm:hover{color:var(--red);border-color:var(--red)}
.folder-contents{margin:4px 0 4px 38px;padding:8px 12px;background:var(--bg);
  border:1px solid var(--border);border-radius:6px;font-size:11px;max-height:200px;overflow-y:auto;animation:slideIn .2s ease-out}
.folder-contents::-webkit-scrollbar{width:4px}
.folder-contents::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.fc-item{display:flex;justify-content:space-between;padding:3px 0;color:var(--text-dim);border-bottom:1px solid var(--border)}
.fc-item:last-child{border:none}
.fc-path{color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-right:12px}
.fc-size{flex-shrink:0;color:var(--text-dim)}
.fc-header{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--text-dim);margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid var(--border)}

/* chat */
.chat-wrap{display:flex;flex-direction:column;height:calc(100vh - 140px);max-height:700px}
.chat-device-bar{display:flex;gap:8px;margin-bottom:12px;align-items:center}
.chat-device-bar label{font-size:11px;color:var(--text-dim);text-transform:uppercase;letter-spacing:1px}
.chat-device-input{flex:1;max-width:200px;background:var(--bg3);border:1px solid var(--border);color:var(--cyan);
  font-family:var(--font);font-size:12px;padding:5px 10px;border-radius:5px;outline:none}
.chat-device-input:focus{border-color:var(--cyan)}
.chat-messages{flex:1;overflow-y:auto;background:var(--bg2);border:1px solid var(--border);
  border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
.chat-messages::-webkit-scrollbar{width:6px}
.chat-messages::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
.msg{max-width:85%;animation:slideIn .2s ease-out}
.msg.mine{align-self:flex-end}
.msg.theirs{align-self:flex-start}
.msg-bubble{padding:8px 12px;border-radius:10px;font-size:13px;line-height:1.5;word-break:break-word;white-space:pre-wrap}
.msg.mine .msg-bubble{background:var(--green-dim);border:1px solid rgba(61,220,132,0.2);color:var(--text-bright);border-bottom-right-radius:3px}
.msg.theirs .msg-bubble{background:var(--bg3);border:1px solid var(--border);color:var(--text-bright);border-bottom-left-radius:3px}
.msg-meta{font-size:10px;color:var(--text-dim);margin-top:3px;display:flex;gap:8px}
.msg.mine .msg-meta{justify-content:flex-end}
.msg-device{color:var(--cyan);font-weight:500}
.msg-copy{cursor:pointer;opacity:0;transition:opacity .15s}
.msg:hover .msg-copy{opacity:1}
.msg-copy:hover{color:var(--blue)}
.chat-input-row{display:flex;gap:8px}
.chat-input{flex:1;background:var(--bg2);border:1px solid var(--border);color:var(--text-bright);
  font-family:var(--font);font-size:13px;padding:10px 14px;border-radius:8px;outline:none;resize:none;
  min-height:42px;max-height:120px;line-height:1.4}
.chat-input:focus{border-color:var(--border-hl)}
.chat-input::placeholder{color:var(--text-dim)}
.chat-send{background:var(--green-dim);border:1px solid var(--green);color:var(--green);
  font-family:var(--font);font-size:12px;padding:10px 20px;border-radius:8px;cursor:pointer;transition:.15s;
  font-weight:600;white-space:nowrap;align-self:flex-end}
.chat-send:hover{background:rgba(61,220,132,0.2)}
.chat-send:active{transform:scale(0.97)}
.chat-empty{text-align:center;padding:60px 20px;color:var(--text-dim);font-size:13px;margin:auto}
.chat-empty .big{font-size:28px;margin-bottom:10px;display:block;opacity:.4}
.chat-clear{margin-left:auto}

/* log */
.log-panel{background:var(--bg2);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.log-header{padding:10px 14px;font-size:11px;color:var(--text-dim);border-bottom:1px solid var(--border);background:var(--bg3);display:flex;justify-content:space-between}
.log-body{padding:10px 14px;max-height:420px;overflow-y:auto;font-size:12px;line-height:1.8}
.log-body::-webkit-scrollbar{width:6px}
.log-body::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
.log-line{display:flex;gap:10px}
.log-time{color:var(--text-dim);opacity:.5;min-width:90px;flex-shrink:0}
.log-type{min-width:60px;flex-shrink:0;font-weight:600;font-size:11px}
.log-type.system{color:var(--purple)}.log-type.info{color:var(--blue)}
.log-type.commit{color:var(--green)}.log-type.success{color:var(--cyan)}
.log-type.warning{color:var(--orange)}.log-type.error{color:var(--red)}
.log-msg{color:var(--text);word-break:break-all}
.empty-state{text-align:center;padding:60px 20px;color:var(--text-dim);font-size:13px}
.empty-state .big{font-size:32px;margin-bottom:12px;display:block;opacity:.4}
.upload-progress{margin-bottom:16px;padding:12px 14px;background:var(--bg2);border:1px solid var(--border);border-radius:8px}
.progress-bar{height:4px;background:var(--bg3);border-radius:2px;overflow:hidden;margin-top:8px}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--green),var(--cyan));transition:width .2s;border-radius:2px}
.progress-text{font-size:11px;color:var(--text-dim);display:flex;justify-content:space-between}
.token-input{display:flex;gap:8px;margin-bottom:16px;padding:12px;background:var(--bg2);border:1px solid var(--border);border-radius:8px}
.token-input input{flex:1;background:var(--bg3);border:1px solid var(--border);color:var(--text);font-family:var(--font);font-size:12px;padding:6px 10px;border-radius:5px;outline:none}
.token-input input:focus{border-color:var(--border-hl)}
.token-input button{background:var(--green-dim);border:1px solid var(--green);color:var(--green);font-family:var(--font);font-size:11px;padding:6px 14px;border-radius:5px;cursor:pointer}
@media(max-width:640px){
  .header{padding:12px 16px}.main{padding:16px}
  .file-row{grid-template-columns:28px 1fr auto;gap:8px}
  .file-hash,.file-size{display:none}
  .stats-bar{padding:8px 16px;gap:12px;flex-wrap:wrap}
  .drop-btns{flex-direction:column;align-items:center}
  .chat-wrap{height:calc(100vh - 160px)}
  .msg{max-width:92%}
}
</style>
</head>
<body>
<div class="scanline"></div>
<div class="header">
  <div class="logo">
    <div class="logo-icon">G</div>
    <span class="logo-text">gitdrop</span>
    <span class="logo-tag">remote</span>
  </div>
  <div class="header-actions">
    <button class="tab-btn active" onclick="switchTab('files')" id="tabFiles">files</button>
    <button class="tab-btn" onclick="switchTab('chat')" id="tabChat">chat<span class="badge" id="chatBadge" style="display:none">0</span></button>
    <button class="tab-btn" onclick="switchTab('log')" id="tabLog">log<span class="badge" id="logCount">0</span></button>
  </div>
</div>
<div class="stats-bar">
  <div class="stat"><span class="stat-dot" style="background:var(--green)"></span><span id="nodeStatus">connecting...</span></div>
  <div class="stat"><span id="fileCount">0</span> object(s)</div>
  <div class="stat"><span id="totalSize">0 B</span> total</div>
  <div class="stat" style="margin-left:auto;opacity:.5">branch: main</div>
</div>
<div class="main">
  <div class="token-input" id="tokenBar" style="display:none">
    <input type="password" id="tokenInput" placeholder="enter access token..." onkeydown="if(event.key==='Enter')setToken()">
    <button onclick="setToken()">auth</button>
  </div>

  <!-- FILES -->
  <div id="viewFiles">
    <div class="dropzone" id="dropzone">
      <span class="drop-icon" id="dropIcon">↑</span>
      <div class="drop-text" id="dropText">git push — drag files or folders here</div>
      <div class="drop-hint">drop anything · files, folders, archives · max 2 GB</div>
      <div class="drop-btns">
        <button class="drop-btn" onclick="event.stopPropagation();document.getElementById('fileInput').click()">select files</button>
        <button class="drop-btn folder" onclick="event.stopPropagation();document.getElementById('folderInput').click()">select folder</button>
      </div>
      <input type="file" id="fileInput" multiple style="display:none">
      <input type="file" id="folderInput" webkitdirectory mozdirectory style="display:none">
    </div>
    <div id="uploadProgress" style="display:none" class="upload-progress">
      <div class="progress-text"><span id="progressLabel">uploading...</span><span id="progressPct">0%</span></div>
      <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0"></div></div>
    </div>
    <div id="fileSection"></div>
  </div>

  <!-- CHAT -->
  <div id="viewChat" style="display:none">
    <div class="chat-wrap">
      <div class="chat-device-bar">
        <label>device:</label>
        <input class="chat-device-input" id="deviceName" placeholder="my-laptop" maxlength="32">
        <button class="clear-btn chat-clear" onclick="clearChat()">clear all</button>
      </div>
      <div class="chat-messages" id="chatMessages">
        <div class="chat-empty"><span class="big">💬</span>no messages yet — send something</div>
      </div>
      <div class="chat-input-row">
        <textarea class="chat-input" id="chatInput" placeholder="type a message... (Shift+Enter for newline)" rows="1"></textarea>
        <button class="chat-send" onclick="sendMsg()">send</button>
      </div>
    </div>
  </div>

  <!-- LOG -->
  <div id="viewLog" style="display:none">
    <div class="log-panel">
      <div class="log-header"><span>git log --oneline</span><span id="logEntries">0 entries</span></div>
      <div class="log-body" id="logBody"></div>
    </div>
  </div>
</div>

<script>
const API='';
let TOKEN=localStorage.getItem('gitdrop_token')||'';
let files=[],logs=[],messages=[];
let expandedFolders=new Set();
let currentTab='files';
let lastMsgTs=0;
let unreadCount=0;
let pollTimer=null;

/* ── Helpers ───────────────────────────────── */
const ICONS={image:'🖼',video:'🎬',audio:'🎵',pdf:'📄',zip:'📦',code:'⟨⟩',doc:'📝',folder:'📁',default:'📎'};
function getIcon(name,isFolder){
  if(isFolder)return ICONS.folder;const e=name.split('.').pop().toLowerCase();
  if(['jpg','jpeg','png','gif','svg','webp','bmp'].includes(e))return ICONS.image;
  if(['mp4','mov','avi','mkv','webm'].includes(e))return ICONS.video;
  if(['mp3','wav','ogg','flac','aac'].includes(e))return ICONS.audio;
  if(e==='pdf')return ICONS.pdf;if(['zip','rar','7z','tar','gz'].includes(e))return ICONS.zip;
  if(['js','ts','py','rs','go','cpp','c','html','css','json'].includes(e))return ICONS.code;
  if(['doc','docx','txt','md','rtf'].includes(e))return ICONS.doc;return ICONS.default;
}
function fmtSize(b){if(!b)return'0 B';const k=1024,s=['B','KB','MB','GB'];const i=Math.floor(Math.log(b)/Math.log(k));return parseFloat((b/Math.pow(k,i)).toFixed(1))+' '+s[i]}
function ts(){const d=new Date();return d.toLocaleTimeString('en-US',{hour12:false})+'.'+String(d.getMilliseconds()).padStart(3,'0')}
function fmtTime(iso){try{return new Date(iso).toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false})}catch(e){return''}}
function addLog(type,text){logs.push({type,text,time:ts()});document.getElementById('logCount').textContent=logs.length;document.getElementById('logEntries').textContent=logs.length+' entries';renderLog()}
function renderLog(){const el=document.getElementById('logBody');el.innerHTML=logs.map(l=>`<div class="log-line"><span class="log-time">${l.time}</span><span class="log-type ${l.type}">${l.type}</span><span class="log-msg">${l.text}</span></div>`).join('');el.scrollTop=el.scrollHeight}
function headers(){const h={'Accept':'application/json'};if(TOKEN)h['X-Token']=TOKEN;return h}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}

async function apiFetch(path,opts={}){
  opts.headers={...headers(),...(opts.headers||{})};
  const r=await fetch(API+path,opts);
  if(r.status===401){document.getElementById('tokenBar').style.display='flex';addLog('error','auth required');throw new Error('unauthorized')}
  return r;
}
function setToken(){TOKEN=document.getElementById('tokenInput').value;localStorage.setItem('gitdrop_token',TOKEN);document.getElementById('tokenBar').style.display='none';addLog('info','token set');loadFiles();pollMessages()}
function getDevice(){return document.getElementById('deviceName').value.trim()||'anon'}

/* ── Tabs ──────────────────────────────────── */
function switchTab(tab){
  currentTab=tab;
  document.getElementById('viewFiles').style.display=tab==='files'?'':'none';
  document.getElementById('viewChat').style.display=tab==='chat'?'':'none';
  document.getElementById('viewLog').style.display=tab==='log'?'':'none';
  document.getElementById('tabFiles').classList.toggle('active',tab==='files');
  document.getElementById('tabChat').classList.toggle('active',tab==='chat');
  document.getElementById('tabLog').classList.toggle('active',tab==='log');
  if(tab==='chat'){unreadCount=0;updateChatBadge();scrollChat();document.getElementById('chatInput').focus()}
}

/* ── Files ─────────────────────────────────── */
async function loadFiles(){
  try{const r=await apiFetch('/api/files');files=await r.json();document.getElementById('nodeStatus').textContent='node: online';renderFiles()}
  catch(e){document.getElementById('nodeStatus').textContent='node: error';if(e.message!=='unauthorized')addLog('error','fetch failed: '+e.message)}
}
function renderFiles(){
  document.getElementById('fileCount').textContent=files.length;
  document.getElementById('totalSize').textContent=fmtSize(files.reduce((a,f)=>a+f.size,0));
  const sec=document.getElementById('fileSection');
  if(!files.length){sec.innerHTML='<div class="empty-state"><span class="big">∅</span>working tree clean — nothing to commit</div>';return}
  sec.innerHTML=`<div class="section-head"><span class="section-title">staged objects (${files.length})</span><button class="clear-btn" onclick="purgeAll()">git reset --hard</button></div><div class="file-list">${files.map(f=>{
    const isF=f.is_folder,expanded=expandedFolders.has(f.id),dn=isF?f.name.replace(/\.zip$/,''):f.name;
    return`<div class="file-row ${isF?'is-folder':''}"><span class="file-icon">${getIcon(f.name,isF)}</span><span class="file-name">${esc(dn)}${isF?`<span class="folder-badge">${f.file_count} files</span>`:''}</span><span class="file-hash">${f.hash}</span><span class="file-size">${fmtSize(f.size)}</span><div style="display:flex;gap:4px">${isF?`<button class="file-btn peek" onclick="toggleContents('${f.id}')">${expanded?'▾ tree':'▸ tree'}</button>`:''}<button class="file-btn dl" onclick="dlFile('${f.id}','${esc(f.name)}')">fetch</button><button class="file-btn cp" id="cp-${f.id}" onclick="cpLink('${f.id}')">ref</button><button class="file-btn rm" onclick="rmFile('${f.id}','${esc(f.name)}')">rm</button></div></div>${expanded?`<div class="folder-contents" id="fc-${f.id}"><div class="fc-header">loading tree...</div></div>`:''}`}).join('')}</div>`;
  expandedFolders.forEach(id=>loadFolderContents(id));
}
async function toggleContents(id){if(expandedFolders.has(id))expandedFolders.delete(id);else expandedFolders.add(id);renderFiles()}
async function loadFolderContents(id){const el=document.getElementById('fc-'+id);if(!el)return;try{const r=await apiFetch('/api/files/'+id+'/contents');const items=await r.json();el.innerHTML=`<div class="fc-header">tree — ${items.length} files</div>`+items.map(i=>`<div class="fc-item"><span class="fc-path">${esc(i.path)}</span><span class="fc-size">${fmtSize(i.size)}</span></div>`).join('')}catch(e){el.innerHTML='<div class="fc-header" style="color:var(--red)">failed to read tree</div>'}}

/* ── Upload ────────────────────────────────── */
function showProgress(label){document.getElementById('uploadProgress').style.display='block';document.getElementById('progressLabel').textContent=label;document.getElementById('progressPct').textContent='0%';document.getElementById('progressFill').style.width='0'}
function hideProgress(){document.getElementById('uploadProgress').style.display='none';document.getElementById('progressFill').style.width='0'}
function xhrSend(url,formData){return new Promise(resolve=>{const xhr=new XMLHttpRequest();xhr.upload.onprogress=e=>{if(e.lengthComputable){const p=Math.round(e.loaded/e.total*100);document.getElementById('progressFill').style.width=p+'%';document.getElementById('progressPct').textContent=p+'%'}};xhr.onload=()=>{hideProgress();resolve({status:xhr.status,body:xhr.responseText})};xhr.onerror=()=>{hideProgress();addLog('error','network error');resolve({status:0,body:''})};xhr.open('POST',API+url);if(TOKEN)xhr.setRequestHeader('X-Token',TOKEN);xhr.send(formData)})}

async function uploadFiles(fileList){
  const form=new FormData();Array.from(fileList).forEach(f=>form.append('files',f));
  showProgress(`pushing ${fileList.length} file(s)...`);
  const{status,body}=await xhrSend('/api/upload',form);
  if(status===201){const res=JSON.parse(body);res.forEach(f=>addLog('commit',`[${f.hash}] add: ${f.name} (${fmtSize(f.size)})`));addLog('success',`${res.length} file(s) pushed`);loadFiles()}
  else if(status===401){document.getElementById('tokenBar').style.display='flex';addLog('error','auth required')}
  else addLog('error','push failed: '+status);
}
async function uploadFolder(fileList){
  const folderName=(fileList[0].webkitRelativePath||fileList[0].name).split('/')[0];
  const form=new FormData();for(const f of fileList){form.append('files',f);form.append('paths',f.webkitRelativePath||f.name)}
  form.append('folder_name',folderName);showProgress(`packing ${folderName}/...`);
  addLog('info',`staging folder: ${folderName}/ (${fileList.length} files)`);
  const{status,body}=await xhrSend('/api/upload-folder',form);
  if(status===201){const res=JSON.parse(body);addLog('commit',`[${res.hash}] add: ${folderName}/ → ${fmtSize(res.size)} zip`);addLog('success','folder pushed');loadFiles()}
  else addLog('error','folder push failed: '+status);
}

async function readEntryRecursive(entry){
  if(entry.isFile){return new Promise(res=>{entry.file(f=>{f._relativePath=entry.fullPath.replace(/^\//,'');res([f])})})}
  if(entry.isDirectory){const reader=entry.createReader();const entries=await new Promise(res=>{const all=[];const go=()=>{reader.readEntries(batch=>{if(!batch.length)return res(all);all.push(...batch);go()})};go()});const out=[];for(const e of entries){out.push(...await readEntryRecursive(e))}return out}return[]
}
async function handleDrop(e){
  e.preventDefault();const dz=document.getElementById('dropzone');dz.classList.remove('active');
  document.getElementById('dropIcon').textContent='↑';document.getElementById('dropText').textContent='git push — drag files or folders here';
  const items=e.dataTransfer.items;if(!items||!items.length)return;
  let hasDir=false;const entries=[];
  for(let i=0;i<items.length;i++){const entry=items[i].webkitGetAsEntry?items[i].webkitGetAsEntry():null;if(entry){entries.push(entry);if(entry.isDirectory)hasDir=true}}
  if(hasDir){const plainFiles=[];for(const entry of entries){if(entry.isDirectory){addLog('info',`scanning ${entry.name}/...`);const dirFiles=await readEntryRecursive(entry);if(!dirFiles.length){addLog('warning',`${entry.name}/ is empty`);continue}const form=new FormData();for(const f of dirFiles){form.append('files',f);form.append('paths',f._relativePath)}form.append('folder_name',entry.name);showProgress(`packing ${entry.name}/...`);const{status,body}=await xhrSend('/api/upload-folder',form);if(status===201){const res=JSON.parse(body);addLog('commit',`[${res.hash}] add: ${entry.name}/ → ${fmtSize(res.size)} zip`);addLog('success','folder pushed')}else addLog('error','folder push failed: '+status)}else{const f=await new Promise(res=>{entry.file(f=>res(f))});plainFiles.push(f)}}if(plainFiles.length)await uploadFiles(plainFiles);loadFiles()}
  else uploadFiles(e.dataTransfer.files);
}

function dlFile(id,name){const url=API+'/api/files/'+id+(TOKEN?'?token='+encodeURIComponent(TOKEN):'');const a=document.createElement('a');a.href=url;a.download=name;a.click();addLog('info',`fetch: ${name} → local`)}
function cpLink(id){const url=location.origin+'/api/files/'+id;navigator.clipboard.writeText(url).then(()=>{const btn=document.getElementById('cp-'+id);btn.textContent='✓ ref';btn.classList.add('copied');addLog('info','ref copied');setTimeout(()=>{btn.textContent='ref';btn.classList.remove('copied')},2000)})}
async function rmFile(id,name){await apiFetch('/api/files/'+id,{method:'DELETE'});expandedFolders.delete(id);addLog('warning',`rm: ${name}`);loadFiles()}
async function purgeAll(){if(!confirm('git reset --hard — remove all files?'))return;await apiFetch('/api/files/purge',{method:'POST'});expandedFolders.clear();addLog('warning','reset: all files purged');loadFiles()}

/* ── Chat ──────────────────────────────────── */
function updateChatBadge(){
  const badge=document.getElementById('chatBadge');
  if(unreadCount>0){badge.style.display='';badge.textContent=unreadCount;badge.classList.add('unread')}
  else{badge.style.display='none';badge.classList.remove('unread')}
}
function scrollChat(){const el=document.getElementById('chatMessages');el.scrollTop=el.scrollHeight}

function renderMessages(){
  const el=document.getElementById('chatMessages');
  const dev=getDevice();
  if(!messages.length){el.innerHTML='<div class="chat-empty"><span class="big">💬</span>no messages yet — send something</div>';return}
  el.innerHTML=messages.map(m=>{
    const mine=m.device===dev;
    return`<div class="msg ${mine?'mine':'theirs'}"><div class="msg-bubble">${esc(m.text)}</div><div class="msg-meta"><span class="msg-device">${esc(m.device)}</span><span>${fmtTime(m.time)}</span><span class="msg-copy" onclick="copyMsg(this,'${esc(m.text).replace(/'/g,"\\'")}')">copy</span></div></div>`;
  }).join('');
  scrollChat();
}

function copyMsg(el,text){
  // Decode escaped HTML back for clipboard
  const ta=document.createElement('textarea');ta.innerHTML=text;
  navigator.clipboard.writeText(ta.value).then(()=>{el.textContent='copied!';setTimeout(()=>{el.textContent='copy'},1500)});
}

async function pollMessages(){
  try{
    const r=await apiFetch('/api/messages?since='+lastMsgTs);
    const newMsgs=await r.json();
    if(newMsgs.length){
      // Merge, avoiding duplicates
      const existingIds=new Set(messages.map(m=>m.id));
      const truly_new=newMsgs.filter(m=>!existingIds.has(m.id));
      if(truly_new.length){
        messages.push(...truly_new);
        lastMsgTs=Math.max(...messages.map(m=>m.ts));
        if(currentTab!=='chat'){unreadCount+=truly_new.filter(m=>m.device!==getDevice()).length;updateChatBadge()}
        renderMessages();
      }
    }
  }catch(e){}
}

async function sendMsg(){
  const input=document.getElementById('chatInput');
  const text=input.value.trim();
  if(!text)return;
  const device=getDevice();
  localStorage.setItem('gitdrop_device',device);
  try{
    const r=await apiFetch('/api/messages',{method:'POST',headers:{'Content-Type':'application/json',...headers()},body:JSON.stringify({text,device})});
    if(r.ok){const msg=await r.json();const existingIds=new Set(messages.map(m=>m.id));if(!existingIds.has(msg.id)){messages.push(msg);lastMsgTs=Math.max(lastMsgTs,msg.ts)}input.value='';input.style.height='42px';renderMessages()}
  }catch(e){addLog('error','send failed')}
}

async function clearChat(){
  if(!confirm('clear all messages?'))return;
  await apiFetch('/api/messages',{method:'DELETE'});
  messages=[];lastMsgTs=0;renderMessages();addLog('warning','chat cleared');
}

/* ── Events ───────────────────────────────── */
const dz=document.getElementById('dropzone');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('active');document.getElementById('dropIcon').textContent='⚡';document.getElementById('dropText').textContent='drop to push'});
dz.addEventListener('dragleave',()=>{dz.classList.remove('active');document.getElementById('dropIcon').textContent='↑';document.getElementById('dropText').textContent='git push — drag files or folders here'});
dz.addEventListener('drop',handleDrop);
document.getElementById('fileInput').addEventListener('change',e=>{if(e.target.files.length)uploadFiles(e.target.files);e.target.value=''});
document.getElementById('folderInput').addEventListener('change',e=>{if(e.target.files.length)uploadFolder(e.target.files);e.target.value=''});

// Chat input: Enter sends, Shift+Enter newline
const chatInput=document.getElementById('chatInput');
chatInput.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMsg()}});
chatInput.addEventListener('input',()=>{chatInput.style.height='42px';chatInput.style.height=Math.min(chatInput.scrollHeight,120)+'px'});

// Restore device name
const savedDevice=localStorage.getItem('gitdrop_device');
if(savedDevice)document.getElementById('deviceName').value=savedDevice;
document.getElementById('deviceName').addEventListener('change',()=>{localStorage.setItem('gitdrop_device',document.getElementById('deviceName').value)});

// Init
addLog('system','gitdrop v1.2.0 — files + messaging');
addLog('info',`remote: ${location.host}`);
loadFiles();
pollMessages();
// Poll for new messages every 2 seconds
pollTimer=setInterval(pollMessages,2000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="gitdrop file server")
    parser.add_argument("--port", type=int, default=7070)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--token", default="", help="Access token (or set GITDROP_TOKEN env)")
    args = parser.parse_args()
    if args.token: AUTH_TOKEN = args.token
    print(f"""
    ┌─────────────────────────────────────────┐
    │         gitdrop v1.2.0                  │
    │─────────────────────────────────────────│
    │  http://{args.host}:{args.port}                │
    │  storage: {STORAGE_DIR}             │
    │  auth: {'enabled' if AUTH_TOKEN else 'disabled'}                       │
    │  features: files, folders, chat         │
    └─────────────────────────────────────────┘
    """)
    app.run(host=args.host, port=args.port, debug=False)

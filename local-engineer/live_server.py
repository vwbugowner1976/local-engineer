#!/usr/bin/env python3
"""Local Engineer live status/event server.

Read-only by default. It watches the existing per-session events.jsonl and
working_state.json files, so the runtime itself does not need a long-lived
socket connection. Control endpoints create small request files consumed by a
launcher/process supervisor.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE = pathlib.Path.home() / ".local" / "state" / "local-engineer"
SESSIONS = STATE / "sessions"


def json_read(path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def session_dirs():
    if not SESSIONS.exists():
        return []
    return sorted((p for p in SESSIONS.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)


def latest_session():
    dirs = session_dirs()
    return dirs[0] if dirs else None


def load_state(session):
    return json_read(session / "working_state.json", {}) if session else {}


def load_events(session, since=0, limit=200):
    if not session:
        return []
    path = session / "events.jsonl"
    events = []
    try:
        with path.open(encoding="utf-8") as f:
            for seq, line in enumerate(f, 1):
                if seq <= since:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    event = {"type": "malformed_event", "raw": line.rstrip()}
                event["_seq"] = seq
                events.append(event)
    except FileNotFoundError:
        pass
    return events[-max(1, min(limit, 1000)):]


def status_payload(session):
    state = load_state(session)
    events = load_events(session, limit=1)
    last = events[-1] if events else {}
    return {
        "session": session.name if session else None,
        "status": state.get("status", "not_running"),
        "phase": state.get("phase", "unknown"),
        "round": state.get("rounds", 0),
        "repair_attempt": state.get("repair_attempts", 0),
        "repair_targeted_reads_used": state.get("repair_targeted_reads_used", 0),
        "last_tool": (last.get("tool") or last.get("message", {}).get("tool_calls", [{}])[0].get("function", {}).get("name")
                      if isinstance(last.get("message", {}), dict) else None),
        "last_result": last.get("result", ""),
        "target": state.get("hypothesis_target_file", ""),
        "next_action": state.get("next_action", ""),
        "allowed_tools": state.get("repair_allowed_reads", []),
        "build_status": state.get("build_status", ""),
        "test_status": state.get("test_status", ""),
        "hypothesis": state.get("hypothesis", ""),
        "files_modified": state.get("files_modified", []),
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def write_control(session, action, extra=None):
    if not session:
        return False
    control = session / "control"
    control.mkdir(exist_ok=True)
    payload = {"action": action, "requested_at": dt.datetime.now().isoformat()}
    if extra:
        payload.update(extra)
    (control / (action + ".json")).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return True


HTML = r"""<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local Engineer Live</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#111;color:#eee}
main{max-width:1000px;margin:auto;padding:16px}
.card{background:#1b1b1b;border:1px solid #333;border-radius:12px;padding:14px;margin:10px 0}
h1{font-size:22px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}
.label{color:#999;font-size:12px}.value{font-size:16px;margin-top:3px}
pre{white-space:pre-wrap;word-break:break-word;margin:0;font-size:12px;line-height:1.45}
.event{padding:10px 0;border-bottom:1px solid #2b2b2b}
.event:last-child{border-bottom:0}
.event-tool{font-weight:700;font-size:13px;margin-bottom:5px}
.event-args{color:#aaa;margin-bottom:5px}
.event-result{color:#ddd}
#events{max-height:58vh;overflow:auto}
button{padding:9px 12px;border-radius:8px;border:1px solid #555;background:#222;color:#fff}
.ok{color:#9f9}.bad{color:#f99}.muted{color:#aaa}
</style>
<main>
<h1>Local Engineer Live</h1>
<div class="card grid" id="status"></div>
<div class="card"><div class="label">Hypothesis</div><pre id="hypothesis"></pre></div>
<div class="card"><div class="label">Events</div><div id="events"><span class="muted">connecting…</span></div></div>
<div class="card"><button onclick="control('stop')">Request stop</button> <button onclick="control('resume')">Request resume</button> <span id="control" class="muted"></span></div>
</main>
<script>
let seq=0;
function esc(x){return String(x??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function stripAnsi(x){
  return String(x??'')
    .replace(/\x1B\][0-?]*[ -\/]*[@-~]/g,'')
    .replace(/\x1B\[[0-?]*[ -\/]*[@-~]/g,'')
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g,'');
}
function formatEvent(e){
  const tool=e.tool||e.message?.tool_calls?.[0]?.function?.name||e.type||'event';
  const result=stripAnsi(e.result||'');
  const args=e.args&&Object.keys(e.args).length?JSON.stringify(e.args,null,2):'';
  return '<div class="event"><div class="event-tool">'+esc(tool)+'</div>'+
    (args?'<pre class="event-args">'+esc(args)+'</pre>':'')+
    (result?'<pre class="event-result">'+esc(result)+'</pre>':'')+
    '</div>';
}
async function refresh(){
  try{
    const s=await fetch('/status').then(r=>r.json());
    const rows=[
      ['session',s.session],['status',s.status],['phase',s.phase],['round',s.round],
      ['repair',s.repair_attempt+'/'+Math.max(2,s.repair_attempt)],
      ['last tool',s.last_tool],['target',s.target],['next',s.next_action],
      ['build',s.build_status],['test',s.test_status]
    ];
    document.getElementById('status').innerHTML=rows.map(([a,b])=>'<div><div class="label">'+esc(a)+'</div><div class="value">'+esc(b)+'</div></div>').join('');
    document.getElementById('hypothesis').textContent=s.hypothesis||'';
    const ev=await fetch('/events?since='+seq+'&limit=120').then(r=>r.json());
    if(ev.length){seq=Math.max(seq,...ev.map(x=>x._seq));const box=document.getElementById('events');box.insertAdjacentHTML('beforeend',ev.map(formatEvent).join(''));box.scrollTop=box.scrollHeight;}
  }catch(e){document.getElementById('control').textContent='connection: '+e}
}
async function control(action){const r=await fetch('/'+action,{method:'POST'});document.getElementById('control').textContent=await r.text();}
refresh();setInterval(refresh,1200);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalEngineerLive/1"

    def send_json(self, data, code=200):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        session = latest_session()
        if u.path == "/":
            raw = HTML.encode()
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        if u.path == "/status":
            self.send_json(status_payload(session)); return
        if u.path == "/current":
            self.send_json(load_state(session)); return
        if u.path == "/checkpoint":
            self.send_json({"session": session.name if session else None,
                             "path": str(session / "working_state.json") if session else None}); return
        if u.path == "/events":
            q=parse_qs(u.query); since=int(q.get("since",["0"])[0]); limit=int(q.get("limit",["200"])[0])
            self.send_json(load_events(session,since,limit)); return
        self.send_json({"error":"not found"},404)

    def do_POST(self):
        session=latest_session()
        u=urlparse(self.path)
        if u.path in ("/stop","/resume"):
            if write_control(session,u.path[1:]):
                self.send_json({"ok":True,"action":u.path[1:],"session":session.name})
            else: self.send_json({"ok":False,"error":"no session"},404)
            return
        self.send_json({"error":"not found"},404)

    def log_message(self, fmt, *args):
        print("[live] "+fmt%args, flush=True)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--host",default="0.0.0.0")
    ap.add_argument("--port",type=int,default=8765)
    args=ap.parse_args()
    print(f"Local Engineer Live: http://127.0.0.1:{args.port}",flush=True)
    ThreadingHTTPServer((args.host,args.port),Handler).serve_forever()


if __name__=="__main__":
    main()

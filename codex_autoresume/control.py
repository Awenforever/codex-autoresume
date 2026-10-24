"""Local control API + built-in panel for the daemon.

A tiny 127.0.0.1-only HTTP server the in-app control surface (and any UI /
skill / curl) uses to read state and toggle per-thread guarding:

    GET  /                  -> built-in control panel (dark, Codex-styled)
    GET  /api/status        -> {paused, dry_run, quota, threads: [...]}
    POST /api/threads/<id>  -> {guard?, weekly?, model?, effort?}
    POST /api/pause         -> {paused: bool}

All writes land in the watcher state file; the running daemon picks them up
on its next tick (live sync), so toggles take effect within seconds.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .state import load_state, save_state

PANEL_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex 守护控制台</title>
<style>
:root{--bg:#0a0c0d;--card:#131719;--card2:#181d20;--line:#242b30;--fg:#ecefef;
--dim:#87929a;--accent:#10a37f;--accent-soft:#12362c;--warn:#e0a341;--danger:#e5534b}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,"Segoe UI",
"Microsoft YaHei",sans-serif;padding:20px 18px 40px;max-width:920px;margin:0 auto;
-webkit-font-smoothing:antialiased}
header{display:flex;align-items:center;gap:12px;margin-bottom:18px}
.logo{width:38px;height:38px;border-radius:11px;background:linear-gradient(135deg,
#10a37f,#0a6e56);display:flex;align-items:center;justify-content:center;
box-shadow:0 4px 18px rgba(16,163,127,.35)}
.logo svg{width:22px;height:22px}
h1{font-size:17px;font-weight:650;letter-spacing:.2px}
.sub{font-size:12px;color:var(--dim);margin-top:1px}
.hright{margin-left:auto;display:flex;gap:8px;align-items:center}
.chip{font-size:12px;border-radius:999px;padding:4px 12px;font-weight:600;
border:1px solid var(--line);color:var(--dim);background:var(--card)}
.chip.on{border-color:rgba(16,163,127,.5);color:var(--accent);background:var(--accent-soft)}
.chip.dry{border-color:rgba(224,163,65,.5);color:var(--warn)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;
padding:16px 18px;margin-bottom:14px}
.hero{display:flex;gap:26px;align-items:center;flex-wrap:wrap}
.ring{display:flex;flex-direction:column;align-items:center;gap:2px}
.ring svg{width:92px;height:92px;transform:rotate(-90deg)}
.ring .track{fill:none;stroke:#1e2529;stroke-width:8}
.ring .val{fill:none;stroke-width:8;stroke-linecap:round;transition:stroke-dashoffset .6s}
.ring .pct{position:relative;margin-top:-64px;margin-bottom:26px;font-size:17px;
font-weight:700;text-align:center;width:92px}
.ring .lbl{font-size:12px;color:var(--dim)}
.ring .reset{font-size:11px;color:var(--dim)}
.hinfo{flex:1;min-width:190px;display:flex;flex-direction:column;gap:8px}
.kv{display:flex;justify-content:space-between;font-size:13px;color:var(--dim);
border-bottom:1px dashed var(--line);padding-bottom:6px}
.kv b{color:var(--fg);font-weight:600}
.kv .ok{color:var(--accent)} .kv .bad{color:var(--danger)}
button{background:var(--card2);border:1px solid var(--line);color:var(--fg);
border-radius:10px;padding:7px 16px;font-size:13px;cursor:pointer;
transition:border-color .15s,transform .1s}
button:hover{border-color:var(--accent)} button:active{transform:scale(.97)}
.btn-main{background:var(--accent-soft);border-color:rgba(16,163,127,.4);color:var(--accent)}
.guide{font-size:12.5px;color:var(--dim);line-height:1.7}
.guide b{color:var(--fg)}
.guide code{background:var(--card2);border:1px solid var(--line);border-radius:6px;
padding:1px 7px;font-size:11.5px;color:var(--accent)}
.sect{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--dim);
text-transform:uppercase;letter-spacing:1px;margin:20px 2px 10px}
.sect:after{content:"";flex:1;height:1px;background:var(--line)}
.tcard{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:13px 16px;margin-bottom:10px;transition:border-color .15s}
.tcard.guard{border-color:rgba(16,163,127,.45)}
.tcard:hover{border-color:#31403a}
.thead{display:flex;align-items:center;gap:9px}
.dot{width:8px;height:8px;border-radius:50%;background:#39424a;flex:none}
.tcard.guard .dot{background:var(--accent);box-shadow:0 0 10px var(--accent);
animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
.ttl{font-weight:600;font-size:13.5px;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis;flex:1;min-width:0}
.badge{font-size:10px;border-radius:6px;padding:1.5px 7px;font-weight:600;flex:none}
.badge.goal{background:var(--accent-soft);color:var(--accent)}
.badge.gstat{background:#3a3320;color:var(--warn)}
.meta{font-size:11.5px;color:var(--dim);margin:5px 0 11px 17px}
.meta em{font-style:normal;color:#a8b3ba}
.controls{display:flex;gap:14px;align-items:center;flex-wrap:wrap;
padding-left:17px;border-top:1px solid var(--line);padding-top:11px}
.ctl{display:flex;align-items:center;gap:7px}
.ctl label{font-size:12px;color:var(--dim)}
.sw{position:relative;width:40px;height:22px;display:inline-block;flex:none}
.sw input{opacity:0;width:0;height:0}
.sw i{position:absolute;inset:0;background:#2c343a;border-radius:999px;
transition:.18s;cursor:pointer}
.sw i:before{content:"";position:absolute;width:16px;height:16px;border-radius:50%;
background:#aab4bb;top:3px;left:3px;transition:.18s}
.sw input:checked+i{background:var(--accent);box-shadow:0 0 10px rgba(16,163,127,.4)}
.sw input:checked+i:before{transform:translateX(18px);background:#fff}
input[type=text]{background:var(--card2);border:1px solid var(--line);color:var(--fg);
border-radius:9px;padding:6px 10px;font-size:12.5px;width:150px;transition:border-color .15s}
input[type=text]:focus{outline:none;border-color:var(--accent)}
input::placeholder{color:#5c666d}
select{background:var(--card2);border:1px solid var(--line);color:var(--fg);
border-radius:9px;padding:6px 8px;font-size:12.5px;width:104px}
select:focus{outline:none;border-color:var(--accent)}
.empty{color:var(--dim);font-size:13px;text-align:center;padding:14px 0}
#toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);
background:var(--accent-soft);color:var(--accent);border:1px solid rgba(16,163,127,.5);
padding:8px 18px;border-radius:11px;font-size:13px;opacity:0;transition:.25s;
pointer-events:none;box-shadow:0 6px 24px rgba(0,0,0,.4)}
#toast.show{opacity:1}
@media (max-width:680px){body{padding:12px 10px 30px}.hero{gap:14px}
.controls{gap:10px}input[type=text]{width:118px}}
</style></head><body>
<header>
 <div class="logo"><svg viewBox="0 0 64 64"><path d="M32 4 54 12v18c0 15-9.5 25.5-22 30C19.5 55.5 10 45 10 30V12Z"
 fill="none" stroke="#fff" stroke-width="5"/><path d="M21 32.5 29 40.5 43 25" fill="none"
 stroke="#fff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
 <div><h1>Codex 守护</h1><div class="sub">额度重置后自动继续任务 · 仅本机</div></div>
 <div class="hright" id="chips"></div>
</header>
<div class="card"><div class="hero" id="quota">
 <div style="color:var(--dim);font-size:13px">加载中…</div>
</div></div>
<div class="card guide">
 <b>怎么用：</b>在任意 Codex 对话里说
 <code>为当前对话开启守护</code>、
 <code>这个任务跨周也要继续</code>、
 <code>用 gpt-6.1-sol high 继续</code>即可；或在下方列表直接开关。
 开启守护的会话，只会在「<b>没有正在运行的任务 · 未被你暂停 · 5h 额度可用</b>》
 且被额度打断过时自动继续；goal 对话走官方“继续”按钮，绝不注入消息；
 “跨周”关闭时，周额度耗尽即永久停止守护。
</div>
<div class="sect">近期会话</div>
<div id="threads"></div>
<div id="toast"></div>
<script>
var eff=["","none","minimal","low","medium","high","xhigh"];
function fmt(v){return (v===null||v===undefined)?"—":v}
function fmtT(sec){if(!sec)return "";try{return new Date(sec*1000).toLocaleTimeString(
"zh-CN",{hour:"2-digit",minute:"2-digit"})}catch(e){return ""}}
function ring(pct,label,reset){
 var p=(pct===null||pct===undefined)?0:Math.min(100,pct);
 var C=213.6,off=C*(1-p/100);
 var col=(p>=100)?"#e5534b":(p>=70)?"#e0a341":"#10a37f";
 return '<div class="ring"><svg viewBox="0 0 76 76">'+
 '<circle class="track" cx="38" cy="38" r="34"/>'+
 '<circle class="val" cx="38" cy="38" r="34" stroke="'+col+
 '" stroke-dasharray="'+C+'" stroke-dashoffset="'+off+'"/></svg>'+
 '<div class="pct" style="color:'+col+'">'+fmt(pct)+"%</div>"+
 '<div class="lbl">'+label+"</div>"+
 (reset?'<div class="reset">重置 '+reset+"</div>":"")+"</div>"}
function toast(m){var t=document.getElementById("toast");t.textContent=m;
t.classList.add("show");setTimeout(function(){t.classList.remove("show")},1500)}
function api(p,o){var init=o?{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify(o)}:undefined;return fetch(p,init).then(function(r){return r.json()})}
function refresh(){
return api("/api/status").then(function(s){
window.__s=s;
var q=s.quota,qh="";
if(!q){
qh='<div style="flex:1;color:var(--dim);font-size:13px">额度快照暂不可用（网络波动），守护仍按重置时间正常工作</div>';
}else{
qh=ring(q.primary_used,"5 小时窗口",fmtT(q.primary_reset))+
ring(q.secondary_used,"周窗口",fmtT(q.secondary_reset))+
'<div class="hinfo">'+
'<div class="kv"><span>使用状态</span><b class="'+(q.allowed===false?"bad":"ok")+'">'+
(q.allowed===false?"额度受限":q.allowed===true?"✓ 可正常使用":"状态未知")+"</b></div>"+
'<div class="kv"><span>重置赠送</span><b>'+fmt(q.credits)+" 个</b></div>"+
'<div class="kv"><span>快照时间</span><b>'+(q.fetched_at?String(q.fetched_at).slice(11,16):"—")+"</b></div>"+
'<div style="margin-top:4px"><button class="btn-main" onclick="togglePause()">'+
(s.paused?"▶ 恢复守护":"⏸ 全部暂停")+"</button></div></div>";
}
document.getElementById("quota").innerHTML=qh;
document.getElementById("chips").innerHTML=
'<span class="chip '+(s.paused?"":"on")+'">'+(s.paused?"已暂停":"守护运行中")+"</span>"+
'<span class="chip '+(s.dry_run?"dry":"on")+'">'+(s.dry_run?"DRY-RUN":"LIVE")+"</span>";
var dl="";
if(s.models&&s.models.length){
dl='<datalist id="models">';
for(var i=0;i<s.models.length;i++){dl+='<option value="'+s.models[i].id+'">'+
s.models[i].name+"</option>"}
dl+="</datalist>";
}
var rows="";
var ts=s.threads||[];
for(var i=0;i<ts.length;i++){
var t=ts[i];
var badges=(t.is_goal?'<span class="badge goal">GOAL</span>':"")+
(t.goal_status?'<span class="badge gstat">'+t.goal_status+"</span>":"");
var tries=(t.attempts&&t.attempts>t.resumes)?"（尝试 "+t.attempts+"）":"";
rows+=(i===0?dl:"")+
'<div class="tcard '+(t.guard?"guard":"")+'">'+
'<div class="thead"><span class="dot"></span><span class="ttl">'+
((t.title||"")||String(t.id||"").slice(0,13))+badges+"</span></div>"+
'<div class="meta">最近 '+fmt(t.updated)+" · 成功恢复 <em>"+fmt(t.resumes)+
"</em> 次"+tries+
(t.next_wake?" · 下次检查 "+String(t.next_wake).replace("T"," ").slice(5,16):"")+
(t.last_outcome?" · "+t.last_outcome:"")+"</div>"+
'<div class="controls">'+
'<div class="ctl"><label class="sw"><input type="checkbox"'+(t.guard?" checked":"")+
' onchange="setT(\''+t.id+'\',{guard:this.checked})"><i></i></label>'+
'<label>守护</label></div>'+
'<div class="ctl"><label class="sw"><input type="checkbox"'+(t.weekly?" checked":"")+
' onchange="setT(\''+t.id+'\',{weekly:this.checked})"><i></i></label>'+
'<label>跨周</label></div>'+
'<div class="ctl"><label>模型</label><input type="text" list="models" value="'+
(t.model||"")+'" placeholder="继承线程" onchange="setT(\''+t.id+'\',{model:this.value})"></div>'+
'<div class="ctl"><label>Reasoning</label><select onchange="setT(\''+t.id+
'\',{effort:this.value})">';
for(var j=0;j<eff.length;j++){rows+='<option '+((eff[j]===(t.effort||""))?"selected":"")+
' value="'+eff[j]+'">'+(eff[j]||"继承")+"</option>"}
rows+="</select></div></div></div>";
}
document.getElementById("threads").innerHTML=rows||
'<div class="card empty">暂无近期会话——在 Codex 里打开一个对话后刷新即可</div>';
}).catch(function(){
document.getElementById("quota").innerHTML=
'<div style="color:var(--danger);font-size:13px">守护进程未运行（连接失败）。启动：schtasks /Run /TN codex-autoresume</div>'})}
function setT(id,o){api("/api/threads/"+id,o).then(function(){toast("已更新");refresh()})}
function togglePause(){var s=window.__s;api("/api/pause",{paused:!s.paused}).then(
function(){toast(s.paused?"守护已恢复":"守护已暂停");refresh()})}
refresh();setInterval(refresh,15000);
</script></body></html>"""




class ControlApi:
    def __init__(self, daemon):
        self.daemon = daemon
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self, host: str, port: int) -> None:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):      # silence default stderr noise
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type",
                                 "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    body = PANEL_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/api/status":
                    self._json(api.status())
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = {}
                if length:
                    try:
                        payload = json.loads(
                            self.rfile.read(length).decode("utf-8"))
                    except ValueError:
                        self._json({"error": "bad json"}, 400)
                        return
                if self.path == "/api/pause":
                    self._json(api.set_paused(bool(payload.get("paused"))))
                    return
                if self.path.startswith("/api/threads/"):
                    tid = self.path[len("/api/threads/"):].strip("/")
                    self._json(api.set_thread(tid, payload))
                    return
                self._json({"error": "not found"}, 404)

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="cxr-control", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd = None

    # ------------------------------------------------------------- handlers

    def status(self) -> dict:
        d = self.daemon
        st = d.state
        from .codexenv import CodexState
        try:
            codex = CodexState(d.codex_home)
            threads = codex.list_threads(limit=25)
            goals = {g["thread_id"]: g for g in codex.goals()}
        except Exception:
            threads, goals = [], {}
        out_threads = []
        for info in threads:
            t = st.threads.get(info.id)
            goal = goals.get(info.id)
            tc = d._effective_thread_config(info.id, t)
            out_threads.append({
                "id": info.id,
                "title": (info.name or info.title or "").replace("\n", " ")[:60],
                "is_goal": goal is not None or bool(t and t.is_goal),
                "goal_status": (goal or {}).get("status"),
                "guard": bool(t and (t.tracked or t.manual)),
                "weekly": bool(tc.get("weekly_resume")),
                "model": tc.get("model") or "",
                "effort": tc.get("reasoning_effort") or "",
                "resumes": (t.success_resumes if t else 0),
                "attempts": (t.total_resumes if t else 0),
                "last_outcome": t.last_outcome if t else "",
                "next_wake": (t.next_wake_iso[:19] if t and t.next_wake_iso
                              else None),
                "updated": _fmt_ms(info.updated_at_ms),
            })
        snap = d._snapshot
        quota = None
        if snap:
            quota = {
                "primary_used": snap.primary.used_percent,
                "primary_reset": snap.primary.resets_at,
                "secondary_used": snap.secondary.used_percent,
                "secondary_reset": snap.secondary.resets_at,
                "credits": len(snap.reset_credits),
                "allowed": snap.ordinary_usage_allowed,
                "fetched_at": getattr(d, "_snapshot_wall", None),
            }
        return {
            "paused": bool(st and st.paused),
            "dry_run": bool(d.cfg["general"]["dry_run"]),
            "quota": quota,
            "models": d.model_catalog(),
            "threads": out_threads,
        }

    def set_paused(self, paused: bool) -> dict:
        st = self.daemon.state
        st.paused = paused
        save_state(st)
        self.daemon._state_mtime = os.path.getmtime(st.state_path)
        return {"paused": paused}

    def set_thread(self, thread_id: str, payload: dict) -> dict:
        st = self.daemon.state
        t = st.thread(thread_id)
        if "guard" in payload:
            t.manual = bool(payload["guard"])
            t.tracked = bool(payload["guard"])
            if payload["guard"] and not t.last_kind:
                t.last_kind = "usage_primary"
        if "weekly" in payload:
            t.guard_weekly = bool(payload["weekly"])
        if "model" in payload:
            t.guard_model = str(payload["model"] or "") or None
        if "effort" in payload:
            t.guard_effort = str(payload["effort"] or "") or None
        save_state(st)
        self.daemon._state_mtime = os.path.getmtime(st.state_path)
        return {"thread": thread_id, "guard": t.manual,
                "weekly": t.guard_weekly, "model": t.guard_model,
                "effort": t.guard_effort}


def _fmt_ms(ms: int | None) -> str | None:
    if not ms:
        return None
    from datetime import datetime
    return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")

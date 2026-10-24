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
<title>codex-autoresume 守护控制台</title>
<style>
:root{--bg:#0d0d0d;--card:#171717;--line:#2a2a2a;--fg:#ececec;--dim:#9b9b9b;
--accent:#10a37f;--accent2:#e0a341;--danger:#e5534b}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,
"Segoe UI","Microsoft YaHei",sans-serif;padding:18px;max-width:880px;margin:0 auto}
h1{font-size:16px;font-weight:600;display:flex;align-items:center;gap:10px}
h1 .dot{width:9px;height:9px;border-radius:50%;background:var(--accent);
box-shadow:0 0 8px var(--accent)}
.sub{color:var(--dim);font-size:12px;margin:4px 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:14px 16px;margin-bottom:12px}
.quota{display:flex;gap:22px;flex-wrap:wrap;align-items:center}
.qitem{flex:1;min-width:180px}
.qitem .lbl{font-size:12px;color:var(--dim);display:flex;justify-content:space-between}
.bar{height:6px;border-radius:3px;background:#262626;margin-top:5px;overflow:hidden}
.bar>i{display:block;height:100%;border-radius:3px;background:var(--accent)}
.bar.warn>i{background:var(--accent2)}.bar.full>i{background:var(--danger)}
.chips{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
.chip{font-size:12px;border:1px solid var(--line);border-radius:999px;
padding:3px 10px;color:var(--dim)}
.chip.on{border-color:var(--accent);color:var(--accent)}
button{background:#222;border:1px solid var(--line);color:var(--fg);
border-radius:9px;padding:6px 14px;font-size:13px;cursor:pointer}
button:hover{border-color:var(--accent)}
.row{display:flex;align-items:center;gap:12px;padding:11px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}
.ttl{flex:1;min-width:0}
.ttl b{display:block;font-weight:600;font-size:13px;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.ttl span{font-size:11px;color:var(--dim)}
.badge{font-size:10px;border-radius:5px;padding:1px 6px;margin-left:6px;
background:#243b33;color:var(--accent);vertical-align:2px}
.sw{position:relative;width:38px;height:21px;flex:none}
.sw input{opacity:0;width:0;height:0}
.sw i{position:absolute;inset:0;background:#333;border-radius:999px;
transition:.18s;cursor:pointer}
.sw i:before{content:"";position:absolute;width:15px;height:15px;border-radius:50%;
background:#bbb;top:3px;left:3px;transition:.18s}
.sw input:checked+i{background:var(--accent)}
.sw input:checked+i:before{transform:translateX(17px);background:#fff}
.ctl{display:flex;flex-direction:column;gap:3px;align-items:center}
.ctl label{font-size:10px;color:var(--dim)}
select,input[type=text]{background:#101010;border:1px solid var(--line);
color:var(--fg);border-radius:7px;padding:4px 7px;font-size:12px;width:118px}
.foot{color:var(--dim);font-size:11px;text-align:center;margin-top:6px}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);
background:#1f2f28;color:var(--accent);border:1px solid var(--accent);
padding:7px 16px;border-radius:10px;font-size:13px;opacity:0;transition:.25s;pointer-events:none}
#toast.show{opacity:1}
@media (max-width:680px){body{padding:10px;max-width:none}
.row{flex-wrap:wrap;gap:8px}.ttl{flex-basis:100%}
.quota{gap:10px}.qitem{min-width:140px;flex:1 1 45%}
h1{font-size:14px}}
</style></head><body>
<h1><span class="dot"></span>codex-autoresume <span style="color:var(--dim);
font-weight:400">守护控制台</span></h1>
<div class="sub">会话额度重置后自动继续 · 仅本机 127.0.0.1 ·
<a href="#" onclick="refresh();return false" style="color:var(--accent)">刷新</a></div>
<div class="card"><div class="quota" id="quota"></div>
<div class="chips" id="chips"></div></div>
<div class="card" id="threads"></div>
<div class="foot">守护仅在「任务未在运行/未暂停 且 5h 额度可用」时触发 ·
goal 线程通过官方继续按钮语义恢复（thread/goal/set，绝不注入消息）·
跨周关闭时周额度耗尽即永久停止<br>
用法：在 Codex 对话里直接说「为当前对话开启守护」「打开守护面板」，或在此页面开关</div>
<div id="toast"></div>
<script>
const eff=["","none","minimal","low","medium","high","xhigh"];
function fmt(v){return (v===null||v===undefined)?"—":v}
function fmtT(s){if(!s)return "";try{
return new Date(s*1000).toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit"})}catch(e){return ""}}
async function api(p,o){const r=await fetch(p,o?{method:"POST",
headers:{"Content-Type":"application/json"},body:JSON.stringify(o)}:undefined);
return r.json()}
function toast(m){const t=document.getElementById("toast");t.textContent=m;
t.classList.add("show");setTimeout(function(){t.classList.remove("show")},1400)}
function bar(v){const w=(v===null||v===undefined)?0:Math.min(100,v);
const c=(v!==null&&v!==undefined&&v>=100)?"full":(v!==null&&v!==undefined&&v>=70)?"warn":"";
return '<div class="bar '+c+'"><i style="width:'+w+'%"></i></div>'}
async function refresh(){
let s;try{s=await api("/api/status")}catch(e){
document.getElementById("quota").innerHTML=
'<div class="qitem"><div class="lbl"><span>守护进程</span></div>'+
'<div style="font-size:13px;color:var(--danger);margin-top:6px">连接失败——守护进程未运行？</div></div>';return}
const q=s.quota;
let qh="";
if(!q){
qh='<div class="qitem"><div class="lbl"><span>额度快照</span></div>'+
'<div style="font-size:12px;color:var(--dim);margin-top:6px">暂不可用（网络波动），守护仍按重置时间工作</div></div>';
}else{
const r1=fmtT(q.primary_reset),r2=fmtT(q.secondary_reset);
qh='<div class="qitem"><div class="lbl"><span>5 小时窗口</span><span>'+fmt(q.primary_used)+
"%"+(r1?" · 重置 "+r1:"")+"</span></div>"+bar(q.primary_used)+"</div>"+
'<div class="qitem"><div class="lbl"><span>周窗口</span><span>'+fmt(q.secondary_used)+"%</span></div>"+
bar(q.secondary_used)+"</div>"+
'<div class="qitem"><div class="lbl"><span>重置赠送</span><span>'+fmt(q.credits)+" 个</span></div>"+
'<div style="font-size:12px;color:var(--dim);margin-top:6px">'+
(q.allowed===false?"⚠️ 额度受限":q.allowed===true?"✓ 可正常使用":"额度状态未知")+
(q.fetched_at?" · 快照 "+String(q.fetched_at).slice(11,16):"")+"</div></div>";
}
document.getElementById("quota").innerHTML=qh;
document.getElementById("chips").innerHTML=
'<span class="chip '+(s.paused?"":"on")+'">'+(s.paused?"⏸ 已暂停":"● 守护中")+"</span>"+
'<span class="chip '+(s.dry_run?"":"on")+'">'+(s.dry_run?"DRY-RUN（不发送）":"LIVE")+"</span>"+
'<button onclick="api(\'/api/pause\',{paused:'+(s.paused?false:true)+'}).then(function(){toast(s.paused?\'守护已恢复\':\'守护已暂停\');refresh()})">'+
(s.paused?"恢复守护":"全部暂停")+"</button>";
const rows=(s.threads||[]).map(function(t){
const gs=t.goal_status?'<span class="badge" style="background:#3b3324;color:#e0a341">'+t.goal_status+"</span>":"";
return '<div class="row">'+
' <div class="ttl"><b>'+((t.title||"")||(t.id||"").slice(0,13))+
(t.is_goal?'<span class="badge">GOAL</span>':"")+gs+"</b>"+
'<span>最近 '+fmt(t.updated)+" · 恢复 "+fmt(t.resumes)+" 次"+
(t.next_wake?" · 下次 "+String(t.next_wake).replace("T"," ").slice(5,16):"")+
(t.last_outcome?" · "+t.last_outcome:"")+"</span></div>"+
' <div class="ctl"><label>守护</label><label class="sw"><input type="checkbox"'+
(t.guard?" checked":"")+' onchange="setT(\''+t.id+'\',{guard:this.checked})"><i></i></label></div>'+
' <div class="ctl"><label>跨周</label><label class="sw"><input type="checkbox"'+
(t.weekly?" checked":"")+' onchange="setT(\''+t.id+'\',{weekly:this.checked})"><i></i></label></div>'+
' <div class="ctl"><label>模型</label><input type="text" value="'+(t.model||"")+
'" placeholder="继承线程" onchange="setT(\''+t.id+'\',{model:this.value})"></div>'+
' <div class="ctl"><label>Reasoning</label><select onchange="setT(\''+t.id+'\',{effort:this.value})">'+
eff.map(function(e){return '<option '+((e===(t.effort||""))?"selected":"")+' value="'+e+'">'+(e||"继承")+"</option>"}).join("")+
"</select></div></div>"}).join("");
document.getElementById("threads").innerHTML=rows||
'<div class="sub" style="padding:8px 0">暂无近期会话——打开一个对话后刷新即可看到</div>';}
async function setT(id,o){await api("/api/threads/"+id,o);toast("已更新");refresh()}
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
                "resumes": t.total_resumes if t else 0,
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

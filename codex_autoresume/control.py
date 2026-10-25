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
"Microsoft YaHei",sans-serif;padding:20px 18px 44px;max-width:940px;margin:0 auto;
-webkit-font-smoothing:antialiased}
header{display:flex;align-items:center;gap:12px;margin-bottom:16px}
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
padding:16px 18px;margin-bottom:13px}
.hero{display:flex;gap:28px;align-items:center;flex-wrap:wrap}
.ringcol{display:flex;flex-direction:column;align-items:center;gap:12px}
.ringrow{display:flex;gap:18px}
.ringwrap{display:flex;flex-direction:column;align-items:center;gap:4px}
.ring{position:relative;width:92px;height:92px}
.ring svg{width:92px;height:92px;transform:rotate(-90deg);display:block}
.ring .track{fill:none;stroke:#1e2529;stroke-width:8}
.ring .val{fill:none;stroke-width:8;stroke-linecap:round;transition:stroke-dashoffset .6s}
.ring .pct{position:absolute;inset:0;display:flex;align-items:center;
justify-content:center;font-size:16px;font-weight:700}
.ringlbl{font-size:12px;color:var(--dim)}
.ringreset{font-size:11px;color:var(--dim)}
.hinfo{flex:1;min-width:230px;display:flex;flex-direction:column;gap:7px}
.kv{display:flex;justify-content:space-between;font-size:13px;color:var(--dim);
border-bottom:1px dashed var(--line);padding-bottom:5px;gap:12px}
.kv b{color:var(--fg);font-weight:600;text-align:right}
.kv{align-items:center}
.kv>span:first-child{flex:none;white-space:nowrap}
.kv>span:last-child{flex:1;min-width:0;text-align:right;white-space:normal;overflow-wrap:anywhere}
.kv.news>span:last-child{font-size:12px;text-align:left}
.newsbox{flex:1;min-width:0;max-height:150px;overflow-y:auto;
background:var(--card2);border:1px solid var(--line);border-radius:10px;
padding:8px 12px;font-size:12px;line-height:1.8;font-family:Consolas,
"Courier New",monospace;text-align:left}
.newsbox .nl{white-space:normal;overflow-wrap:anywhere}
.newsbox .d{color:var(--accent);font-weight:700;margin-right:8px;flex:none;
background:#12202b;border:1px solid #1e3a4d;border-radius:6px;
padding:0 6px;margin-right:10px}
.hl-ok{color:var(--accent);font-weight:600}
.hl-warn{color:var(--warn);font-weight:600}
.hl-t{color:#7ec8ff}
.chip{white-space:nowrap}
.kv .ok{color:var(--accent)} .kv .bad{color:var(--danger)} .kv .eta{color:var(--warn)}
button{background:var(--card2);border:1px solid var(--line);color:var(--fg);
border-radius:10px;padding:7px 16px;font-size:13px;cursor:pointer;
transition:border-color .15s,transform .1s}
button:hover{border-color:var(--accent)} button:active{transform:scale(.97)}
.btn-main{background:var(--accent-soft);border-color:rgba(16,163,127,.4);color:var(--accent)}
.btn-mini{padding:5px 10px;font-size:12px;border-radius:8px}
.sect{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--dim);
text-transform:uppercase;letter-spacing:1px;margin:18px 2px 10px}
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
.controls{display:flex;gap:11px;align-items:center;flex-wrap:wrap;
padding-left:17px;border-top:1px solid var(--line);padding-top:11px}
.controls2{display:flex;gap:11px;align-items:center;flex-wrap:wrap;
padding-left:17px;margin-top:9px}
.ctl{display:flex;align-items:center;gap:7px;position:relative}
.ctl label{font-size:12px;color:var(--dim)}
.tip{position:relative;cursor:help}
.tip:after{content:attr(data-tip);position:absolute;bottom:calc(100% + 8px);
left:50%;transform:translateX(-50%);background:#20272c;color:#dfe6ea;
font-size:11.5px;line-height:1.5;padding:7px 11px;border-radius:9px;
white-space:normal;width:220px;border:1px solid var(--line);
box-shadow:0 8px 22px rgba(0,0,0,.45);opacity:0;pointer-events:none;
transition:opacity .15s;z-index:9999;text-align:left}
.tip:hover:after{opacity:1}
.tip.end:after{left:auto;right:-8px;transform:none}
.tip.start:after{left:-8px;transform:none}
.sw{position:relative;width:40px;height:22px;display:inline-block;flex:none}
.sw input{opacity:0;width:0;height:0}
.sw i{position:absolute;inset:0;background:#2c343a;border-radius:999px;
transition:.18s;cursor:pointer}
.sw i:before{content:"";position:absolute;width:16px;height:16px;border-radius:50%;
background:#aab4bb;top:3px;left:3px;transition:.18s}
.sw input:checked+i{background:var(--accent);box-shadow:0 0 10px rgba(16,163,127,.4)}
.sw input:checked+i:before{transform:translateX(18px);background:#fff}
input[type=text]{background:var(--card2);border:1px solid var(--line);color:var(--fg);
border-radius:9px;padding:5px 8px;font-size:12px;width:126px;transition:border-color .15s}
input[type=text]:focus{outline:none;border-color:var(--accent)}
input::placeholder{color:#5c666d}
select{background:var(--card2);border:1px solid var(--line);color:var(--fg);
border-radius:9px;padding:6px 8px;font-size:12.5px;width:126px}
select:focus{outline:none;border-color:var(--accent)}
.prow{display:flex;gap:8px;align-items:center;padding:10px 0 0 17px;
border-top:1px dashed var(--line);margin-top:10px;flex-wrap:wrap}
.prow input{flex:1;min-width:220px}
.empty{color:var(--dim);font-size:13px;text-align:center;padding:14px 0}
.foot{color:#5f6a71;font-size:11px;text-align:center;margin-top:14px;line-height:1.7}
.foot code{color:#3f4a52;background:none;padding:0}
#toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);
background:var(--accent-soft);color:var(--accent);border:1px solid rgba(16,163,127,.5);
padding:8px 18px;border-radius:11px;font-size:13px;opacity:0;transition:.25s;
pointer-events:none;box-shadow:0 6px 24px rgba(0,0,0,.4)}
#toast.show{opacity:1}
@media (max-width:680px){body{padding:12px 10px 30px}.hero{gap:14px}
.controls,.controls2{gap:10px}input[type=text]{width:118px}}
</style></head><body>
<header>
 <div class="logo"><svg viewBox="0 0 64 64"><path d="M32 4 54 12v18c0 15-9.5 25.5-22 30C19.5 55.5 10 45 10 30V12Z"
 fill="none" stroke="#fff" stroke-width="5"/><path d="M21 32.5 29 40.5 43 25" fill="none"
 stroke="#fff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
 <div><h1 data-i18n="title">Codex 守护</h1><div class="sub" data-i18n="sub">额度重置后自动继续任务 · 仅本机</div></div>
 <div class="hright" id="chips"></div>
 <button class="btn-mini" style="flex:none;min-width:44px" id="langBtn" onclick="toggleLang()">CN</button>
 <button class="btn-mini" style="flex:none" data-i18n="refresh" onclick="refresh();toast(L('refreshed'))">刷新</button>
</header>
<div class="card"><div class="hero" id="quota">
 <div id="ld" style="color:var(--dim);font-size:13px">加载中…</div>
</div>
</div>
<div class="sect" data-i18n="recent">近期会话</div>
<div id="threads"></div>
<div class="foot" id="foot"></div>
<div id="toast"></div>
<script>
var eff=["","none","minimal","low","medium","high","xhigh"];
var LNG="zh";try{LNG=localStorage.getItem("cglang")||"zh"}catch(e){}
var I18N={
zh:{title:"Codex 守护",sub:"额度重置后自动继续任务 · 仅本机",refresh:"刷新",loading:"加载中…",
w5:"5 小时窗口",wk:"周窗口",reset:"重置",status:"使用状态",limited:"额度受限",
avail:"✓ 可正常使用",unknown:"状态未知",eta5:"5h 额度预估耗尽",etaw:"周额度预估耗尽",
credits:"重置赠送",forecast:"预告",landed:"到账",
news:"动态",pauseAll:"⏸ 全部暂停",resumeAll:"▶ 全部继续",running:"守护运行中",
pausedC:"已暂停",recent:"近期会话",
noThread:"暂无近期会话——在 Codex 里打开一个对话后刷新即可",
guard:"守护",weekly:"跨周",card:"重置卡",noneC:"（无）",expirePfx:"（到期 ",
model:"模型",inherit:"继承",prompt:"继续指令",resetD:"恢复默认",
lastUpd:"最近",resumed:"成功恢复",times:" 次",tryPfx:"（尝试 ",trySfx:"）",
nextChk:"下次检查",
tipGuard:"打开则表示 5h 额度耗尽后自动继续任务",
tipWeekly:"打开则表示守护开启时周额度重置后也自动继续任务",
tipCard:"周额度耗尽且账户有官方重置卡时，自动消耗一张以继续任务",
quotaNA:"额度快照暂不可用（网络波动），守护仍按重置时间正常工作",
postAt:"动态发布时间",exhausted:"已耗尽",newsWin:"近 48 小时",snapAge:"快照",g_usage_limited:"额度受限",g_budget_limited:"预算受限",g_active:"运行中",
g_paused:"已暂停",g_blocked:"受阻",g_complete:"已完成",
goalOK:"✓ 已自动继续",uiaOK:"✓ 已点继续",writerBusy:"会话被 Codex 应用占用，切走/关闭后自动接管",quotaWait:"等待额度",
errConn:"无法连接守护进程：",errTimeout:"请求超时（8 秒无响应）",errFail:"连接失败",
errHint:"。请点右上角「刷新」重试；若反复失败，启动：",
upd:"已更新",restored:"已恢复默认指令",pauseOK:"守护已暂停",resumeOK:"守护已恢复",
refreshed:"已刷新",
footer:"打开方式：侧栏输入 <code>http://127.0.0.1:8765</code>（推荐，不消耗额度）· 桌面快捷方式 · 完整说明见插件 README"},
en:{title:"Codex Guard",sub:"Auto-continue conversations after quota resets",
refresh:"Refresh",loading:"Loading…",w5:"5h window",wk:"Weekly",reset:"Resets",
status:"Usage",limited:"Limited",avail:"✓ Available",unknown:"Unknown",
eta5:"5h quota ETA",etaw:"Weekly quota ETA",
credits:"Reset credits",forecast:"Forecast",landed:"Landed",
news:"News",pauseAll:"⏸ Pause all",resumeAll:"▶ Resume all",
running:"Watching",pausedC:"Paused",recent:"Recent conversations",
noThread:"No recent conversations — open one in Codex, then refresh",
guard:"Auto-resume",weekly:"Weekly",card:"Credits",noneC:"(none)",
expirePfx:" (expires ",model:"Model",inherit:"Inherit",prompt:"Resume prompt",
resetD:"Reset",lastUpd:"Last",resumed:"resumed",times:"×",tryPfx:" (",
trySfx:" tries)",nextChk:"next check",
tipGuard:"On: automatically continue this thread when the 5h quota resets",
tipWeekly:"On: also continue after the weekly quota resets (Auto-resume on)",
tipCard:"When the weekly quota runs out, spend a reset credit to keep going",
quotaNA:"Quota snapshot unavailable — resets are still tracked",
postAt:"post publication time",exhausted:"Exhausted",newsWin:"last 48h",snapAge:"snapshot",g_usage_limited:"quota-limited",g_budget_limited:"budget-limited",g_active:"active",
g_paused:"paused",g_blocked:"blocked",g_complete:"complete",
goalOK:"✓ auto-continued",uiaOK:"✓ Continue clicked",writerBusy:"held by the Codex app; guard resumes once released",quotaWait:"waiting for quota",
errConn:"Cannot reach guard daemon: ",errTimeout:"request timed out (8s)",
errFail:"connection failed",
errHint:". Click Refresh (top-right); if it persists start: ",
upd:"Saved",restored:"Prompt reset to default",pauseOK:"Paused all",
resumeOK:"Resumed all",refreshed:"Refreshed",
footer:"Open: type <code>http://127.0.0.1:8765</code> in the sidebar (recommended, no quota cost) · desktop shortcut · full docs in the plugin README"}};
function L(k){var d=I18N[LNG]||I18N.zh;var v=d[k];
return (v===undefined)?(I18N.zh[k]||k):v}
function p2(n){return (n<10?"0":"")+n}
function u(d){return p2(d.getMonth()+1)+"-"+p2(d.getDate())+" "+
p2(d.getHours())+":"+p2(d.getMinutes())}
function fmtHM(sec){if(!sec)return "";return u(new Date(sec*1000))}
function fmtMDHM(sec){if(!sec)return "";return u(new Date(sec*1000))}
function fmtISO(iso){if(!iso)return "";
if(String(iso).indexOf("T")<0)return String(iso);
var d=new Date(iso);if(isNaN(d.getTime()))return String(iso);
return u(d)}
function fmtMS(ms){if(!ms)return "";return u(new Date(ms))}
function gstat(x){var k="g_"+String(x||"");var v=I18N[LNG][k];
return (v===undefined)?String(x||""):v}
function fmt(v){return (v===null||v===undefined)?"--":v}
function lerp(a,b,t){return Math.round(a+(b-a)*t)}
function hex2rgb(h){return [parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)]}
function ringColor(p){
 var G=hex2rgb("#10a37f"),A=hex2rgb("#e0a341"),R=hex2rgb("#e5534b"),c;
 if(p<=50){c=G}else if(p<=80){var t=(p-50)/30;c=[lerp(G[0],A[0],t),lerp(G[1],A[1],t),lerp(G[2],A[2],t)]}
 else{var t2=(p-80)/20;c=[lerp(A[0],R[0],t2),lerp(A[1],R[1],t2),lerp(A[2],R[2],t2)]}
 return "rgb("+c[0]+","+c[1]+","+c[2]+")"}
function ring(pct,label,reset){
 var p=(pct===null||pct===undefined)?0:Math.min(100,pct);
 var C=213.6,off=C*(1-p/100);
 var col=ringColor(p);
 return '<div class="ringwrap"><div class="ring"><svg viewBox="0 0 76 76" style="filter:drop-shadow(0 0 5px '+col+'55)">'+
 '<circle class="track" cx="38" cy="38" r="34"/>'+
 '<circle class="val" cx="38" cy="38" r="34" stroke="'+col+
 '" stroke-dasharray="'+C+'" stroke-dashoffset="'+off+'"/></svg>'+
 '<div class="pct" style="color:'+col+'">'+fmt(pct)+"%</div></div>"+
 '<div class="ringlbl">'+label+"</div>"+
 (reset?'<div class="ringreset">'+L("reset")+" "+reset+"</div>":"")+"</div>"}
function etaRow(label,q,etaK,rateK,exhK){
 var eta=q[etaK],rate=q[rateK];
 var b=(eta?('<b class="eta">'+fmtMDHM(eta)+"</b>")
 :(exhK&&q[exhK]&&q.secondary_used>=100?
   ('<b class="eta">'+L("exhausted")+" · "+fmtMDHM(q[exhK])+"</b>")
 :"<b>--</b>"));
 var tail=(rate?(' <span style="font-size:11px">（'+rate+"%/h）</span>"):"");

 return '<div class="kv"><span>'+label+"</span><span>"+b+tail+"</span></div>"}
function kvRow(k,v){return '<div class="kv"><span>'+k+"</span><span>"+v+"</span></div>"}
function toast(m){var t=document.getElementById("toast");t.textContent=m;
t.classList.add("show");setTimeout(function(){t.classList.remove("show")},1500)}
function api(p,o){var init=o?{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify(o)}:undefined;
var tp=new Promise(function(_,rej){setTimeout(function(){rej(new Error("timeout"))},8000)});
return Promise.race([fetch(p,init).then(function(r){return r.json()}),tp])}
function modelOpts(cur){var o="";var ms=(window.__s&&window.__s.models)||[];
for(var k=0;k<ms.length;k++){o+='<option '+((cur||"")===ms[k].id?"selected":"")+' value="'+esc(ms[k].id)+'">'+esc(ms[k].name)+"</option>"}
return o}
function esc(s){return String(s===null||s===undefined?"":s).replace(/&/g,"&amp;")
.replace(/</g,"&lt;").replace(/"/g,"&quot;")}
function hlNews(t){
var x=esc(t);
x=x.replace(/(Landed as announced[^,.]{0,40})/gi,'<span class="hl-ok">$1</span>');
x=x.replace(/\b(Landing|tomorrow|before)\b/gi,'<span class="hl-warn">$1</span>');
x=x.replace(/(\d{1,2}:\d{2})\s*(AM|PM|UTC|PST|PDT|Pacific Time)?/gi,
function(m){return '<span class="hl-t">'+m+'</span>'});
return x}
function newsBox(s){
 var ann=s.announcement||null;
 var es=(s.news_log&&s.news_log.length)?s.news_log:
 (ann&&ann.status?[{at:"",text:ann.status,kind:ann.status_kind}]:[]);
 if(!es.length){return ""}
 var rows="";
 for(var i=0;i<es.length;i++){
 var e=es[i];
 rows+='<div class="nl">'+(e.at?'<span class="d" title="'+L("postAt")+'">'+
 fmtPostAt(e.at)+"]</span>":"")+
 hlNews(e.text)+"</div>"}
 return '<div class="kv"><span>'+L("news")+'</span><span style="font-size:11px">'+
 L("newsWin")+"</span></div>"+'<div class="newsbox" id="newsbox">'+rows+"</div>"}
function snapAge(q){
 if(!q||!q.fetched_at)return "";
 var t=new Date(q.fetched_at);if(isNaN(t.getTime()))return "";
 var n=Math.max(0,Math.round((Date.now()-t.getTime())/1000));
 var w=(n<60)?(n+"s"):Math.round(n/60)+"min";
 return L("snapAge")+" "+w}
function fmtPostAt(iso){
 if(String(iso).indexOf("Z")>=0||String(iso).indexOf("+00:00")>=0){
 var d=new Date(iso);
 if(!isNaN(d.getTime())){return p2(d.getUTCMonth()+1)+"-"+p2(d.getUTCDate())+" "+
 p2(d.getUTCHours())+":"+p2(d.getUTCMinutes())+" UTC"}}
 return fmtISO(iso)}
function annRowsFor(s){
 var ann=s.announcement||null,rows="";
 var fct="--";
 if(ann&&ann.status_kind==="upcoming"&&ann.status_short){
 fct=esc(ann.status_short)}
 rows+=kvRow(L("forecast"),'<b class="eta">'+fct+"</b>");
 var cand=[];
 var evs=s.reset_events||[];
 for(var i=0;i<evs.length;i++){if(evs[i].kind==="weekly"||evs[i].kind==="credit")cand.push(evs[i])}
 var ces=s.credit_events||[];
 for(var j=0;j<ces.length;j++){cand.push({kind:"credit",at:ces[j].landed_at,title:ces[j].title})}
 var landTxt="--";
 if(cand.length){
 cand.sort(function(a,b){return String(b.at).localeCompare(String(a.at))});
 var e0=cand[0];
 landTxt=fmtISO(e0.at)+" · "+(e0.kind==="weekly"?L("wk"):L("card"))}
 rows+=kvRow(L("landed"),'<b class="ok">'+esc(landTxt)+"</b>");
 rows+=newsBox(s)
 return rows}
function refresh(){
return api("/api/status").then(function(s){
window.__s=s;
var q=s.quota,qh="";
var annRows=annRowsFor(s);
if(!q){
qh='<div style="flex:1;color:var(--dim);font-size:13px">'+L("quotaNA")+"</div>";
}else{
qh='<div class="ringcol"><div class="ringrow">'+
ring(q.primary_used,L("w5"),fmtHM(q.primary_reset))+
ring(q.secondary_used,L("wk"),fmtMDHM(q.secondary_reset))+
'</div><div style="font-size:11px;color:var(--dim);text-align:center" id="snapage">'+
snapAge(q)+'</div><button class="btn-main" style="width:100%" onclick="togglePause()">'+
(s.paused?L("resumeAll"):L("pauseAll"))+"</button></div>"+
'<div class="hinfo">'+
kvRow(L("status"),'<b class="'+(q.allowed===false?"bad":"ok")+'">'+
(q.allowed===false?L("limited"):q.allowed===true?L("avail"):L("unknown"))+"</b>")+
etaRow(L("eta5"),q,"eta_primary","rate_primary")+
etaRow(L("etaw"),q,"eta_secondary","rate_secondary","secondary_reset")+
kvRow(L("credits"),'<b>'+fmt((q.credits_count!==undefined)?q.credits_count:q.credits)+"</b>")+
annRows+"</div>";
}
document.getElementById("quota").innerHTML=qh;
document.getElementById("chips").innerHTML=
'<span class="chip '+(s.paused?"":"on")+'">'+(s.paused?L("pausedC"):L("running"))+"</span>"+
'<span class="chip '+(s.dry_run?"dry":"on")+'">'+(s.dry_run?"DRY-RUN":"LIVE")+"</span>";
var dl="";
if(s.models&&s.models.length){
dl='<datalist id="models">';
for(var i=0;i<s.models.length;i++){dl+='<option value="'+esc(s.models[i].id)+'">'+
esc(s.models[i].name)+"</option>"}
dl+="</datalist>";
}
var defp=s.default_prompt||"";
var rows="";
var ts=s.threads||[];
for(var i=0;i<ts.length;i++){
var t=ts[i];
var badges=(t.is_goal?'<span class="badge goal">GOAL</span>':"")+
(t.goal_status?'<span class="badge gstat">'+esc(gstat(t.goal_status))+"</span>":"");
var tries=(t.attempts&&t.attempts>t.resumes)?(L("tryPfx")+t.attempts+L("trySfx")):"";
var oc=t.last_outcome;
var ocMap={"goal_continue":"goalOK","uia_continue":"uiaOK","writer_busy":"writerBusy","usage_limit":"quotaWait"};
var ocTxt=oc?(L(ocMap[oc]||"")!==ocMap[oc]?L(ocMap[oc]):esc(oc)):"";
var cq0=s.quota||{};
var cc0=(cq0.credits_count!==undefined)?cq0.credits_count:cq0.credits;
var cInfo=(cc0?("×"+cc0):L("noneC"));
if(cc0&&cq0.credits_nearest_expiry){cInfo+=L("expirePfx")+fmtMDHM(cq0.credits_nearest_expiry)+")"}
var cardSwitch='<div class="ctl tip end" data-tip="'+esc(L("tipCard"))+'">'+
'<label class="sw"><input type="checkbox"'+(t.reset_card?" checked":"")+
' onchange="setT(\''+t.id+'\',{reset_card:this.checked})"><i></i></label>'+
'<label>'+L("card")+'</label><span style="font-size:11px;color:var(--dim)">'+cInfo+"</span></div>";
var promptRow="";
if(!t.is_goal){
promptRow='<div class="prow"><label style="font-size:12px;color:var(--dim)">'+L("prompt")+"</label>"+
'<input type="text" id="pp_'+t.id.slice(0,8)+'" value="'+esc(t.prompt)+
'" placeholder="'+esc(defp)+'" onchange="setT(\''+t.id+'\',{prompt:this.value})">'+
'<button class="btn-mini" onclick="resetPrompt(\''+t.id+'\')">'+L("resetD")+"</button></div>";
}
var rsel='<div class="ctl"><label>Reasoning</label><select onchange="setT(\''+t.id+
'\',{effort:this.value})">';
for(var j=0;j<eff.length;j++){rsel+='<option '+((eff[j]===(t.effort||""))?"selected":"")+
' value="'+eff[j]+'">'+(eff[j]||L("inherit"))+"</option>"}
rsel+="</select></div>";
var modelCtl='<div class="ctl"><label>'+L("model")+'</label><select onchange="setT(\''+t.id+
'\',{model:this.value})"><option '+((t.model||"")===""?"selected":"")+' value="">'+
L("inherit")+"</option>"+modelOpts(t.model)+"</select></div>";
rows+=(i===0?dl:"")+
'<div class="tcard '+(t.guard?"guard":"")+'">'+
'<div class="thead"><span class="dot"></span><span class="ttl">'+
esc((t.title||"")||String(t.id||"").slice(0,13))+"</span>"+badges+"</div>"+
'<div class="meta">'+L("lastUpd")+" "+fmtMS(t.updated_ms)+" · "+L("resumed")+" <em>"+fmt(t.resumes)+
"</em>"+L("times")+tries+
(t.next_wake?(" · "+L("nextChk")+" "+fmtISO(t.next_wake)):"")+
(ocTxt?(" · "+ocTxt):"")+"</div>"+
'<div class="controls">'+
'<div class="ctl tip start" data-tip="'+esc(L("tipGuard"))+'">'+
'<label class="sw"><input type="checkbox"'+(t.guard?" checked":"")+
' onchange="setT(\''+t.id+'\',{guard:this.checked})"><i></i></label>'+
'<label>'+L("guard")+"</label></div>"+
'<div class="ctl tip" data-tip="'+esc(L("tipWeekly"))+'">'+
'<label class="sw"><input type="checkbox"'+(t.weekly?" checked":"")+
' onchange="setT(\''+t.id+'\',{weekly:this.checked})"><i></i></label>'+
'<label>'+L("weekly")+"</label></div>"+
cardSwitch+"</div>"+
'<div class="controls2">'+modelCtl+rsel+"</div>"+promptRow+"</div>";
}
document.getElementById("threads").innerHTML=rows||
'<div class="card empty">'+L("noThread")+"</div>";
var nb=document.getElementById("newsbox");
if(nb){nb.scrollTop=nb.scrollHeight}
}).catch(function(e){
var why=(e&&e.message==="timeout")?L("errTimeout"):L("errFail");
document.getElementById("quota").innerHTML=
'<div style="color:var(--danger);font-size:13px">'+L("errConn")+why+L("errHint")+
"schtasks /Run /TN codex-autoresume</div>"})}
function setT(id,o){api("/api/threads/"+id,o).then(function(){toast(L("upd"));refresh()})}
function resetPrompt(id){api("/api/threads/"+id,{prompt:""}).then(function(){
toast(L("restored"));refresh()})}
function togglePause(){var s=window.__s;api("/api/pause",{paused:!s.paused}).then(
function(){toast(s.paused?L("resumeOK"):L("pauseOK"));refresh()})}
function applyLang(){
var els=document.querySelectorAll("[data-i18n]");
for(var i=0;i<els.length;i++){els[i].textContent=L(els[i].getAttribute("data-i18n"))}
var lb=document.getElementById("langBtn");if(lb){lb.textContent=(LNG==="en")?"EN":"CN"}
var ft=document.getElementById("foot");if(ft){ft.innerHTML=L("footer")}
var ld=document.getElementById("ld");if(ld){ld.textContent=L("loading")}
try{document.documentElement.lang=(LNG==="en")?"en":"zh-CN"}catch(e){}
try{document.title=(LNG==="en")?"Codex Guard Console":"Codex 守护控制台"}catch(e){}}
function toggleLang(){LNG=(LNG==="zh")?"en":"zh";
try{localStorage.setItem("cglang",LNG)}catch(e){}
applyLang();refresh()}
applyLang();refresh();setInterval(refresh,15000);
document.addEventListener("visibilitychange",function(){
if(!document.hidden){refresh()}});
window.addEventListener("focus",function(){refresh()});
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
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    body = PANEL_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/api/status":
                    self._json(api.status())
                elif self.path == "/api/uia/probe":
                    from .uia import find_continue_buttons, probe_detailed
                    self._json({"buttons": find_continue_buttons(),
                                "detailed": probe_detailed()})
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
                "prompt": tc.get("prompt") or "",
                "reset_card": bool(tc.get("reset_card")),
                "resumes": (t.success_resumes if t else 0),
                "attempts": (t.total_resumes if t else 0),
                "last_outcome": t.last_outcome if t else "",
                "next_wake": (t.next_wake_iso[:19] if t and t.next_wake_iso
                              else None),
                "updated_ms": info.updated_at_ms,
            })
        snap = d._snapshot
        quota = None
        if snap:
            import time as _time
            from .usage import eta_epoch, estimate_ratio, simulate_weekly_eta
            samples = getattr(d, "_usage_samples", [])
            eta_p, rate_p, _ = eta_epoch(samples, 1, 3600)
            # weekly ETA by stepping through 5h windows: within the current
            # partial window at most (100-P)% remains; each reset opens a
            # fresh window worth min(rate*5h, 100)% of primary; weekly rises
            # `ratio` per primary percent
            ratio = estimate_ratio(samples) or 1.0
            win_sec = (snap.primary.window_duration_mins or 300) * 60
            sim_rate = rate_p or (100.0 / (win_sec / 3600.0))
            eta_s = simulate_weekly_eta(
                _time.time(), snap.primary.used_percent,
                snap.secondary.used_percent, sim_rate, ratio,
                snap.primary.resets_at, win_sec,
                weekly_reset_epoch=snap.secondary.resets_at)
            rate_s = sim_rate * ratio
            quota = {
                "primary_used": snap.primary.used_percent,
                "primary_reset": snap.primary.resets_at,
                "secondary_used": snap.secondary.used_percent,
                "secondary_reset": snap.secondary.resets_at,
                "credits": len(snap.reset_credits),
                "allowed": snap.ordinary_usage_allowed,
                "fetched_at": getattr(d, "_snapshot_wall", None),
                "eta_primary": eta_p,
                "rate_primary": round(rate_p, 2) if rate_p else None,
                "eta_secondary": eta_s,
                "rate_secondary": round(rate_s, 2) if rate_s else None,
                "eta_secondary_capped": False,
                "credits_count": len(snap.reset_credits),
                "credits_nearest_expiry": (
                    min((c.expires_at for c in snap.reset_credits
                         if c.expires_at), default=None) or None),
            }
        ann = d.cached_announcement()
        return {
            "paused": bool(st and st.paused),
            "dry_run": bool(d.cfg["general"]["dry_run"]),
            "quota": quota,
            "models": d.cached_models(),
            "default_prompt": d.cfg["resume"]["prompt_generic"],
            "announcement": ann,
            "credit_events": list((st.credit_events if st else []))[-5:],
            "reset_events": list((st.reset_events if st else []))[-8:],
            "news_log": list((st.news_log if st else []))[-60:],
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
        if "prompt" in payload:
            t.guard_prompt = str(payload["prompt"] or "").strip() or None
        if "reset_card" in payload:
            t.guard_reset_card = bool(payload["reset_card"])
        save_state(st)
        self.daemon._state_mtime = os.path.getmtime(st.state_path)
        return {"thread": thread_id, "guard": t.manual,
                "weekly": t.guard_weekly, "model": t.guard_model,
                "effort": t.guard_effort, "prompt": t.guard_prompt,
                "reset_card": t.guard_reset_card}


def _fmt_ms(ms: int | None) -> str | None:
    if not ms:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(
        ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M") + " UTC"

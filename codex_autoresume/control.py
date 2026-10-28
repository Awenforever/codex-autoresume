"""Local control API + built-in panel for the daemon.

A tiny 127.0.0.1-only HTTP server the in-app control surface (and any UI /
skill / curl) uses to read state and toggle per-thread guarding:

    GET  /                  -> built-in control panel (dark, Codex-styled)
    GET  /api/status        -> {paused, dry_run, quota, threads: [...]}
    POST /api/threads/<id>  -> {guard?, weekly?, model?, effort?}
    POST /api/pause         -> {paused: bool}
    POST /api/update        -> {confirm: true}  (self-update + restart)
    POST /api/stats/reset   -> {confirm: true}  (zero the resume counters)

All writes land in the watcher state file; the running daemon picks them up
on its next tick (live sync), so toggles take effect within seconds.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .state import load_state, save_state
from . import __version__

PANEL_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Guard Console</title>
<style>
:root{--bg:#0a0c0d;--card:#131719;--card2:#181d20;--line:#242b30;--fg:#ecefef;
--dim:#87929a;--accent:#10a37f;--accent-soft:#12362c;--warn:#e0a341;--danger:#e5534b;
--track:#1e2529;--hand:#ecefef;--hand2:#c8d2d8;--chipbg:#12202b;--chipbd:#1e3a4d;
--warnbg:#3a3320;--link:#7ec8ff;--tickmaj:#39424a;--tickmin:#242b30;
--hovbd:#31403a;--tipbg:#20272c;--tipfg:#dfe6ea;--swbg:#2c343a;--swknob:#aab4bb;
--ph:#5c666d;--accent-rgb:16,163,127;--warn-rgb:224,163,65;--shadow:rgba(0,0,0,.45);
--reset:#c9a6ff;
color-scheme:dark}
body.light{--bg:#f5f7f7;--card:#ffffff;--card2:#eef2f2;--line:#dfe6e8;--fg:#1b272c;
--dim:#64737a;--accent:#0d8a6a;--accent-soft:#e0f2ec;--warn:#a9741f;--danger:#cf443c;
--track:#e4ebeb;--hand:#202b31;--hand2:#5d6b72;--chipbg:#e9f3f0;--chipbd:#cfe4dc;
--warnbg:#f6ecd4;--link:#17749f;--tickmaj:#9fadb2;--tickmin:#d3dcde;
--hovbd:#c9d8d2;--tipbg:#ffffff;--tipfg:#26343b;--swbg:#c6d2d6;--swknob:#ffffff;
--ph:#9aa7ad;--accent-rgb:13,138,106;--warn-rgb:169,116,31;--shadow:rgba(23,42,50,.16);
--reset:#6a3fd1;
color-scheme:light}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,"Segoe UI",
"Microsoft YaHei",sans-serif;padding:20px 18px 44px;max-width:940px;margin:0 auto;
-webkit-font-smoothing:antialiased}
body.theming,body.theming *{transition:background-color .35s ease,color .35s ease,
border-color .35s ease,box-shadow .35s ease,stroke .35s ease,fill .35s ease}
body.theming .ring .val{transition:stroke-dashoffset .6s,stroke .35s ease}
header{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.logo{width:38px;height:38px;border-radius:11px;background:linear-gradient(135deg,
#10a37f,#0a6e56);display:flex;align-items:center;justify-content:center;
box-shadow:0 4px 18px rgba(var(--accent-rgb),.35)}
.logo svg{width:22px;height:22px}
.logowrap{position:relative;flex:none;line-height:0}
button.logo{border:none;padding:0;cursor:default}
button.logo:active{transform:none}
.logowrap.upd button.logo{cursor:pointer}
.logowrap.upd button.logo:hover{filter:brightness(1.09)}
button.logo:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.upddot{display:none;position:absolute;top:-2px;right:-2px;width:11px;height:11px;
border-radius:50%;background:#ff4d4f;border:2px solid var(--bg);
animation:updpulse 2s infinite}
.logowrap.upd .upddot{display:block}
@keyframes updpulse{0%{box-shadow:0 0 0 0 rgba(255,77,79,.55)}
70%{box-shadow:0 0 0 7px rgba(255,77,79,0)}100%{box-shadow:0 0 0 0 rgba(255,77,79,0)}}
#updOverlay{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;
align-items:center;justify-content:center;z-index:10000;padding:20px}
#updOverlay.show{display:flex}
.modal{background:var(--card);border:1px solid var(--line);border-radius:16px;
padding:20px 22px;max-width:430px;width:100%;box-shadow:0 18px 60px var(--shadow)}
.mtitle{font-size:16px;font-weight:650;margin-bottom:10px}
.mver{display:flex;align-items:center;gap:10px;font-size:13px;
color:var(--dim);margin-bottom:10px}
.mver b{color:var(--accent);font-size:14px}
.mnote{font-size:12.5px;color:var(--dim);line-height:1.7;margin-bottom:16px}
.mbtns{display:flex;justify-content:flex-end;gap:9px}
.mbtns button{padding:7px 14px;border-radius:9px;font-size:13px;cursor:pointer}
.mbtns .primary{background:var(--accent);border-color:var(--accent);
color:#fff;font-weight:600}
.mbtns button:disabled{opacity:.55;cursor:default}
h1{font-size:17px;font-weight:650;letter-spacing:.2px}
.hrow{display:flex;align-items:baseline;gap:7px}
.ver{font-size:11px;font-weight:500;color:var(--dim);opacity:.72;letter-spacing:.2px}
.sub{font-size:12px;color:var(--dim);margin-top:1px}
.hright{margin-left:auto;display:flex;gap:8px;align-items:center}
.chip{font-size:12px;border-radius:999px;padding:4px 12px;font-weight:600;
border:1px solid var(--line);color:var(--dim);background:var(--card)}
.chip.dry{border-color:rgba(var(--warn-rgb),.5);color:var(--warn)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;
padding:16px 18px;margin-bottom:13px}
.hero{display:flex;gap:28px;align-items:center;flex-wrap:wrap}
.ringcol{display:flex;flex-direction:column;align-items:center;gap:12px}
.ringrow{display:flex;gap:18px}
.ringwrap{display:flex;flex-direction:column;align-items:center;gap:4px}
.ring{position:relative;width:92px;height:92px}
.ring svg{width:92px;height:92px;transform:rotate(-90deg);display:block}
.ring .track{fill:none;stroke:var(--track);stroke-width:8}
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
.clockwrap{display:flex;flex-direction:column;align-items:center;gap:4px;width:92px}
.cface{width:92px;height:92px}
.cfacebg{fill:var(--card2);stroke:var(--line);stroke-width:1.5}
.chand{stroke:var(--hand);stroke-width:3.4;stroke-linecap:round}
.chandm{stroke:var(--hand2);stroke-width:2.4;stroke-linecap:round}
.tick{stroke:var(--tickmin);stroke-width:1.3;stroke-linecap:round}
.tickmaj{stroke:var(--tickmaj);stroke-width:2.4;stroke-linecap:round}
.chands{stroke:var(--accent);stroke-width:1.4;stroke-linecap:round;
filter:drop-shadow(0 0 3px rgba(var(--accent-rgb),.45))}
.cdot{fill:var(--accent)}
.clocklbl{font-size:10.5px;color:var(--dim);text-align:center;
line-height:1.4;white-space:nowrap}
.clocklbl b{color:var(--fg);font-weight:600;font-size:11px}
.newsbox .d{color:var(--accent);font-weight:700;margin-right:8px;flex:none;
background:var(--chipbg);border:1px solid var(--chipbd);border-radius:6px;
padding:0 6px;margin-right:10px}
.hl-ok{color:var(--accent);font-weight:600}
.hl-warn{color:var(--warn);font-weight:600}
.hl-t{color:var(--link)}
.hl-reset{font-weight:700}
.hl-reset .rl:nth-child(5n+1){color:var(--danger)}
.hl-reset .rl:nth-child(5n+2){color:var(--warn)}
.hl-reset .rl:nth-child(5n+3){color:var(--accent)}
.hl-reset .rl:nth-child(5n+4){color:var(--link)}
.hl-reset .rl:nth-child(5n){color:var(--reset)}
.chip{white-space:nowrap}
.kv .ok{color:var(--accent)} .kv .bad{color:var(--danger)} .kv .eta{color:var(--warn)}
.kv .ok,.kv .eta{transition:color .4s ease}
button{background:var(--card2);border:1px solid var(--line);color:var(--fg);
border-radius:10px;padding:7px 16px;font-size:13px;cursor:pointer;
transition:border-color .15s,transform .1s}
button:hover{border-color:var(--accent)} button:active{transform:scale(.97)}
.btn-main{background:var(--accent-soft);border-color:rgba(var(--accent-rgb),.4);color:var(--accent)}
.btn-mini{padding:5px 10px;font-size:12px;border-radius:8px}
.btn-mini.theme,.btn-mini.icon{width:32px;min-width:32px;padding:0;height:26px;
display:inline-flex;align-items:center;justify-content:center;color:var(--dim)}
.btn-mini.theme:hover,.btn-mini.icon:hover{color:var(--fg);border-color:var(--hovbd)}
.btn-mini.theme svg{display:none}
.btn-mini.theme[data-mode="auto"] .ic-auto,
.btn-mini.theme[data-mode="light"] .ic-sun,
.btn-mini.theme[data-mode="dark"] .ic-moon{display:block}
.sect{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--dim);
text-transform:uppercase;letter-spacing:1px;margin:18px 2px 10px}
.sect:after{content:"";flex:1;height:1px;background:var(--line)}
.tcard{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:13px 16px;margin-bottom:10px;transition:border-color .15s}
.tcard.guard{border-color:rgba(var(--accent-rgb),.45)}
.tcard:hover{border-color:var(--hovbd)}
.thead{display:flex;align-items:center;gap:9px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--tickmaj);flex:none}
.tcard.guard .dot{background:var(--accent);box-shadow:0 0 10px var(--accent);
animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
.ttl{font-weight:600;font-size:13.5px;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis;flex:1;min-width:0}
.badge{font-size:10px;border-radius:6px;padding:1.5px 7px;font-weight:600;flex:none}
.badge.goal{background:var(--accent-soft);color:var(--accent)}
.badge.gstat{background:var(--warnbg);color:var(--warn)}
.badge.run{background:var(--accent-soft);color:var(--accent)}
.meta{font-size:11.5px;color:var(--dim);margin:5px 0 11px 17px}
.meta em{font-style:normal;color:var(--dim)}
.controls{display:flex;gap:11px;align-items:center;flex-wrap:wrap;
padding-left:17px;border-top:1px solid var(--line);padding-top:11px}
.controls2{display:flex;gap:11px;align-items:center;flex-wrap:wrap;
padding-left:17px;margin-top:9px}
.ctl{display:flex;align-items:center;gap:7px;position:relative}
.ctl label{font-size:12px;color:var(--dim)}
.tip{position:relative;cursor:help}
.tip:after{content:attr(data-tip);position:absolute;bottom:calc(100% + 8px);
left:50%;transform:translateX(-50%);background:var(--tipbg);color:var(--tipfg);
font-size:11.5px;line-height:1.5;padding:7px 11px;border-radius:9px;
white-space:normal;width:220px;border:1px solid var(--line);
box-shadow:0 8px 22px var(--shadow);opacity:0;pointer-events:none;
transition:opacity .15s;z-index:9999;text-align:left}
.tip:hover:after{opacity:1}
.tipn{cursor:default}
.tipn:after{content:attr(data-tip);position:absolute;bottom:calc(100% + 8px);
left:50%;transform:translateX(-50%);background:var(--tipbg);color:var(--tipfg);
font-size:11.5px;line-height:1.5;padding:7px 11px;border-radius:9px;
white-space:normal;width:220px;border:1px solid var(--line);
box-shadow:0 8px 22px var(--shadow);opacity:0;pointer-events:none;
transition:opacity .15s;z-index:9999;text-align:left}
.tipn:hover:after{opacity:1}
.tip.end:after{left:auto;right:-8px;transform:none}
.tip.start:after{left:-8px;transform:none}
.sw{position:relative;width:40px;height:22px;display:inline-block;flex:none}
.sw input{opacity:0;width:0;height:0}
.sw i{position:absolute;inset:0;background:var(--swbg);border-radius:999px;
transition:.18s;cursor:pointer}
.sw i:before{content:"";position:absolute;width:16px;height:16px;border-radius:50%;
background:var(--swknob);top:3px;left:3px;transition:.18s}
.sw input:checked+i{background:var(--accent);box-shadow:0 0 10px rgba(var(--accent-rgb),.4)}
.sw input:checked+i:before{transform:translateX(18px);background:#fff}
input[type=text]{background:var(--card2);border:1px solid var(--line);color:var(--fg);
border-radius:9px;padding:5px 8px;font-size:12px;width:126px;transition:border-color .15s}
input[type=text]:focus{outline:none;border-color:var(--accent)}
input::placeholder{color:var(--ph)}
select{background:var(--card2);border:1px solid var(--line);color:var(--fg);
border-radius:9px;padding:6px 8px;font-size:12.5px;width:126px}
select:focus{outline:none;border-color:var(--accent)}
select:disabled{opacity:.45;cursor:not-allowed}
.prow{display:flex;gap:8px;align-items:center;padding:10px 0 0 17px;
border-top:1px dashed var(--line);margin-top:10px;flex-wrap:wrap}
.prow input{flex:1;min-width:220px}
.empty{color:var(--dim);font-size:13px;text-align:center;padding:14px 0}
.foot{color:var(--dim);font-size:11px;text-align:center;margin-top:14px;line-height:1.7}
.foot code{color:var(--dim);background:none;padding:0}
#toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);
background:var(--accent-soft);color:var(--accent);border:1px solid rgba(var(--accent-rgb),.5);
padding:8px 18px;border-radius:11px;font-size:13px;opacity:0;transition:.25s;
pointer-events:none;box-shadow:0 6px 24px var(--shadow)}
#toast.show{opacity:1}
@media (max-width:680px){body{padding:12px 10px 30px}.hero{gap:14px}
.controls,.controls2{gap:10px}input[type=text]{width:118px}}
</style></head><body>
<header>
 <div class="logowrap" id="logoWrap"><button class="logo" id="logoBtn" onclick="openUpdate()" aria-label="Codex Guard"><svg viewBox="0 0 24 24" fill="none">
 <path d="M12 3.4 19.4 7.7v8.6L12 20.6 4.6 16.3V7.7Z" stroke="#fff" stroke-width="2"
 stroke-linejoin="round"/><path d="M9.8 8.4 15.8 12 9.8 15.6Z" fill="#fff"/></svg></button><span class="upddot" id="updDot"></span></div>
 <div><div class="hrow"><h1 data-i18n="title">Codex Guard</h1><span class="ver">v__CXR_VERSION__</span></div><div class="sub" data-i18n="sub">Auto-continue conversations after quota resets</div></div>
 <div class="hright" id="chips"></div>
 <button class="btn-mini theme" id="themeBtn" data-mode="auto" onclick="cycleTheme()"><svg class="ic-auto" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="8.2"/><path d="M12 3.8a8.2 8.2 0 0 1 0 16.4Z" fill="currentColor" stroke="none"/></svg><svg class="ic-sun" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4.1"/><path d="M12 2.9v2.2M12 18.9v2.2M2.9 12h2.2M18.9 12h2.2M5.6 5.6l1.6 1.6M16.8 16.8l1.6 1.6M18.4 5.6l-1.6 1.6M7.2 16.8l-1.6 1.6"/></svg><svg class="ic-moon" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M20.4 14.2A8.6 8.6 0 0 1 9.8 3.6 8.6 8.6 0 1 0 20.4 14.2Z"/></svg></button>
 <button class="btn-mini icon" id="langBtn" onclick="toggleLang()" aria-label="Language"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="8.4"/><path d="M3.6 12h16.8M12 3.6c2.5 2.3 3.8 5.1 3.8 8.4s-1.3 6.1-3.8 8.4c-2.5-2.3-3.8-5.1-3.8-8.4s1.3-6.1 3.8-8.4Z"/></svg></button>
</header>
<div class="card"><div class="hero">
 <div class="ringcol">
  <div class="ringrow" id="clkrow">
   <div class="clockwrap">
    <svg class="cface" id="clkL" viewBox="0 0 100 100">
     <circle cx="50" cy="50" r="47" class="cfacebg"/><g id="clkLt"></g>
     <line id="clkLh" x1="50" y1="50" x2="50" y2="28" class="chand"/>
     <line id="clkLm" x1="50" y1="50" x2="50" y2="17" class="chandm"/>
     <line id="clkLs" x1="50" y1="57" x2="50" y2="13" class="chands"/>
     <circle cx="50" cy="50" r="2.4" class="cdot"/>
    </svg>
    <div class="clocklbl" id="clkLlbl"></div>
   </div>
   <div class="clockwrap">
    <svg class="cface" id="clkP" viewBox="0 0 100 100">
     <circle cx="50" cy="50" r="47" class="cfacebg"/><g id="clkPt"></g>
     <line id="clkPh" x1="50" y1="50" x2="50" y2="28" class="chand"/>
     <line id="clkPm" x1="50" y1="50" x2="50" y2="17" class="chandm"/>
     <line id="clkPs" x1="50" y1="57" x2="50" y2="13" class="chands"/>
     <circle cx="50" cy="50" r="2.4" class="cdot"/>
    </svg>
    <div class="clocklbl" id="clkPlbl"></div>
   </div>
  </div>
  <div class="ringrow" id="ringrow">
   <div style="color:var(--dim);font-size:13px" data-i18n="loading">Loading…</div>
  </div>
  <div id="pausewrap"></div>
 </div>
 <div class="hinfo" id="hinfo">
  <div style="color:var(--dim);font-size:13px" data-i18n="loading">Loading…</div>
 </div>
</div></div>
<div class="sect" data-i18n="recent">Recent conversations</div>
<div id="threads"></div>
<div class="foot" id="foot"></div>
<div id="updOverlay" onclick="if(event.target===this)closeUpdate()">
 <div class="modal">
  <div class="mtitle" data-i18n="updTitle">Update available</div>
  <div class="mver"><span>v<span id="updFrom"></span></span><span>→</span><b>v<span id="updTo"></span></b></div>
  <div class="mnote" id="updNote"></div>
  <div class="mbtns"><button class="mclose" id="updCancel" onclick="closeUpdate()" data-i18n="updCancel">Later</button><button class="primary" id="updGo" onclick="runUpdate()" data-i18n="updGo">Update &amp; restart</button></div>
 </div>
</div>
<div id="toast"></div>
<script>
var eff=["","none","minimal","low","medium","high","xhigh"];
var LNG="en";try{LNG=localStorage.getItem("cglang")||"en"}catch(e){}if(LNG!=="zh"&&LNG!=="en")LNG="en";
var I18N={
zh:{title:"Codex 守护",sub:"额度重置后自动继续任务 · 仅本机",langTip:"语言：中文（点击切换）",loading:"加载中…",theme:"主题",themeAuto:"自动跟随系统",themeLight:"浅色",themeDark:"深色",
w5:"5 小时窗口",wk:"周窗口",reset:"重置",status:"使用状态",limited:"额度受限",
avail:"✓ 可正常使用",unknown:"状态未知",eta5:"5h 额度预估耗尽",etaw:"周额度预估耗尽",weeklyfirst:"周额度先重置",
credits:"重置赠送",forecast:"预告",landed:"到账",
news:"动态",pauseAll:"⏸ 全部暂停",resumeAll:"▶ 全部继续",
recent:"近期会话",
noThread:"暂无近期会话——在 Codex 里打开一个对话，面板会自动刷新",
guard:"守护",weekly:"跨周",card:"重置卡",noneC:"（无）",expirePfx:"（到期 ",
model:"模型",inherit:"继承",prompt:"继续指令",resetD:"恢复默认",
lastUpd:"最近",resumed:"成功恢复",times:" 次",tryPfx:"（尝试 ",trySfx:"）",

tipGuard:"打开则表示 5h 额度耗尽后自动继续任务",
tipWeekly:"打开则表示守护开启时周额度重置后也自动继续任务",
tipCard:"周额度耗尽且账户有官方重置卡时，自动消耗一张以继续任务",
quotaNA:"额度快照暂不可用（网络波动），守护仍按重置时间正常工作",
postAt:"动态发布时间",exhausted:"已耗尽",newsWin:"近 48 小时",clkLocal:"本机",clkPT:"PT",stRunning:"运行中",stWaiting:"等待额度",
tipModel:"每次自动恢复时使用的模型/推理强度；选「继承」则沿用对话自身设置",
tipModelGoal:"/goal 会话由桌面应用驱动，固定沿用对话自身设置，因此锁定为继承",g_usage_limited:"额度受限",g_budget_limited:"预算受限",g_active:"运行中",
g_paused:"已暂停",g_blocked:"受阻",g_complete:"已完成",
goalOK:"✓ 已自动继续",uiaOK:"✓ 已点继续",writerBusy:"会话被 Codex 应用占用，切走/关闭后自动接管",quotaWait:"等待额度",capacityWait:"等待模型容量",
errConn:"无法连接守护进程：",errTimeout:"请求超时（8 秒无响应）",errFail:"连接失败",
errHint:"。面板每 15 秒自动重试；若持续失败，请确认守护进程在运行：",
upd:"已更新",restored:"已恢复默认指令",pauseOK:"守护已暂停",resumeOK:"守护已恢复",
updTitle:"发现新版本",updTip:"可升级到 v",updNote:"守护进程会自动更新代码并重启；正在进行的对话和额度不受影响。面板会自动重新连接。",updCancel:"稍后",updGo:"更新并重启",updStarting:"正在准备升级…",updRestarting:"正在升级并重启守护进程，面板会自动重新连接…",updFailed:"升级未能启动：",updLastFail:"上次升级未完成：",updSlow:"升级耗时较长——请查看 update.log，或手动重启守护进程。",
footer:"打开方式：侧栏输入 <code>http://127.0.0.1:8765</code>（推荐，不消耗额度）· 桌面快捷方式 · 完整说明见插件 README"},
en:{title:"Codex Guard",sub:"Auto-continue conversations after quota resets",
langTip:"Language: English (click to switch)",loading:"Loading…",theme:"Theme",themeAuto:"Follow system",themeLight:"Light",themeDark:"Dark",w5:"5h window",wk:"Weekly",reset:"Resets",
status:"Usage",limited:"Limited",avail:"✓ Available",unknown:"Unknown",
eta5:"5h quota ETA",etaw:"Weekly quota ETA",weeklyfirst:"Weekly resets first",
credits:"Reset credits",forecast:"Forecast",landed:"Landed",
news:"News",pauseAll:"⏸ Pause all",resumeAll:"▶ Resume all",
recent:"Recent conversations",
noThread:"No recent conversations — open one in Codex; the panel refreshes automatically",
guard:"Auto-resume",weekly:"Weekly",card:"Credits",noneC:"(none)",
expirePfx:" (expires ",model:"Model",inherit:"Inherit",prompt:"Resume prompt",
resetD:"Reset",lastUpd:"Last",resumed:"resumed",times:"×",tryPfx:" (",
trySfx:" tries)",
tipGuard:"On: automatically continue this thread when the 5h quota resets",
tipWeekly:"On: also continue after the weekly quota resets (Auto-resume on)",
tipCard:"When the weekly quota runs out, spend a reset credit to keep going",
quotaNA:"Quota snapshot unavailable — resets are still tracked",
postAt:"post publication time",exhausted:"Exhausted",newsWin:"last 48h",clkLocal:"Local",clkPT:"PT",stRunning:"Running",stWaiting:"Waiting for quota",
tipModel:"Model/reasoning used for every automatic resume; Inherit keeps the conversation's own settings",
tipModelGoal:"/goal threads are driven by the desktop app and always use their own settings, so they stay locked",g_usage_limited:"quota-limited",g_budget_limited:"budget-limited",g_active:"active",
g_paused:"paused",g_blocked:"blocked",g_complete:"complete",
goalOK:"✓ auto-continued",uiaOK:"✓ Continue clicked",writerBusy:"held by the Codex app; guard resumes once released",quotaWait:"waiting for quota",capacityWait:"waiting for model capacity",
errConn:"Cannot reach guard daemon: ",errTimeout:"request timed out (8s)",
errFail:"connection failed",
errHint:". The panel retries every 15s; if this persists, start the daemon: ",
upd:"Saved",restored:"Prompt reset to default",pauseOK:"Paused all",
resumeOK:"Resumed all",
updTitle:"Update available",updTip:"Update available: v",updNote:"The guard will update its code and restart. Conversations and quota are unaffected; this panel reconnects automatically.",updCancel:"Later",updGo:"Update & restart",updStarting:"Preparing update…",updRestarting:"Updating and restarting the guard; this panel reconnects automatically…",updFailed:"Update could not start: ",updLastFail:"Last update did not finish: ",updSlow:"This is taking longer than usual — check update.log or restart the daemon manually.",
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
function ringColor(p){
 /* ring palette is theme-independent on purpose: the quota pressure
    colors must read the same in dark and light mode */
 var G=[16,163,127],A=[224,163,65],R=[229,83,75],c;
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
function etaTime(v){return (typeof v==="number")?fmtMDHM(v):fmtISO(v)}
function etaRow(label,q,etaK,rateK,exhK,capK){
 var eta=q[etaK],rate=q[rateK],cap=capK&&q[capK];
 var b=(cap?('<b class="eta">'+L("weeklyfirst")+"</b>")
 :(eta?('<b class="eta">'+fmtMDHM(eta)+"</b>")
 :(exhK&&q.secondary_used>=100?
   ('<b class="eta">'+L("exhausted")+(q[exhK]?(" · "+etaTime(q[exhK])):"")+"</b>")
 :"<b>--</b>")));
 var tail=(!cap&&rate?(' <span style="font-size:11px">('+rate+"%/h)</span>"):"");

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
.replace(/</g,"&lt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")}
function jsq(s){return esc(String(s===null||s===undefined?"":s)
.replace(/\\/g,"\\\\").replace(/'/g,"\\'"))}
function hlNews(t){
 // single pass over the escaped text: no nested spans, broad lexicon
 var RE=new RegExp(
 "(Landed as announced(?:(?!\\bresets?\\b)[^,.]){0,60})"+
 "|\\b(resets?|resetting)\\b"+
 "|\\b(Landing|upcoming|tomorrow|tonight|today|yesterday|midnight|noon|"+
 "dawn|dusk|morning|afternoon|evening|night)\\b"+
 "|(\\b\\d{1,2}:\\d{2}\\s*(?:am|pm)?\\b(?:\\s*(?:UTC|GMT|PST|PDT|PT|EST|EDT|CST|CDT|MST|MDT|Pacific\\s+Time))?"+
 "|\\b\\d{1,2}\\s*(?:am|pm)\\b"+
 "|\\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\\.?\\s*\\d{1,2}\\b"+
 "|\\b\\d{1,2}\\/d{1,2}\\b"+
 "|\\b(?:UTC|GMT|PST|PDT|PT|EST|EDT|CST|CDT|MST|MDT)\\b"+
 "|\\b\\d+\\s*(?:hours?|hrs?|minutes?|mins?|seconds?|secs?|days?|weeks?|months?)\\b)"+
 "|\\b(before|after|around|about|by|until|till|next|soon|early|late)\\b","gi");
 return String(t).replace(RE,function(m,ok,rs,warn,tm){
 var cls=ok?"hl-ok":(rs?"hl-reset":(tm?"hl-t":"hl-warn"));
 if(cls==="hl-reset"){
  var w='<span class="hl-reset">';
  for(var i=0;i<m.length;i++){w+='<span class="rl">'+m.charAt(i)+"</span>"}
  return w+"</span>"}
 return '<span class="'+cls+'">'+m+"</span>"})}
function stripTco(t){
// X appends a t.co card to the end of media/external posts; that trailing
// link is noise. A link elsewhere is content (often the subject, e.g.
// "<card> has landed") and stays. Render-time too: news_log persisted by
// older builds can still carry trailing links.
return String(t||"").replace(/(?:\s*https?:\/\/t\.co\/\S+)+\s*$/i,"")
.replace(/\s+/g," ").trim()}
function newsBox(s){
 var ann=s.announcement||null;
 var es=(s.news_log&&s.news_log.length)?s.news_log:
 (ann&&ann.status?[{at:"",text:ann.status,kind:ann.status_kind}]:[]);
 if(!es.length){return ""}
 var rows="";
 for(var i=0;i<es.length;i++){
 var e=es[i];
 rows+='<div class="nl">'+(e.at?'<span class="d" title="'+L("postAt")+'">'+
 esc(fmtPostAt(e.at))+"</span>":"")+
 hlNews(esc(stripTco(e.text)))+"</div>"}
 return '<div class="kv"><span>'+L("news")+'</span><span style="font-size:11px">'+
 L("newsWin")+"</span></div>"+'<div class="newsbox" id="newsbox">'+rows+"</div>"}
function fmtPostAt(iso){
 // the poster's own wall clock: tracker authors post in Pacific time, so
 // render the post instant in America/Los_Angeles (DST-aware via Intl)
 try{
 var f=new Intl.DateTimeFormat("en-US",{timeZone:"America/Los_Angeles",
 month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});
 var fpa=f.formatToParts(new Date(iso)),pa={};
 for(var i=0;i<fpa.length;i++){pa[fpa[i].type]=fpa[i].value}
 var r=pa.month+"-"+pa.day+" "+pa.hour+":"+pa.minute+" PT";
 if(r.indexOf("undefined")<0){return r}
 }catch(e){}
 return fmtISO(iso)}
function hexRGB(v){
 var m=/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(v||"").trim());
 if(!m){return null}
 var h=m[1];
 if(h.length===3){h=h[0]+h[0]+h[1]+h[1]+h[2]+h[2]}
 return [parseInt(h.slice(0,2),16),parseInt(h.slice(2,4),16),
 parseInt(h.slice(4,6),16)]}
function cssRGB(name){
 try{return hexRGB(getComputedStyle(document.documentElement)
 .getPropertyValue(name))}catch(e){return null}}
function mixRGB(a,b,f){
 return "rgb("+Math.round(a[0]+(b[0]-a[0])*f)+","+
 Math.round(a[1]+(b[1]-a[1])*f)+","+
 Math.round(a[2]+(b[2]-a[2])*f)+")"}
function freshStyle(epochSec,mode){
 // freshness is conveyed by COLOR alone at full opacity: the row's hue at
 // "now" slides to muted gray at a 24h horizon. Forecast decays in both
 // directions (an overdue forecast is stale too); landed decays with age.
 if(epochSec===null||epochSec===undefined||!isFinite(epochSec)){return ""}
 var now=Date.now()/1000,horizon=24*3600;
 var dist=(mode==="forecast")?Math.abs(epochSec-now):Math.max(0,now-epochSec);
 var f=1-Math.min(1,Math.max(0,dist/horizon));
 var base=cssRGB(mode==="forecast"?"--warn":"--accent"),gray=cssRGB("--dim");
 if(!base||!gray){return ""}
 return ' style="color:'+mixRGB(base,gray,1-f)+'"'}
function annRowsFor(s){
 var ann=s.announcement||null,rows="";
 var fct="--",fst="";
 if(ann&&ann.forecast_epoch){fct=fmtMDHM(ann.forecast_epoch);
  fst=freshStyle(ann.forecast_epoch,"forecast")}
 rows+=kvRow(L("forecast"),'<b class="eta"'+fst+">"+fct+"</b>");
 var cand=[];
 var evs=s.reset_events||[];
 for(var i=0;i<evs.length;i++){if(evs[i].kind==="weekly"||evs[i].kind==="credit")cand.push(evs[i])}
 var ces=s.credit_events||[];
 for(var j=0;j<ces.length;j++){cand.push({kind:"credit",at:ces[j].landed_at,title:ces[j].title})}
 var landTxt="--",lst="";
 if(cand.length){
 cand.sort(function(a,b){return String(b.at).localeCompare(String(a.at))});
 var e0=cand[0];
 landTxt=fmtISO(e0.at)+(e0.kind==="credit"?(" · "+L("card")):"");
 var lsec=Date.parse(e0.at)/1000;
 lst=freshStyle(lsec,"landed")}
 rows+=kvRow(L("landed"),'<b class="ok"'+lst+">"+esc(landTxt)+"</b>");
 rows+=newsBox(s)
 return rows}
function syncUpdateUi(s){
 var u=(s&&s.update)||null;
 var wrap=document.getElementById("logoWrap");
 var b=document.getElementById("logoBtn");
 var show=!!(u&&u.available&&u.can_update&&!u.in_progress);
 if(wrap){wrap.classList.toggle("upd",show)}
 if(b){b.title=show?(L("updTip")+u.latest):""}
 if(!window.__updating)return;
 var last=u&&u.last;
 if(u&&!u.in_progress&&(!u.available||
   (last&&last.at&&last.at!==window.__updBase))){location.reload();return}
 var note=document.getElementById("updNote");
 if(note){note.textContent=L("updRestarting")}
}
function openUpdate(){
 var u=(window.__s&&window.__s.update)||null;
 if(!u||!u.available||!u.can_update||u.in_progress){return}
 document.getElementById("updFrom").textContent=u.current||"";
 document.getElementById("updTo").textContent=u.latest||"";
 var note=L("updNote");
 if(u.last&&u.last.status&&u.last.status!=="ok"&&u.last.error){
  note+=" "+L("updLastFail")+u.last.error}
 document.getElementById("updNote").textContent=note;
 var go=document.getElementById("updGo");
 go.disabled=false;go.textContent=L("updGo");
 var c=document.getElementById("updCancel");c.disabled=false;
 document.getElementById("updOverlay").classList.add("show");
}
function closeUpdate(){
 if(window.__updating)return;
 document.getElementById("updOverlay").classList.remove("show");
}
function runUpdate(){
 if(window.__updating)return;
 var u=(window.__s&&window.__s.update)||null;
 window.__updBase=(u&&u.last&&u.last.at)||"";
 var go=document.getElementById("updGo");
 go.disabled=true;go.textContent=L("updStarting");
 api("/api/update",{confirm:true}).then(function(r){
  if(r&&r.started){
   window.__updating=true;
   document.getElementById("updNote").textContent=L("updRestarting");
   document.getElementById("updCancel").disabled=true;
   setTimeout(function(){
    if(window.__updating){
     var n=document.getElementById("updNote");if(n){n.textContent=L("updSlow")}}
   },180000);
  }else{
   go.disabled=false;go.textContent=L("updGo");
   document.getElementById("updNote").textContent=
    L("updFailed")+((r&&r.error)||L("unknown"));
  }
 }).catch(function(e){
  go.disabled=false;go.textContent=L("updGo");
  document.getElementById("updNote").textContent=
   L("updFailed")+((e&&e.message)||L("errFail"));
 });
}
function refresh(){
return api("/api/status").then(function(s){
window.__s=s;
syncUpdateUi(s);
var q=s.quota;
var annRows=annRowsFor(s);
var ringrow=document.getElementById("ringrow");
var pausewrap=document.getElementById("pausewrap");
var hinfo=document.getElementById("hinfo");
if(!q){
ringrow.innerHTML='<div style="flex:1;color:var(--dim);font-size:13px">'+
L("quotaNA")+"</div>";
pausewrap.innerHTML="";
hinfo.innerHTML="";
}else{
ringrow.innerHTML=
ring(q.primary_used,L("w5"),fmtHM(q.primary_reset))+
ring(q.secondary_used,L("wk"),fmtMDHM(q.secondary_reset));
pausewrap.innerHTML='<button class="btn-main" style="width:100%" onclick="togglePause()">'+
(s.paused?L("resumeAll"):L("pauseAll"))+"</button>";
hinfo.innerHTML=
kvRow(L("status"),'<b class="'+(q.allowed===false?"bad":"ok")+'">'+
(q.allowed===false?L("limited"):q.allowed===true?L("avail"):L("unknown"))+"</b>")+
etaRow(L("eta5"),q,"eta_primary","rate_primary")+
etaRow(L("etaw"),q,"eta_secondary","rate_secondary","secondary_exhausted_at","eta_secondary_capped")+
kvRow(L("credits"),'<b>'+fmt((q.credits_count!==undefined)?q.credits_count:q.credits)+"</b>")+
annRows;
}
document.getElementById("chips").innerHTML=
s.dry_run?'<span class="chip dry">DRY-RUN</span>':"";
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
(t.goal_status?'<span class="badge gstat">'+esc(gstat(t.goal_status))+"</span>":"")+
(!t.is_goal&&t.run?'<span class="badge '+(t.run==="running"?"run":"gstat")+'">'+
esc(t.run==="running"?L("stRunning"):L("stWaiting"))+"</span>":"");
var tries=(t.attempts&&t.attempts>t.resumes)?(L("tryPfx")+t.attempts+L("trySfx")):"";
var oc=t.last_outcome;
var ocMap={"goal_continue":"goalOK","uia_continue":"uiaOK","writer_busy":"writerBusy","usage_limit":"quotaWait","capacity_wait":"capacityWait","server_overloaded":"capacityWait","overloaded":"capacityWait"};
var ocTxt=oc?(L(ocMap[oc]||"")!==ocMap[oc]?L(ocMap[oc]):esc(oc)):"";
var cq0=s.quota||{};
var cc0=(cq0.credits_count!==undefined)?cq0.credits_count:cq0.credits;
var cInfo=(cc0?("×"+cc0):L("noneC"));
if(cc0&&cq0.credits_nearest_expiry){cInfo+=L("expirePfx")+fmtMDHM(cq0.credits_nearest_expiry)+")"}
var cardSwitch='<div class="ctl tipn end" data-tip="'+esc(L("tipCard"))+'">'+
'<label class="sw"><input type="checkbox"'+(t.reset_card?" checked":"")+
' onchange="setT(\''+jsq(t.id)+'\',{reset_card:this.checked})"><i></i></label>'+
'<label>'+L("card")+'</label><span style="font-size:11px;color:var(--dim)">'+cInfo+"</span></div>";
var promptRow="";
if(!t.is_goal){
promptRow='<div class="prow"><label style="font-size:12px;color:var(--dim)">'+L("prompt")+"</label>"+
'<input type="text" id="pp_'+esc(t.id.slice(0,8))+'" value="'+esc(t.prompt)+
'" placeholder="'+esc(defp)+'" onchange="setT(\''+jsq(t.id)+'\',{prompt:this.value})">'+
'<button class="btn-mini" onclick="resetPrompt(\''+jsq(t.id)+'\')">'+L("resetD")+"</button></div>";
}
var rTip=t.is_goal?L("tipModelGoal"):L("tipModel");
var rsel='<div class="ctl tipn end" data-tip="'+esc(rTip)+
'"><label>Reasoning</label><select'+(t.is_goal?" disabled":"")+
' onchange="setT(\''+jsq(t.id)+'\',{effort:this.value})">';
for(var j=0;j<eff.length;j++){rsel+='<option '+((eff[j]===(t.effort||""))?"selected":"")+
' value="'+eff[j]+'">'+(eff[j]||L("inherit"))+"</option>"}
rsel+="</select></div>";
var mTip=t.is_goal?L("tipModelGoal"):L("tipModel");
var modelCtl='<div class="ctl tipn end" data-tip="'+esc(mTip)+
'"><label>'+L("model")+'</label><select'+(t.is_goal?" disabled":"")+
' onchange="setT(\''+jsq(t.id)+'\',{model:this.value})"><option '+((t.model||"")===""?"selected":"")+' value="">'+
L("inherit")+"</option>"+modelOpts(t.model)+"</select></div>";
rows+=(i===0?dl:"")+
'<div class="tcard '+(t.guard?"guard":"")+'">'+
'<div class="thead"><span class="dot"></span><span class="ttl">'+
esc((t.title||"")||String(t.id||"").slice(0,13))+"</span>"+badges+"</div>"+
'<div class="meta">'+L("lastUpd")+" "+fmtMS(t.updated_ms)+" · "+L("resumed")+" <em>"+fmt(t.resumes)+
"</em>"+L("times")+tries+

(ocTxt?(" · "+ocTxt):"")+"</div>"+
'<div class="controls">'+
'<div class="ctl tip start" data-tip="'+esc(L("tipGuard"))+'">'+
'<label class="sw"><input type="checkbox"'+(t.guard?" checked":"")+
' onchange="setT(\''+jsq(t.id)+'\',{guard:this.checked})"><i></i></label>'+
'<label>'+L("guard")+"</label></div>"+
'<div class="ctl tip" data-tip="'+esc(L("tipWeekly"))+'">'+
'<label class="sw"><input type="checkbox"'+(t.weekly?" checked":"")+
' onchange="setT(\''+jsq(t.id)+'\',{weekly:this.checked})"><i></i></label>'+
'<label>'+L("weekly")+"</label></div>"+
cardSwitch+"</div>"+
'<div class="controls2">'+modelCtl+rsel+"</div>"+promptRow+"</div>";
}
document.getElementById("threads").innerHTML=rows||
'<div class="card empty">'+L("noThread")+"</div>";
var nb=document.getElementById("newsbox");
if(nb){nb.scrollTop=nb.scrollHeight}
}).catch(function(e){
if(window.__updating){
 document.getElementById("hinfo").innerHTML=
 '<div style="color:var(--dim);font-size:13px">'+L("updRestarting")+"</div>";
 return}
var why=(e&&e.message==="timeout")?L("errTimeout"):L("errFail");
document.getElementById("hinfo").innerHTML=
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
var lb=document.getElementById("langBtn");
if(lb){var lt=L("langTip");lb.title=lt;lb.setAttribute("aria-label",lt)}
var tb=document.getElementById("themeBtn");if(tb){syncThemeUi()}
var ft=document.getElementById("foot");if(ft){ft.innerHTML=L("footer")}
var ld=document.getElementById("ld");if(ld){ld.textContent=L("loading")}
try{document.documentElement.lang=(LNG==="en")?"en":"zh-CN"}catch(e){}
try{document.title=(LNG==="en")?"Codex Guard Console":"Codex 守护控制台"}catch(e){}}
function toggleLang(){LNG=(LNG==="zh")?"en":"zh";
try{localStorage.setItem("cglang",LNG)}catch(e){}
applyLang();refresh()}
function initClk(id){
 var g=document.getElementById(id+"t");if(!g)return;var s="";
 for(var h=0;h<12;h++){var a=h*30*Math.PI/180,maj=h%3==0;
 var r1=maj?37.5:41,r2=44;
 s+='<line x1="'+(50+Math.sin(a)*r1)+'" y1="'+(50-Math.cos(a)*r1)+
 '" x2="'+(50+Math.sin(a)*r2)+'" y2="'+(50-Math.cos(a)*r2)+
 '" class="'+(maj?"tickmaj":"tick")+'"/>'}
 g.innerHTML=s}
function setHands(pfx,h,m,sec){
 var el=function(x){return document.getElementById(pfx+x)};
 if(!el("h")||!el("m")||!el("s"))return;   // faces not rendered yet
 var f=function(x){el("h").setAttribute("transform","rotate("+(h%12*30+m*.5)+" 50 50)");
 el("m").setAttribute("transform","rotate("+(m*6+sec*.1)+" 50 50)");
 el("s").setAttribute("transform","rotate("+(sec*6)+" 50 50)")}
 f()}
var PT_FMT=null;try{PT_FMT=new Intl.DateTimeFormat("en-US",
 {timeZone:"America/Los_Angeles",hourCycle:"h23",hour:"2-digit",
 minute:"2-digit",second:"2-digit",month:"2-digit",day:"2-digit"})}catch(e){}
function p2c(n){return (n<10?"0":"")+n}
function clockTick(){
 var d=new Date();
 setHands("clkL",d.getHours(),d.getMinutes(),d.getSeconds());
 var ll=document.getElementById("clkLlbl");
 if(ll)ll.innerHTML="<b>"+p2c(d.getHours())+":"+p2c(d.getMinutes())+":"+
 p2c(d.getSeconds())+"</b> "+L("clkLocal")+"<br>"+
 p2c(d.getMonth()+1)+"-"+p2c(d.getDate());
 if(PT_FMT){try{
 var pa={},fpa=PT_FMT.formatToParts(d);
 for(var i=0;i<fpa.length;i++){pa[fpa[i].type]=fpa[i].value}
 var H=+pa.hour%24;
 setHands("clkP",H,+pa.minute,+pa.second);
 var pl=document.getElementById("clkPlbl");
 if(pl)pl.innerHTML="<b>"+p2c(H)+":"+pa.minute+":"+pa.second+
 "</b> "+L("clkPT")+"<br>"+pa.month+"-"+pa.day;
 }catch(e){}}
}
initClk("clkL");initClk("clkP");clockTick();setInterval(clockTick,1000);
var THEME="auto";try{var _st=localStorage.getItem("cgtheme");
if(_st==="auto"||_st==="light"||_st==="dark")THEME=_st}catch(e){}
var MQL=(window.matchMedia?window.matchMedia("(prefers-color-scheme: light)"):null);
function themeIsLight(){return THEME==="auto"?(MQL?!!MQL.matches:false):THEME==="light"}
function syncThemeUi(){
var b=document.getElementById("themeBtn");if(!b)return;
b.setAttribute("data-mode",THEME);
var k=(THEME==="auto")?"themeAuto":(THEME==="light")?"themeLight":"themeDark";
var t=L(k);if(THEME==="auto"){t+=" · "+(themeIsLight()?L("themeLight"):L("themeDark"))}
b.title=t;b.setAttribute("aria-label",t)}
function applyTheme(){var light=themeIsLight();
document.body.classList.toggle("light",light);
try{document.documentElement.style.colorScheme=light?"light":"dark"}catch(e){}
syncThemeUi()}
function _themeFade(){var b=document.body;b.classList.add("theming");
clearTimeout(_themeFade._t);
_themeFade._t=setTimeout(function(){b.classList.remove("theming")},420)}
function setTheme(m){THEME=m;try{localStorage.setItem("cgtheme",m)}catch(e){}
_themeFade();applyTheme();if(typeof refresh==='function')refresh()}
function cycleTheme(){setTheme(THEME==="auto"?"light":(THEME==="light"?"dark":"auto"))}
function _themeFollow(){if(THEME!=="auto")return;_themeFade();applyTheme()}
if(MQL){try{MQL.addEventListener("change",_themeFollow)}catch(e){
if(MQL.addListener)MQL.addListener(_themeFollow)}}
applyTheme();applyLang();refresh();setInterval(refresh,15000);
document.addEventListener("visibilitychange",function(){
if(!document.hidden){refresh()}});
window.addEventListener("focus",function(){refresh()});
</script></body></html>"""

PANEL_HTML = PANEL_HTML.replace("__CXR_VERSION__", __version__)






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
                # JSON-only: cross-site form posts cannot set this type
                # without a CORS preflight, which this server never grants
                ctype = (self.headers.get("Content-Type") or "")
                if ctype.split(";")[0].strip().lower() != "application/json":
                    self._json({"error": "Content-Type must be "
                                         "application/json"}, 415)
                    return
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
                if self.path == "/api/update":
                    if payload.get("confirm") is not True:
                        self._json({"error": "confirmation required"}, 400)
                        return
                    self._json(api.start_update())
                    return
                if self.path == "/api/stats/reset":
                    if payload.get("confirm") is not True:
                        self._json({"error": "confirmation required"}, 400)
                        return
                    self._json(api.reset_stats())
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
            self.httpd.server_close()
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
            busy = codex.busy_threads()
        except Exception:
            threads, goals, busy = [], {}, set()
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
                # honest run state for NON-goal threads: "running" when a
                # live turn exists, "waiting" when a guarded thread is
                # parked on a quota window. Anything else stays unlabeled.
                "run": ("running" if info.id in busy else
                        ("waiting" if (t and (t.tracked or t.manual)
                         and t.last_kind in ("usage_primary", "usage_weekly"))
                         else None)),
                "resumes": (t.success_resumes if t else 0),
                "attempts": (t.total_resumes if t else 0),
                "last_outcome": t.last_outcome if t else "",
                "next_wake": (t.next_wake_iso[:19] if t and t.next_wake_iso
                              else None),
                "updated_ms": info.updated_at_ms,
            })
        # show what the planner acts on: first-hand interruption evidence
        # overrides a snapshot that still trails at 99% after the account
        # already refused a turn
        _eff = getattr(d, "_effective_snapshot", None)
        snap = _eff(d._snapshot) if _eff else d._snapshot
        quota = None
        if snap:
            import time as _time
            from .usage import (WINDOW_EPOCH_TOLERANCE, eta_epoch,
                                pooled_ratio, simulate_primary_eta,
                                simulate_weekly_eta)
            samples = getattr(d, "_usage_samples", [])
            eta_p, rate_p, _ = eta_epoch(samples, 1, 3600)
            # Codex quota model: 5h blocks refill fully at their end and
            # gate the weekly drain; the weekly quota accumulates across
            # blocks. NO fallbacks: every rate/ratio is measured, or the
            # row shows "--".   t is pooled over completed 5h windows
            # because one block's integer-rounded ds swings it too much.
            #   rate_p: primary %/h over the trailing hour (min 10min span)
            #   t:      weekly-% per primary-%, pooled within-block
            win_sec = (snap.primary.window_duration_mins or 300) * 60
            P = snap.primary.used_percent
            S = snap.secondary.used_percent
            b = snap.primary.resets_at
            cur = None
            if (st and b is not None and st.weekly_ratio_b is not None
                    and abs(st.weekly_ratio_b - b)
                    <= WINDOW_EPOCH_TOLERANCE
                    and None not in (st.weekly_ratio_p0, st.weekly_ratio_p1,
                                     st.weekly_ratio_s0,
                                     st.weekly_ratio_s1)):
                dp = st.weekly_ratio_p1 - st.weekly_ratio_p0
                ds = st.weekly_ratio_s1 - st.weekly_ratio_s0
                if dp > 0 and ds > 0:
                    cur = {"b": st.weekly_ratio_b, "dp": dp, "ds": ds}
            t = pooled_ratio(st.weekly_ratio_windows if st else [], cur)
            now = _time.time()
            eta_p = simulate_primary_eta(now, P, rate_p)
            eta_s, capped, _reason = simulate_weekly_eta(
                now, P, S, rate_p, t, b, snap.secondary.resets_at, win_sec)
            rate_s = (rate_p * t) if (rate_p and t) else None
            # weekly ETA retention: while the 5h block is exhausted nothing
            # burns, so t is unmeasurable — freeze the last valid value
            # instead of dropping to "--". If the weekly quota is itself
            # exhausted, both projections are meaningless: "--".
            if S is not None and S >= 100:
                eta_p = None
                eta_s = None
                capped = False
                rate_s = None
            elif P is not None and P >= 100:
                # freeze only within a measured generation: with no pooled
                # t (fresh install / upgrade) an old generation's ETA must
                # not resurface
                eta_s = st.last_weekly_eta if t is not None else None
                capped = False
                rate_s = None
            elif eta_s is not None:
                st.last_weekly_eta = eta_s
            elif capped:
                # the weekly resets before exhaustion: there is no ETA to
                # freeze, and an older generation's value must not resurface
                st.last_weekly_eta = None
            quota = {
                "primary_used": snap.primary.used_percent,
                "primary_reset": snap.primary.resets_at,
                "secondary_used": snap.secondary.used_percent,
                "secondary_reset": snap.secondary.resets_at,
                "secondary_exhausted_at": (
                    st.secondary_exhausted_at if st else None),
                "credits": len(snap.reset_credits),
                "allowed": snap.ordinary_usage_allowed,
                "fetched_at": getattr(d, "_snapshot_wall", None),
                "eta_primary": eta_p,
                "rate_primary": round(rate_p, 2) if rate_p else None,
                "eta_secondary": eta_s,
                "rate_secondary": round(rate_s, 2) if rate_s else None,
                "eta_secondary_capped": bool(capped),
                "weekly_ratio": round(t, 4) if t else None,
                "weekly_ratio_windows": (len(st.weekly_ratio_windows)
                                         if st else 0),
                "credits_count": len(snap.reset_credits),
                "credits_nearest_expiry": (
                    min((c.expires_at for c in snap.reset_credits
                         if c.expires_at), default=None) or None),
            }
        ann = d.cached_announcement()
        if ann and ann.get("posts"):
            # the panel renders news_log; the raw 80-post merge would only
            # bloat every status poll
            ann = {k: v for k, v in ann.items() if k != "posts"}
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
            "update": d.cached_update(),
            "threads": out_threads,
        }

    def start_update(self) -> dict:
        fn = getattr(self.daemon, "begin_update", None)
        if fn is None:
            return {"started": False, "error": "update is not supported"}
        try:
            return fn()
        except Exception as e:     # never let the panel see a stack trace
            return {"started": False, "error": str(e)[:300]}

    def reset_stats(self) -> dict:
        """Zero the displayed resumed/attempts counters.

        Daily/global budget counters are untouched; stats_reset_at stops
        load_state from re-deriving successes out of the kept history.
        """
        from datetime import datetime, timezone
        st = self.daemon.state
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for t in st.threads.values():
            t.success_resumes = 0
            t.total_resumes = 0
        st.stats_reset_at = now_iso
        save_state(st)
        self.daemon._state_mtime = os.path.getmtime(st.state_path)
        return {"reset": True, "at": now_iso}

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

"""Minimal MCP stdio server exposing guard-control tools.

Bundled with the codex-autoresume Codex plugin (mcp.json). Tools proxy to
the daemon's local control API (127.0.0.1), so the daemon stays the single
source of truth:

    autoresume_status               -> quota + per-thread guard state
    autoresume_set_guard            -> toggle guard / weekly / model / effort
                                       for one thread (by id or title match)
    autoresume_pause                -> pause or resume the whole watcher

Speaks JSON-RPC 2.0 over stdio (newline-delimited), enough of the MCP
protocol for tool hosting: initialize / notifications/initialized /
tools/list / tools/call.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

CONTROL_URL = "http://127.0.0.1:8765"

TOOLS = [
    {
        "name": "autoresume_status",
        "description": ("Show the codex-autoresume watcher status: quota "
                        "windows, reset credits, and per-conversation guard "
                        "toggles (guard on/off, cross-week, model, "
                        "reasoning effort)."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "autoresume_set_guard",
        "description": (
            "Configure auto-continue guarding for one Codex conversation. "
            "Pass thread_id when known, otherwise title_contains to match "
            "the conversation by its title (e.g. a distinctive phrase from "
            "the current conversation's topic). Guard resumes the thread "
            "only when it is not running/paused AND the 5h window has "
            "quota; weekly=true also continues after weekly-limit resets "
            "(weekly=false stops permanently once the weekly quota is "
            "exhausted). model/effort apply to each automatic resume "
            "(empty = inherit the thread's own settings)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "title_contains": {"type": "string"},
                "guard": {"type": "boolean"},
                "weekly": {"type": "boolean"},
                "model": {"type": "string"},
                "effort": {"type": "string",
                           "enum": ["", "none", "minimal", "low", "medium",
                                    "high", "xhigh"]},
            },
            "required": ["guard"],
        },
    },
    {
        "name": "autoresume_pause",
        "description": "Pause or resume the whole codex-autoresume watcher.",
        "inputSchema": {
            "type": "object",
            "properties": {"paused": {"type": "boolean"}},
            "required": ["paused"],
        },
    },
]


def _control(path: str, payload: dict | None = None,
             base: str = CONTROL_URL) -> dict:
    url = base.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _resolve_thread(args: dict, status: dict) -> str | dict:
    tid = args.get("thread_id")
    if tid:
        return tid
    frag = (args.get("title_contains") or "").strip().lower()
    if not frag:
        return {"error": "provide thread_id or title_contains"}
    hits = [t for t in status.get("threads", [])
            if frag in (t.get("title") or "").lower()
            or frag in t["id"].lower()]
    if len(hits) == 1:
        return hits[0]["id"]
    if not hits:
        return {"error": "no conversation matches %r" % args["title_contains"]}
    return {"error": "ambiguous match (%d threads): %s" % (
        len(hits), ", ".join((h.get("title") or h["id"][:8])[:30]
                             for h in hits[:5]))}


def tool_call(name: str, args: dict, base: str = CONTROL_URL) -> dict:
    """Execute one tool against the control API (also used by tests)."""
    try:
        if name == "autoresume_status":
            return _control("/api/status", base=base)
        if name == "autoresume_set_guard":
            status = _control("/api/status", base=base)
            resolved = _resolve_thread(args, status)
            if isinstance(resolved, dict):
                return resolved          # error payload
            payload: dict = {"guard": bool(args.get("guard"))}
            for k in ("weekly", "model", "effort"):
                if k in args and args[k] is not None:
                    payload[k] = args[k]
            return _control("/api/threads/%s" % resolved, payload, base=base)
        if name == "autoresume_pause":
            return _control("/api/pause",
                            {"paused": bool(args.get("paused"))}, base=base)
        return {"error": "unknown tool %s" % name}
    except urllib.error.URLError as e:
        return {"error": "daemon control API unreachable at %s (%s). "
                         "Start it with: schtasks /Run /TN codex-autoresume "
                         "or `codex-autoresume daemon`" % (base, e)}
    except Exception as e:  # defensive: never crash the stdio loop
        return {"error": "%s: %s" % (type(e).__name__, e)}


def serve(stdin=sys.stdin.buffer, stdout=sys.stdout.buffer) -> None:
    for line in iter(stdin.readline, b""):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method", "")
        rid = msg.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "codex-autoresume",
                               "version": "0.2.0"},
            }
        elif method == "notifications/initialized":
            continue
        elif rid is None:
            continue                     # other notifications: ignore
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = (msg.get("params") or {}).get("name", "")
            args = (msg.get("params") or {}).get("arguments") or {}
            out = tool_call(name, args)
            result = {"content": [{"type": "text",
                                   "text": json.dumps(out, ensure_ascii=False)}]}
        else:
            _send(stdout, {"jsonrpc": "2.0", "id": rid,
                           "error": {"code": -32601,
                                     "message": "method not found"}})
            continue
        _send(stdout, {"jsonrpc": "2.0", "id": rid, "result": result})


def _send(stdout, obj: dict) -> None:
    stdout.write(json.dumps(obj).encode("utf-8") + b"\n")
    stdout.flush()


def main() -> int:
    serve()
    return 0


if __name__ == "__main__":
    serve()

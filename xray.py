#!/usr/bin/env python3
"""
xray.py — Agent-loop session visualizer (interactive server)

Serves a single-page web app that renders the full Claude Code
"agent loop" from any JSONL hook log under ./sessions/.

Features
--------
* Left: sequence diagram (Claude LLM ⇄ Claude Code ⇄ Tools) with
  clickable cards and step-by-step replay controls.
* Right: terminal replay of the same events.
* Click any card / terminal line to open a modal that shows the full
  JSON payload with syntax highlighting and block folding.
* Dropdown at the top to pick any session JSONL file under ./sessions/.
* Toggle between "Normal" and "Enhanced" modes on the fly.

Usage
-----
    python3 xray.py --port 8000
    python3 xray.py --sessions /path/to/sessions
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------- #
# Event classification
# --------------------------------------------------------------------------- #

LANE_LLM = "llm"
LANE_CODE = "code"
LANE_TOOLS = "tools"
LANE_USER = "user"

# Events kept in NORMAL mode
NORMAL_EVENTS = {
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "StopFailure",
}

# Extra events kept in ENHANCED mode (added on top of NORMAL_EVENTS)
ENHANCED_EXTRA_EVENTS = {
    "PermissionRequest",
    "PermissionDenied",
    "Elicitation",
    "ElicitationResult",
    "SubagentStart",
    "SubagentStop",
    "TaskCreated",
    "TaskCompleted",
}


def _short(s, n: int = 110) -> str:
    if s is None:
        return ""
    s = str(s).replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _preview_tool_input(ti) -> str:
    if not isinstance(ti, dict):
        return _short(ti, 120)
    bits = []
    for k, v in list(ti.items())[:3]:
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        bits.append(f"{k}={_short(v, 50)}")
    return ", ".join(bits)


def classify(evt: dict) -> dict:
    """Return a dict with UI-ready fields for one hook event."""
    name = evt.get("hook_event_name", "Unknown")
    p = evt.get("payload") or {}
    tool = evt.get("tool_name") or p.get("tool_name")

    base = dict(
        kind="other",
        event_name=name,
        from_lane=None, to_lane=None,
        chip_label=name.upper(),
        title=name,
        subtitle="",
    )

    if name == "SessionStart":
        base.update(
            kind="session_start",
            chip_label="SESSION_START",
            title="Session 开始",
            subtitle=f"model={p.get('model', '?')} · source={p.get('source', '?')}",
        )
    elif name == "SessionEnd":
        base.update(
            kind="session_end",
            chip_label="SESSION_END",
            title="Session 结束",
            subtitle=f"reason={p.get('reason', '?')}",
        )
    elif name == "UserPromptSubmit":
        base.update(
            kind="request",
            from_lane=LANE_CODE, to_lane=LANE_LLM,
            chip_label="REQUEST",
            title="Claude Code → LLM 发起 API 请求",
            subtitle=f'prompt = "{_short(p.get("prompt"), 90)}"',
        )
    elif name == "PreToolUse":
        t = tool or "?"
        base.update(
            kind="pretool",
            from_lane=LANE_CODE, to_lane=LANE_TOOLS,
            chip_label="PRE_TOOL_USE",
            title=f"Claude Code 调用工具 {t}",
            subtitle=_preview_tool_input(p.get("tool_input")),
        )
    elif name in ("PostToolUse", "PostToolUseFailure"):
        ok = name == "PostToolUse"
        t = tool or "?"
        resp = p.get("tool_response")
        if isinstance(resp, dict):
            sub = "tool_response keys: " + ", ".join(list(resp.keys())[:4])
        elif isinstance(resp, str):
            sub = _short(resp, 120)
        else:
            sub = ""
        base.update(
            kind="toolresult" if ok else "posttoolfail",
            from_lane=LANE_TOOLS, to_lane=LANE_CODE,
            chip_label="TOOL_RESULT" if ok else "TOOL_RESULT_FAIL",
            title=f"工具 {t} 结果 → 新的 user turn" if ok else f"工具 {t} 执行失败",
            subtitle=sub,
        )
    elif name in ("Stop", "StopFailure"):
        ok = name == "Stop"
        msg = p.get("last_assistant_message") or p.get("reason") or ""
        base.update(
            kind="assistant" if ok else "stopfail",
            from_lane=LANE_LLM, to_lane=LANE_CODE,
            chip_label="ASSISTANT" if ok else "STOP_FAILURE",
            title="模型返回本轮最终响应" if ok else "会话异常终止",
            subtitle=_short(msg, 140),
        )
    # --- enhanced ---
    elif name == "PermissionRequest":
        t = tool or "?"
        base.update(
            kind="permreq",
            from_lane=LANE_CODE, to_lane=LANE_USER,
            chip_label="PERMISSION_REQUEST",
            title=f"请求用户授权工具 {t}",
            subtitle=_preview_tool_input(p.get("tool_input")),
        )
    elif name == "PermissionDenied":
        t = tool or "?"
        base.update(
            kind="permdeny",
            from_lane=LANE_USER, to_lane=LANE_CODE,
            chip_label="PERMISSION_DENIED",
            title=f"用户拒绝工具 {t}",
            subtitle=_short(p.get("reason"), 120),
        )
    elif name == "Elicitation":
        base.update(
            kind="elicit",
            from_lane=LANE_LLM, to_lane=LANE_USER,
            chip_label="ELICITATION",
            title="模型向用户追问",
            subtitle=_short(p.get("message") or p.get("question"), 140),
        )
    elif name == "ElicitationResult":
        base.update(
            kind="elicitres",
            from_lane=LANE_USER, to_lane=LANE_LLM,
            chip_label="ELICITATION_RESULT",
            title="用户回答追问",
            subtitle=_short(p.get("answer") or p.get("response"), 140),
        )
    elif name == "SubagentStart":
        base.update(
            kind="subagent_start",
            from_lane=LANE_CODE, to_lane=LANE_TOOLS,
            chip_label="SUBAGENT_START",
            title=f"子代理启动 agent_id={evt.get('agent_id') or p.get('agent_id') or '?'}",
            subtitle=_short(p.get("description") or p.get("subagent_type"), 120),
        )
    elif name == "SubagentStop":
        base.update(
            kind="subagent_stop",
            from_lane=LANE_TOOLS, to_lane=LANE_CODE,
            chip_label="SUBAGENT_STOP",
            title=f"子代理结束 agent_id={evt.get('agent_id') or p.get('agent_id') or '?'}",
            subtitle=_short(p.get("result"), 120),
        )
    elif name == "TaskCreated":
        base.update(
            kind="task_created",
            from_lane=LANE_CODE, to_lane=LANE_CODE,
            chip_label="TASK_CREATED",
            title="创建子任务",
            subtitle=_short(p.get("subject") or p.get("description"), 140),
        )
    elif name == "TaskCompleted":
        base.update(
            kind="task_completed",
            from_lane=LANE_CODE, to_lane=LANE_CODE,
            chip_label="TASK_COMPLETED",
            title="子任务完成",
            subtitle=_short(p.get("subject") or p.get("description"), 140),
        )
    return base


def load_events(path: Path, mode: str) -> list[dict]:
    keep = set(NORMAL_EVENTS)
    if mode == "enhanced":
        keep |= ENHANCED_EXTRA_EVENTS

    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as e:
                sys.stderr.write(f"warn: {path.name}:{lineno} bad JSON: {e}\n")
                continue
            if rec.get("hook_event_name") not in keep:
                continue
            c = classify(rec)
            c["ts"] = rec.get("ts") or ""
            c["session_id"] = rec.get("session_id") or ""
            c["tool_name"] = rec.get("tool_name")
            c["raw"] = rec
            out.append(c)
    return out


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #

class XrayHandler(BaseHTTPRequestHandler):
    server_version = "XrayServer/1.0"
    # set by main()
    sessions_dir: Path = Path.cwd() / "sessions"

    # --- helpers ----------------------------------------------------------

    def _json(self, status: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: str) -> None:
        b = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write(f"[xray] {self.address_string()} - {fmt % args}\n")

    # --- routes -----------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)

        if route == "/" or route == "/index.html":
            return self._html(INDEX_HTML)

        if route == "/api/sessions":
            files = []
            if self.sessions_dir.exists():
                # newest first — recent sessions are the ones you usually
                # want to inspect
                paths = sorted(self.sessions_dir.glob("*.jsonl"),
                               key=lambda p: p.stat().st_mtime,
                               reverse=True)
                for p in paths:
                    files.append({
                        "name": p.name,
                        "size": p.stat().st_size,
                        "mtime": int(p.stat().st_mtime),
                    })
            return self._json(HTTPStatus.OK, {"sessions": files})

        if route == "/api/session":
            fname = (qs.get("file") or [""])[0]
            mode = (qs.get("mode") or ["normal"])[0]
            if mode not in ("normal", "enhanced"):
                mode = "normal"
            if not fname:
                return self._json(HTTPStatus.BAD_REQUEST,
                                  {"error": "missing ?file="})
            # safety: stay inside sessions_dir
            target = (self.sessions_dir / fname).resolve()
            if not str(target).startswith(str(self.sessions_dir.resolve())):
                return self._json(HTTPStatus.FORBIDDEN,
                                  {"error": "path outside sessions dir"})
            if not target.exists():
                return self._json(HTTPStatus.NOT_FOUND,
                                  {"error": f"no such session: {fname}"})
            events = load_events(target, mode)
            return self._json(HTTPStatus.OK, {
                "file": fname,
                "mode": mode,
                "count": len(events),
                "events": events,
            })

        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})


# --------------------------------------------------------------------------- #
# Front-end (single HTML blob)
# --------------------------------------------------------------------------- #

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Agent Loop X-Ray</title>
<style>
  :root {
    --bg: #fdf8e6;
    --panel: #fffdf4;
    --line: #e8d27a;
    --ink: #1f2937;
    --orange: #d97757;
    --orange-ink: #9a3412;
    --orange-bg: #ffedd5;
    --blue: #2b7fff;
    --blue-bg: #dbeafe;
    --blue-ink: #1e3a8a;
    --green: #0f766e;
    --green-bg: #d1fae5;
    --green-ink: #065f46;
    --red: #dc2626;
    --red-bg: #fee2e2;
    --red-ink: #991b1b;
    --purple: #7c3aed;
    --purple-bg: #ede9fe;
    --purple-ink: #4c1d95;
  }
  * { box-sizing: border-box; }
  html, body { margin:0; padding:0; background:var(--bg); color:var(--ink);
    font-family: "Inter", "PingFang SC", "Noto Sans SC",
                 "Microsoft YaHei", -apple-system, BlinkMacSystemFont,
                 "Helvetica Neue", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility; }
  .app { display:flex; height:100vh; width:100vw; }
  .left { flex: 1.35; overflow:auto; background:var(--panel);
          border-right:1px solid #f3e4a0; }
  .right { flex:1; display:flex; flex-direction:column;
           background:#0b1220; color:#e2e8f0; }

  /* toolbar */
  .toolbar { padding:10px 14px; display:flex; gap:8px; align-items:center;
             background:#fff7d6; border-bottom:1px solid #f3e4a0;
             position:sticky; top:0; z-index:30; flex-wrap:wrap; }
  .toolbar.dark { background:#111a2e; border-color:#1e293b; color:#e2e8f0; }
  .toolbar select, .toolbar button {
    cursor:pointer; padding:6px 12px; border-radius:6px;
    border:1px solid #cbd5e1; background:#fff; font-size:13px;
    font-weight:500; font-family:inherit; color:var(--ink); }
  .toolbar button:hover:not(:disabled) { background:#f1f5f9; }
  .toolbar button:disabled { opacity:0.4; cursor:not-allowed; }
  .toolbar .spacer { flex:1; }
  .toolbar .status { font-size:12px; color:#78350f; }
  .toolbar.dark .status { color:#94a3b8; }
  .toolbar label { font-size:12.5px; color:#78350f; display:flex;
                   gap:6px; align-items:center; }

  .mode-switch {
    display:inline-flex; align-items:center; gap:6px;
    padding:4px 10px; border-radius:999px; background:#fde68a;
    border:1px solid #e9b949; font-size:12px; color:#78350f;
    cursor:pointer; user-select:none;
  }
  .mode-switch.on { background:#fcd34d; }
  .mode-switch .dot { width:8px; height:8px; border-radius:50%;
    background:#b45309; }
  .mode-switch.on .dot { background:#9a3412; }

  /* sequence diagram */
  .diagram { position:relative; padding:0 20px 40px 20px; }
  .lanes {
    display:grid; grid-template-columns:1fr 1fr 1fr;
    position:sticky; top:50px; background:var(--panel);
    padding:18px 0 14px 0; z-index:15;
    border-bottom:1px solid #f5e6a6;
  }
  .lane { display:flex; flex-direction:column; align-items:center; gap:8px; }
  .lane-title { text-align:center; font-weight:700; font-size:17px; }
  .lane-title.llm, .lane-title.code { color:#d97757; }
  .lane-title.tools { color:#2b7fff; }
  .lane-icon { width:36px; height:36px;
    display:inline-flex; align-items:center; justify-content:center; }
  .lane-icon svg { width:100%; height:100%; }
  .lane-icon.llm, .lane-icon.code { color:#d97757; }
  .lane-icon.tools { color:#2b7fff; }

  .tracks { position:relative; padding-top:20px; min-height:60vh; }
  .lane-line { position:absolute; top:0; bottom:0; width:2px;
    background:#f0d36d; opacity:0.55; }
  .lane-line.l1 { left: calc(16.6667% - 1px); }
  .lane-line.l2 { left: 50%; }
  .lane-line.l3 { left: calc(83.3333% - 1px); }

  .step { position:relative; display:grid;
          grid-template-columns:1fr 1fr 1fr; align-items:center;
          padding:22px 0; }
  .step.hidden-step { display:none; }

  .card { position:relative; padding:12px 16px; border-radius:14px;
          background:var(--orange-bg); border:1px solid #f6c684;
          cursor:pointer; box-shadow:0 1px 0 rgba(0,0,0,0.04);
          transition:transform .1s, box-shadow .1s, background .1s;
          z-index:3; margin:0 18px; min-width:0; }
  .card:hover { transform:translateY(-1px);
                box-shadow:0 4px 10px rgba(0,0,0,.08); }
  .card .chip { display:inline-block; font-size:11px; font-weight:800;
     letter-spacing:1px; padding:3px 9px; border-radius:5px;
     margin-bottom:7px; text-transform:uppercase;
     background:#fed7aa; color:var(--orange-ink); }
  .card .title { font-size:15px; font-weight:700; color:#111827;
                 line-height:1.45; overflow-wrap:anywhere;
                 word-break:break-word; }
  .card .sub { font-size:12.5px; color:#4b5563; margin-top:5px;
               line-height:1.5; overflow-wrap:anywhere;
               word-break:break-word; }

  /* kind variants */
  .card.request, .card.pretool, .card.subagent_start {
    background:#ffe8c9; border-color:#f6c684; }
  .card.request .chip, .card.pretool .chip, .card.subagent_start .chip {
    background:#fed7aa; color:var(--orange-ink); }

  .card.assistant, .card.elicit {
    background:#e0ecff; border-color:#b9ceff; }
  .card.assistant .chip, .card.elicit .chip {
    background:#c7dbff; color:var(--blue-ink); }

  .card.toolresult, .card.subagent_stop, .card.elicitres {
    background:#d7f3e4; border-color:#9ee2bd; }
  .card.toolresult .chip, .card.subagent_stop .chip, .card.elicitres .chip {
    background:#b9ebd0; color:var(--green-ink); }

  .card.posttoolfail, .card.stopfail, .card.permdeny {
    background:#fde2e2; border-color:#f8b4b4; }
  .card.posttoolfail .chip, .card.stopfail .chip, .card.permdeny .chip {
    background:#fbcaca; color:var(--red-ink); }

  .card.permreq {
    background:#ede9fe; border-color:#c4b5fd; }
  .card.permreq .chip {
    background:#ddd6fe; color:var(--purple-ink); }

  .card.task_created, .card.task_completed {
    background:#fef3c7; border-color:#fde68a; }
  .card.task_created .chip, .card.task_completed .chip {
    background:#fde68a; color:#92400e; }

  /* card placement by lane pair */
  .card.pair-llm-code, .card.pair-code-llm { grid-column: 1 / span 2;
                                              margin-right:28%; }
  .card.pair-code-tools, .card.pair-tools-code { grid-column: 2 / span 2;
                                                  margin-left:28%; }
  .card.pair-code-user, .card.pair-user-code,
  .card.pair-llm-user, .card.pair-user-llm {
    /* enhanced arrows involving USER; place card in middle column */
    grid-column: 2 / span 1; margin:0 10px; }
  .card.pair-code-code { grid-column: 2 / span 1; margin:0 10px; }

  /* arrows */
  .arrow { position:absolute; height:2.5px; bottom:14px;
           background:var(--orange); z-index:1; }
  .arrow::after {
    content:''; position:absolute; top:-6px; width:0; height:0;
    border-top:7px solid transparent; border-bottom:7px solid transparent; }
  .arrow.lr::after { right:-1px; border-left:11px solid currentColor; }
  .arrow.rl::after { left:-1px; border-right:11px solid currentColor; }

  .arrow.request, .arrow.pretool, .arrow.subagent_start {
    background:#d97757; color:#d97757; }
  .arrow.assistant, .arrow.elicit {
    background:#2b7fff; color:#2b7fff; }
  .arrow.toolresult, .arrow.subagent_stop, .arrow.elicitres {
    background:#0f766e; color:#0f766e; }
  .arrow.posttoolfail, .arrow.stopfail, .arrow.permdeny {
    background:var(--red); color:var(--red); }
  .arrow.permreq { background:var(--purple); color:var(--purple); }

  .arrow.a-llm-code, .arrow.a-code-llm { left:16.6667%; width:33.3333%; }
  .arrow.a-code-tools, .arrow.a-tools-code { left:50%; width:33.3333%; }
  /* User lane is conceptually BELOW code, we draw a short arrow
     in the middle column */
  .arrow.a-code-user, .arrow.a-user-code,
  .arrow.a-llm-user, .arrow.a-user-llm {
    left:42%; width:16%; }

  /* boundary */
  .boundary { position:relative; text-align:center; margin:22px 0; }
  .boundary .bar { height:1.5px; background:#d4a017; opacity:.6; }
  .boundary .pill { display:inline-block; padding:5px 14px;
    background:#fde68a; border:1px solid #e9b949; color:#78350f;
    font-weight:700; font-size:12.5px; border-radius:999px;
    position:relative; top:-13px; cursor:pointer; }
  .boundary .pill:hover { background:#fcd34d; }

  .upcoming { opacity:0.18; filter:grayscale(0.8); pointer-events:none; }
  .active-card { outline:2.5px solid #ef4444; outline-offset:3px; }

  /* terminal */
  .terminal { flex:1; overflow:auto; padding:14px 16px;
              font-family:"JetBrains Mono","SF Mono",Menlo,Consolas,monospace;
              font-size:12.5px; line-height:1.55; }
  .terminal .line { margin:3px 0; cursor:pointer; border-radius:4px;
                    padding:2px 4px; }
  .terminal .line:hover { background:#172133; }
  .terminal .line.active { background:#1e293b; }
  .terminal .t-time { color:#64748b; }
  .terminal .t-event { font-weight:700; }
  .terminal .t-event.sess { color:#a78bfa; }
  .terminal .t-event.req  { color:#fb923c; }
  .terminal .t-event.pre  { color:#fbbf24; }
  .terminal .t-event.tr   { color:#34d399; }
  .terminal .t-event.asst { color:#60a5fa; }
  .terminal .t-event.err  { color:#f87171; }
  .terminal .t-event.perm { color:#c084fc; }
  .terminal .t-event.sub  { color:#2dd4bf; }
  .terminal .t-event.task { color:#fde68a; }
  .terminal .t-event.elicit{ color:#f472b6; }
  .terminal .t-body { color:#cbd5e1; }

  /* modal + JSON tree */
  .modal { position:fixed; inset:0; background:rgba(15,23,42,0.72);
           display:flex; align-items:center; justify-content:center;
           z-index:100; }
  .modal.hidden { display:none; }
  .modal .box { background:#0b1220; color:#e2e8f0; border-radius:10px;
                width:82vw; height:82vh; display:flex; flex-direction:column;
                border:1px solid #1e293b;
                box-shadow:0 20px 50px rgba(0,0,0,0.5); }
  .modal header { padding:10px 14px; border-bottom:1px solid #1e293b;
                  display:flex; align-items:center; gap:12px; }
  .modal header .chip { font-size:11px; font-weight:800;
       letter-spacing:0.6px; padding:3px 8px; border-radius:4px;
       background:#1e293b; color:#fbbf24; }
  .modal header h3 { margin:0; font-size:14px; font-weight:600;
                     color:#e2e8f0; }
  .modal header .meta { font-size:11px; color:#94a3b8; }
  .modal header .actions { margin-left:auto; display:flex; gap:6px; }
  .modal header button { background:#1e293b; color:#e2e8f0;
         border:1px solid #334155; padding:5px 12px; border-radius:4px;
         cursor:pointer; font-size:12px; }
  .modal header button:hover { background:#334155; }
  .modal .body { flex:1; overflow:auto; margin:0;
                 padding:14px 18px;
                 font-family:"JetBrains Mono","SF Mono",Menlo,Consolas,monospace;
                 font-size:12px; line-height:1.55; color:#e2e8f0; }

  /* JSON tree */
  .jt { margin:0; padding:0; }
  .jt ul { list-style:none; margin:0; padding-left:16px;
           border-left:1px dashed #1e293b; }
  .jt li { white-space:pre-wrap; word-break:break-word; }
  .jt .toggle { display:inline-block; width:14px; text-align:center;
     color:#94a3b8; cursor:pointer; user-select:none;
     margin-right:2px; }
  .jt .toggle:hover { color:#fbbf24; }
  .jt .collapsed > ul { display:none; }
  .jt .collapsed .ellipsis { display:inline; }
  .jt .ellipsis { display:none; color:#64748b; }
  .j-key  { color:#7dd3fc; }
  .j-str  { color:#fda4af; }
  .j-num  { color:#fbbf24; }
  .j-bool { color:#c084fc; }
  .j-null { color:#94a3b8; }
  .j-punc { color:#64748b; }
  .empty-hint { padding:40px; text-align:center; color:#78350f;
                font-size:14px; }
</style>
</head>
<body>
<div class="app">
  <div class="left">
    <div class="toolbar">
      <label>Session:
        <select id="sessionSel"></select>
      </label>
      <label class="mode-switch" id="modeSwitch" title="增强模式包含权限、追问、子代理等事件">
        <span class="dot"></span>
        <span id="modeLabel">普通模式</span>
      </label>
      <span style="width:10px"></span>
      <button id="btnPrev">← 上一步</button>
      <button id="btnNext">下一步 →</button>
      <button id="btnPlay">▶ 自动播放</button>
      <button id="btnAll">⏭ 全部展开</button>
      <button id="btnReset">⟳ 重置</button>
      <div class="spacer"></div>
      <span class="status" id="status"></span>
    </div>
    <div class="diagram">
      <div class="lanes">
        <div class="lane">
          <div class="lane-title llm">Claude LLM</div>
          <div class="lane-icon llm" aria-hidden="true">
            <svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
              <path fill="currentColor" d="M245.333 656.64l172.63-96.77 2.9-8.45-2.9-4.69H409.6l-28.93-1.71-98.64-2.73-85.5-3.5-82.86-4.52-20.9-4.35-19.63-25.86 2.05-12.8 17.49-11.78 25.09 2.22 55.64 3.76 83.29 5.8 60.42 3.5 89.6 9.39h14.17l2.05-5.8-4.86-3.58-3.84-3.5-86.19-58.37-93.35-61.78-48.81-35.5-26.45-17.92-13.4-16.98-5.72-36.86 23.9-26.46 32.26 2.22 8.28 2.3 32.6 25.09 69.8 53.93 91.14 67.07 13.31 11.1 5.29-3.76.68-2.65-5.97-10.07-49.5-89.43-52.9-91.05-23.55-37.8-6.23-22.62a108.63 108.63 0 0 1-3.84-26.62l27.3-37.04 15.2-4.95 36.44 4.95 15.36 13.31 22.61 51.71 36.7 81.5 56.83 110.76 16.73 32.85 8.87 30.47 3.33 9.39h5.8V211.2l4.61-62.47 8.7-76.54 8.45-98.56 2.9-27.82 13.74-33.28 27.3-18 21.34 10.24 17.58 25.09-2.48 16.21-10.41 67.67-20.48 106.15-13.31 71.08h7.77l8.87-8.87 36.01-47.79 60.42-75.43 26.71-30.04 31.15-33.02 19.97-15.79h37.8l27.73 41.3-12.37 42.67-38.91 49.24-32.26 41.81-46.25 62.12-28.84 49.75 2.65 4.01 6.83-.77 104.53-22.19 56.4-10.24 67.33-11.43 30.46 14.16 3.33 14.51-11.95 29.44-72.1 17.75-84.4 16.9-125.78 29.78-1.54 1.11 1.79 2.22 56.66 5.29 24.24 1.37h59.3l110.42 8.19 28.93 19.12 17.32 23.3-2.9 17.75-44.37 22.7-60.07-14.25-139.95-33.28-48.04-11.95h-6.66v3.93l40.02 39.08 73.39 66.22 91.74 85.16 4.61 21.16-11.78 16.64-12.46-1.79-80.64-60.59-31.06-27.31-70.49-59.31h-4.69v6.23l16.21 23.72 85.85 128.85 4.44 39.42-6.23 12.97-22.19 7.68-24.49-4.35-50.26-70.4-51.71-79.36-41.81-71-5.12 2.9-24.66 265.39-11.52 13.48-26.71 10.24-22.19-16.9-11.78-27.3 11.78-53.93 14.25-70.4 11.52-55.98 10.41-69.46 6.23-23.04-.43-1.62-5.12.68-52.39 71.94-79.79 107.69-63.15 67.41-15.1 6.06-26.2-13.48 2.47-24.23 14.59-21.59 87.38-110.93 52.65-68.86 34.05-39.77-.26-5.8h-2.05L224.26 751.87l-41.3 5.38-17.83-16.64 2.22-27.3 8.53-8.88 69.72-48.04-.26.25z"/>
            </svg>
          </div>
        </div>
        <div class="lane">
          <div class="lane-title code">Claude Code</div>
          <div class="lane-icon code" aria-hidden="true">
            <svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
              <path fill="currentColor" d="M895.92 467.16H1024v132.35H896v129.2h-63.45v124.63H768V728.7h-63.45v124.63H640V728.7H384v124.63h-64.51V728.7H256v124.63h-64.55V728.7H128V599.47H0V467.2h128V213.33h767.92v253.83zM256 467.16h63.49V345.69H256v121.47zm448.43 0H768V345.69h-63.57v121.47z"/>
            </svg>
          </div>
        </div>
        <div class="lane">
          <div class="lane-title tools">Tools</div>
          <div class="lane-icon tools" aria-hidden="true">
            <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
              <path fill="currentColor" fill-rule="evenodd"
              d="M12 6.75a5.25 5.25 0 0 1 6.775-5.025.75.75 0 0 1 .313 1.248l-3.32 3.319c.063.475.276.934.641 1.299.365.365.824.578 1.3.64l3.318-3.319a.75.75 0 0 1 1.248.313 5.25 5.25 0 0 1-5.472 6.756c-1.018-.086-1.87.1-2.309.634L7.344 21.3A3.298 3.298 0 1 1 2.7 16.657l8.684-7.151c.533-.44.72-1.291.634-2.309A5.342 5.342 0 0 1 12 6.75Z"
              clip-rule="evenodd"/>
            </svg>
          </div>
        </div>
      </div>
      <div class="tracks" id="tracks">
        <div class="lane-line l1"></div>
        <div class="lane-line l2"></div>
        <div class="lane-line l3"></div>
      </div>
    </div>
  </div>

  <div class="right">
    <div class="toolbar dark">
      <strong style="color:#fbbf24;">Terminal Replay</strong>
      <div class="spacer"></div>
      <span class="status" id="termStatus"></span>
    </div>
    <div class="terminal" id="terminal"></div>
  </div>
</div>

<div class="modal hidden" id="modal">
  <div class="box">
    <header>
      <span class="chip" id="modalChip">?</span>
      <h3 id="modalTitle">?</h3>
      <span class="meta" id="modalMeta"></span>
      <div class="actions">
        <button id="btnExpandAll">展开全部</button>
        <button id="btnCollapseAll">折叠全部</button>
        <button id="btnCopy">复制 JSON</button>
        <button id="modalClose">关闭 ✕</button>
      </div>
    </header>
    <div class="body" id="modalBody"></div>
  </div>
</div>

<script>
let EVENTS = [];
let CURRENT_FILE = null;
let MODE = 'normal';
let currentStep = 0;
let playTimer = null;

const tracks = document.getElementById('tracks');
const terminal = document.getElementById('terminal');
const sessionSel = document.getElementById('sessionSel');
const modeSwitch = document.getElementById('modeSwitch');
const modeLabel = document.getElementById('modeLabel');

const termClassForKind = {
  session_start:'sess', session_end:'sess',
  request:'req', assistant:'asst',
  pretool:'pre', toolresult:'tr',
  posttoolfail:'err', stopfail:'err',
  permreq:'perm', permdeny:'err',
  elicit:'elicit', elicitres:'elicit',
  subagent_start:'sub', subagent_stop:'sub',
  task_created:'task', task_completed:'task',
};
const termLabelForKind = {
  session_start:'SESSION_START', session_end:'SESSION_END',
  request:'USER_PROMPT→LLM', assistant:'LLM_ASSISTANT',
  pretool:'PRE_TOOL_USE', toolresult:'POST_TOOL_USE',
  posttoolfail:'POST_TOOL_USE_FAIL', stopfail:'STOP_FAILURE',
  permreq:'PERMISSION_REQUEST', permdeny:'PERMISSION_DENIED',
  elicit:'ELICITATION', elicitres:'ELICITATION_RESULT',
  subagent_start:'SUBAGENT_START', subagent_stop:'SUBAGENT_STOP',
  task_created:'TASK_CREATED', task_completed:'TASK_COMPLETED',
};

function htmlEscape(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d)) return ts;
  return d.toLocaleTimeString('zh-CN', {hour12:false})
       + '.' + String(d.getMilliseconds()).padStart(3,'0');
}

/* ---------- fetch sessions list ---------- */
async function loadSessionsList(preselect) {
  const resp = await fetch('/api/sessions');
  const data = await resp.json();
  sessionSel.innerHTML = '';
  (data.sessions || []).forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.name;
    opt.textContent = s.name;
    sessionSel.appendChild(opt);
  });
  if (data.sessions && data.sessions.length) {
    const target = preselect && data.sessions.find(s => s.name === preselect)
                 ? preselect : data.sessions[0].name;
    sessionSel.value = target;
    await loadSession(target);
  } else {
    EVENTS = [];
    buildDiagram(); buildTerminal(); render();
    tracks.innerHTML =
      '<div class="empty-hint">⚠ 未在 sessions/ 目录下找到任何 *.jsonl 文件</div>';
  }
}

async function loadSession(name) {
  CURRENT_FILE = name;
  const resp = await fetch('/api/session?file='
                 + encodeURIComponent(name) + '&mode=' + MODE);
  const data = await resp.json();
  EVENTS = data.events || [];
  currentStep = 0;
  buildDiagram();
  buildTerminal();
  render();
}

/* ---------- diagram ---------- */
function buildDiagram() {
  tracks.innerHTML =
    '<div class="lane-line l1"></div>' +
    '<div class="lane-line l2"></div>' +
    '<div class="lane-line l3"></div>';

  if (!EVENTS.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-hint';
    empty.textContent = '该会话没有可展示的事件（试试切到“增强模式”）';
    tracks.appendChild(empty);
    return;
  }

  EVENTS.forEach((e, i) => {
    if (e.kind === 'session_start' || e.kind === 'session_end') {
      const b = document.createElement('div');
      b.className = 'boundary step';
      b.dataset.index = i;
      b.innerHTML =
        '<div class="bar"></div>' +
        '<div class="pill" data-idx="' + i + '">⟦ '
          + htmlEscape(e.chip_label) + ' ⟧ '
          + htmlEscape(e.title) + '</div>';
      b.querySelector('.pill').addEventListener('click', () => openModal(i));
      tracks.appendChild(b);
      return;
    }

    const step = document.createElement('div');
    step.className = 'step';
    step.dataset.index = i;

    const pair = (e.from_lane || '') + '-' + (e.to_lane || '');
    const arrowPair = pair;
    // Arrow direction by lane geometry:
    //   LLM (16.67%)  Code (50%)  Tools (83.33%)  —  "rl" means tip on
    //   the left side, "lr" means tip on the right side.
    let arrowDir = 'lr';
    if (pair === 'code-llm'    // user prompt: Code (middle) → LLM (left)
        || pair === 'tools-code' // tool result: Tools (right) → Code (mid)
        || pair === 'user-code'  // enhanced: user → code (back into diagram)
        || pair === 'user-llm')  // enhanced: user → llm (back into diagram)
      arrowDir = 'rl';
    // code-code (tasks) has no directional arrow; treat as lr default
    const hasArrow = !!(e.from_lane && e.to_lane && e.from_lane !== e.to_lane);

    let cardSide = 'pair-' + pair;

    step.innerHTML =
      (hasArrow
         ? '<div class="arrow ' + e.kind + ' a-' + arrowPair
            + ' ' + arrowDir + '"></div>'
         : '')
      + '<div class="card ' + e.kind + ' ' + cardSide
          + '" data-idx="' + i + '">'
          + '<span class="chip">' + htmlEscape(e.chip_label) + '</span>'
          + '<div class="title">' + htmlEscape(e.title) + '</div>'
          + (e.subtitle ? '<div class="sub">'
               + htmlEscape(e.subtitle) + '</div>' : '')
      + '</div>';

    step.querySelector('.card').addEventListener('click',
      () => openModal(i));
    tracks.appendChild(step);
  });
}

function buildTerminal() {
  terminal.innerHTML = '';
  EVENTS.forEach((e, i) => {
    const cls = termClassForKind[e.kind] || 'sess';
    const lbl = termLabelForKind[e.kind] || e.chip_label;
    const time = fmtTime(e.ts);
    const row = document.createElement('div');
    row.className = 'line';
    row.dataset.idx = i;
    let body = '';
    if (e.subtitle)
      body = ' <span class="t-body">' + htmlEscape(e.subtitle) + '</span>';
    row.innerHTML =
      '<span class="t-time">[' + htmlEscape(time) + ']</span> ' +
      '<span class="t-event ' + cls + '">' + htmlEscape(lbl) + '</span> ' +
      '<span class="t-body">' + htmlEscape(e.title) + '</span>' + body;
    row.addEventListener('click', () => openModal(i));
    terminal.appendChild(row);
  });
}

/* ---------- step control ---------- */
function render() {
  const steps = tracks.querySelectorAll('.step');
  steps.forEach(s => {
    const i = Number(s.dataset.index);
    s.classList.toggle('upcoming', i >= currentStep);
  });
  const lines = terminal.querySelectorAll('.line');
  lines.forEach(ln => {
    const i = Number(ln.dataset.idx);
    ln.style.display = (i < currentStep) ? '' : 'none';
  });
  document.getElementById('btnPrev').disabled = currentStep <= 0;
  document.getElementById('btnNext').disabled = currentStep >= EVENTS.length;
  document.getElementById('status').textContent =
    'step ' + currentStep + ' / ' + EVENTS.length
    + '  ·  ' + (CURRENT_FILE || '—');
  document.getElementById('termStatus').textContent =
    currentStep + ' / ' + EVENTS.length + ' events';

  terminal.scrollTop = terminal.scrollHeight;

  if (currentStep > 0 && currentStep <= EVENTS.length) {
    const target = tracks.querySelector(
      '.step[data-index="' + (currentStep - 1) + '"]');
    if (target) target.scrollIntoView({behavior:'smooth', block:'center'});
  }
}

function next() { if (currentStep < EVENTS.length) { currentStep++; render(); } }
function prev() { if (currentStep > 0) { currentStep--; render(); } }
function reset() { currentStep = 0; render(); stopPlay(); }
function showAll() { currentStep = EVENTS.length; render(); stopPlay(); }
function stopPlay() {
  if (playTimer) { clearInterval(playTimer); playTimer = null; }
  document.getElementById('btnPlay').textContent = '▶ 自动播放';
}
function togglePlay() {
  if (playTimer) { stopPlay(); return; }
  document.getElementById('btnPlay').textContent = '⏸ 暂停';
  playTimer = setInterval(() => {
    if (currentStep >= EVENTS.length) { stopPlay(); return; }
    next();
  }, 900);
}

document.getElementById('btnPrev').addEventListener('click', prev);
document.getElementById('btnNext').addEventListener('click', next);
document.getElementById('btnReset').addEventListener('click', reset);
document.getElementById('btnAll').addEventListener('click', showAll);
document.getElementById('btnPlay').addEventListener('click', togglePlay);

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if (e.key === 'ArrowRight' || e.key === ' ') { e.preventDefault(); next(); }
  if (e.key === 'ArrowLeft') prev();
  if (e.key === 'Escape') closeModal();
});

sessionSel.addEventListener('change', () => {
  loadSession(sessionSel.value);
});

modeSwitch.addEventListener('click', async () => {
  MODE = (MODE === 'normal') ? 'enhanced' : 'normal';
  modeSwitch.classList.toggle('on', MODE === 'enhanced');
  modeLabel.textContent = (MODE === 'enhanced') ? '增强模式' : '普通模式';
  if (CURRENT_FILE) await loadSession(CURRENT_FILE);
});

/* ---------- modal w/ JSON tree ---------- */
const modal = document.getElementById('modal');
const modalBody = document.getElementById('modalBody');
const modalTitle = document.getElementById('modalTitle');
const modalChip = document.getElementById('modalChip');
const modalMeta = document.getElementById('modalMeta');

function renderPrimitive(v) {
  if (v === null) return '<span class="j-null">null</span>';
  if (typeof v === 'boolean') return '<span class="j-bool">' + v + '</span>';
  if (typeof v === 'number') return '<span class="j-num">' + v + '</span>';
  if (typeof v === 'string') return '<span class="j-str">' +
       htmlEscape(JSON.stringify(v)) + '</span>';
  return htmlEscape(String(v));
}

/* Render a value as either a leaf <span> or a collapsible <li> tree.
   Depth 0 creates the root <li>. */
function renderJsonNode(key, value, isLast) {
  const li = document.createElement('li');
  const comma = isLast ? '' : '<span class="j-punc">,</span>';
  const keyHtml = (key !== null)
      ? '<span class="j-key">' + htmlEscape(JSON.stringify(key))
        + '</span><span class="j-punc">: </span>'
      : '';

  if (value === null || typeof value !== 'object') {
    li.innerHTML = keyHtml + renderPrimitive(value) + comma;
    return li;
  }
  const isArr = Array.isArray(value);
  const entries = isArr
      ? value.map((v, i) => [i, v])
      : Object.entries(value);

  const openB = isArr ? '[' : '{';
  const closeB = isArr ? ']' : '}';
  if (!entries.length) {
    li.innerHTML = keyHtml +
      '<span class="j-punc">' + openB + closeB + '</span>' + comma;
    return li;
  }

  li.innerHTML =
      '<span class="toggle" data-role="t">▾</span>' +
      keyHtml +
      '<span class="j-punc">' + openB + '</span>' +
      '<span class="ellipsis j-punc">…' + entries.length + ' '
         + (isArr ? 'items' : 'keys') + '…</span>';

  const ul = document.createElement('ul');
  entries.forEach(([k, v], idx) => {
    const last = idx === entries.length - 1;
    ul.appendChild(renderJsonNode(isArr ? null : k, v, last));
  });
  li.appendChild(ul);

  const end = document.createElement('span');
  end.innerHTML = '<span class="j-punc">' + closeB + '</span>' + comma;
  li.appendChild(end);

  li.querySelector('[data-role="t"]').addEventListener('click', ev => {
    ev.stopPropagation();
    const collapsed = li.classList.toggle('collapsed');
    li.querySelector('[data-role="t"]').textContent =
        collapsed ? '▸' : '▾';
  });
  return li;
}

function renderJsonTree(value) {
  const root = document.createElement('ul');
  root.className = 'jt';
  root.appendChild(renderJsonNode(null, value, true));
  return root;
}

function setAllCollapsed(state) {
  modalBody.querySelectorAll('.jt li').forEach(li => {
    if (!li.querySelector(':scope > ul')) return;
    li.classList.toggle('collapsed', state);
    const t = li.querySelector(':scope > [data-role="t"]');
    if (t) t.textContent = state ? '▸' : '▾';
  });
}

function openModal(i) {
  const e = EVENTS[i];
  modal.classList.remove('hidden');
  modalChip.textContent = e.chip_label;
  modalTitle.textContent = e.title;
  modalMeta.textContent =
    fmtTime(e.ts) + '  ·  session ' + (e.session_id || '')
    + (e.tool_name ? '  ·  tool=' + e.tool_name : '');
  modalBody.innerHTML = '';
  modalBody.appendChild(renderJsonTree(e.raw));

  document.querySelectorAll('.active-card').forEach(
    x => x.classList.remove('active-card'));
  const card = tracks.querySelector('[data-idx="' + i + '"]');
  if (card) card.classList.add('active-card');
  terminal.querySelectorAll('.line.active').forEach(
    x => x.classList.remove('active'));
  const row = terminal.querySelector('.line[data-idx="' + i + '"]');
  if (row) row.classList.add('active');
}

function closeModal() { modal.classList.add('hidden'); }

document.getElementById('modalClose').addEventListener('click', closeModal);
document.getElementById('btnExpandAll').addEventListener('click',
  () => setAllCollapsed(false));
document.getElementById('btnCollapseAll').addEventListener('click',
  () => setAllCollapsed(true));
document.getElementById('btnCopy').addEventListener('click', async () => {
  const e = EVENTS.find(x => x.title === modalTitle.textContent) || null;
  // copy via currently-shown event raw
  try {
    const idx = Array.from(tracks.querySelectorAll('.active-card'))
        .map(x => Number(x.dataset.idx || x.dataset.index))[0];
    const ev = (idx != null && EVENTS[idx]) ? EVENTS[idx] : e;
    if (!ev) return;
    await navigator.clipboard.writeText(
        JSON.stringify(ev.raw, null, 2));
    const btn = document.getElementById('btnCopy');
    const old = btn.textContent;
    btn.textContent = '✓ 已复制';
    setTimeout(() => { btn.textContent = old; }, 1200);
  } catch (err) { alert('复制失败: ' + err); }
});
modal.addEventListener('click', ev => { if (ev.target === modal) closeModal(); });

/* ---------- init ---------- */
loadSessionsList();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--sessions", default="~/.claude/session-logs", help="Default: ${HOME}/.claude/session-logs")
    ap.add_argument("--no-open", action="store_true", help="do not auto-open the browser")
    args = ap.parse_args(argv)

    sessions = Path(args.sessions).expanduser().resolve()
    if not sessions.exists():
        sys.stderr.write(f"error: sessions dir not found: {sessions}\n")
        return 2

    XrayHandler.sessions_dir = sessions

    httpd = ThreadingHTTPServer((args.host, args.port), XrayHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"▶ xray server listening on {url}")
    print(f"  sessions: {sessions}")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

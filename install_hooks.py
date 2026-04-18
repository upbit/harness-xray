#!/usr/bin/env python3
"""
install_hooks.py
================

One-shot installer for the harness-xray logging setup.

What it does
------------
1. Copies `log-jsonl.sh` from this repo into `~/.claude/hooks/log-jsonl.sh`
   and marks it executable (`chmod +x`).
2. Merges a fixed set of hook events into `~/.claude/settings.json`
   under `.hooks.<EventName>`. All events share the same single command,
   so the reference definitions live inline (below) as two arrays:
     * TOOL_EVENTS  — events that take a `"matcher": "*"`
     * PLAIN_EVENTS — events without a matcher
   Merge semantics: conflicting keys under `.hooks` are overwritten;
   everything else in the user's settings is preserved.
3. Writes a timestamped backup of the pre-existing user settings
   before overwriting it.

Usage
-----
    python3 install_hooks.py                  # apply
    python3 install_hooks.py --dry-run        # preview, write nothing
    python3 install_hooks.py --target ~/other-settings.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import stat
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# Reference hook set (edit here, not in a JSON file)
# --------------------------------------------------------------------------- #

HOOK_CMD = "~/.claude/hooks/log-jsonl.sh"

# Events that fire on a tool call — register with matcher "*" so every
# tool is captured without having to enumerate names.
TOOL_EVENTS = [
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "PermissionDenied",
]

# Everything else — no matcher.
PLAIN_EVENTS = [
    # Normal mode — LLM ⇄ Code ⇄ Tools loop boundaries + user prompts.
    "SessionStart", "SessionEnd", "UserPromptSubmit",
    "Stop", "StopFailure",
    # Enhanced mode — elicitation round-trips and subagent/task lifecycle.
    "Elicitation", "ElicitationResult",
    "SubagentStart", "SubagentStop",
    "TaskCreated", "TaskCompleted",
]


def build_ref_hooks() -> dict:
    """Assemble the reference `.hooks` object from TOOL_EVENTS / PLAIN_EVENTS."""
    cmd = {"type": "command", "command": HOOK_CMD}
    ref: dict = {}
    for e in TOOL_EVENTS:
        ref[e] = [{"matcher": "*", "hooks": [cmd]}]
    for e in PLAIN_EVENTS:
        ref[e] = [{"hooks": [cmd]}]
    return ref


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def ensure_executable(path: Path) -> None:
    st = path.stat()
    path.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def backup_file(path: Path) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak.{ts}")
    shutil.copy2(path, backup)
    return backup


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #

def install_hook_script(repo: Path, dry_run: bool) -> None:
    src = repo / "log-jsonl.sh"
    if not src.exists():
        sys.exit(f"error: {src} not found")
    dst = Path.home() / ".claude" / "hooks" / "log-jsonl.sh"

    print(f"[hook script] {src} -> {dst}")
    if dry_run:
        print("          (dry-run: not copied)")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    ensure_executable(dst)


def merge_settings(target: Path, dry_run: bool) -> None:
    ref_hooks = build_ref_hooks()

    if target.exists():
        try:
            user = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            sys.exit(f"error: {target} is not valid JSON ({e}). "
                     "Refusing to merge; fix it or move it aside and rerun.")
        if not isinstance(user, dict):
            sys.exit(f"error: {target} is not a JSON object")
    else:
        print(f"[settings.json] {target} does not exist — will create it")
        user = {}

    existing = user.get("hooks") if isinstance(user.get("hooks"), dict) else {}
    merged = {**existing, **ref_hooks}                # conflicts overwrite

    added       = [e for e in ref_hooks if e not in existing]
    overwritten = [e for e in ref_hooks if e in existing]
    untouched   = [e for e in existing  if e not in ref_hooks]
    print("[merge] hooks added:       " + (", ".join(added)       or "—"))
    print("[merge] hooks overwritten: " + (", ".join(overwritten) or "—"))
    print("[merge] hooks untouched:   " + (", ".join(untouched)   or "—"))

    user["hooks"] = merged
    out = json.dumps(user, ensure_ascii=False, indent=2) + "\n"

    print(f"[settings.json] {target}")
    if dry_run:
        print("          (dry-run: not written)")
        return

    if target.exists():
        print(f"[backup] {backup_file(target)}")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(out, encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", type=Path,
                    default=Path(__file__).resolve().parent,
                    help="repo root containing log-jsonl.sh "
                         "(default: the folder this script lives in)")
    ap.add_argument("--target", type=Path,
                    default=Path.home() / ".claude" / "settings.json",
                    help="target user settings.json "
                         "(default: ~/.claude/settings.json)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would change, write nothing")
    args = ap.parse_args(argv)

    repo = args.repo.expanduser().resolve()
    target = args.target.expanduser().resolve()

    print(f"repo:   {repo}")
    print(f"target: {target}")
    print(f"dry run: {args.dry_run}\n")

    install_hook_script(repo, args.dry_run)
    print()
    merge_settings(target, args.dry_run)

    print("\n✔ done." if not args.dry_run
          else "\n✔ dry-run complete (no changes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

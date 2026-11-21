#!/usr/bin/env python3
"""Claude Code hook for the Herdr artifacts panel. One of several sources: the
scanner (artifacts.py) also reads transcripts, subagent transcripts and the
Claude desktop app's records, so this hook adds facts and never replaces them.

Install with `python3 install.py` (adds it to every Claude Code profile).

    SessionStart  -> state/terminals/<HC_TERMINAL_ID>.json: which session runs in
                     which Herdr terminal, its transcript and profile. Survives a
                     changed sessions/<pid>.json format and follows /clear and /resume.
    PostToolUse   -> state/events/<session>.jsonl: one line per Artifact,
      (Artifact*)    ArtifactComments or ArtifactData call, with its structured
                     input and result, the subagent that made it, and the terminal.

It must never slow down or break a session: standard library only, no output,
every error swallowed, exit 0. Records are small: large inputs (file bodies,
edits) are dropped, results are cut to 4 KB.
"""

import json
import os
import sys
import time

STATE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "herdr-artifacts")
KEEP_INPUT = ("action", "url", "title", "description", "file_path", "icon", "type_url", "asset", "path", "scope", "label", "collection")
RESULT_MAX = 4096


def text_of(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(text_of(v) for v in value)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if "content" in value:
            return text_of(value["content"])
        try:
            return json.dumps(value)
        except (TypeError, ValueError):
            return ""
    return ""


def write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def main() -> None:
    event = json.load(sys.stdin)
    if not isinstance(event, dict):
        return
    name = event.get("hook_event_name")
    sid = str(event.get("session_id") or "")
    if not sid or "/" in sid:
        return
    terminal = os.environ.get("HC_TERMINAL_ID", "")
    common = {
        "ts": time.time(),
        "session": sid,
        "terminal": terminal,
        "transcript": event.get("transcript_path") or "",
        "cwd": event.get("cwd") or "",
        "profile": os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
        "pid": os.getppid(),  # the Claude Code process that ran this hook
    }
    if name == "SessionStart":
        if terminal and "/" not in terminal:
            write_json(os.path.join(STATE, "terminals", f"{terminal}.json"), {**common, "source": event.get("source") or ""})
        return
    if name == "PostToolUse" and str(event.get("tool_name") or "").startswith("Artifact"):
        raw = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
        line = {
            **common,
            "tool": event.get("tool_name"),
            "tool_use_id": event.get("tool_use_id") or "",
            "agent": event.get("agent_id") or "",
            "agent_type": event.get("agent_type") or "",
            "input": {k: raw[k] for k in KEEP_INPUT if k in raw and isinstance(raw[k], (str, bool, int, float))},
            "result": text_of(event.get("tool_response"))[:RESULT_MAX],
        }
        path = os.path.join(STATE, "events", f"{sid}.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a hook must never break the session
        pass
    sys.exit(0)

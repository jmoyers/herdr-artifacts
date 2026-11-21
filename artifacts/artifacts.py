#!/usr/bin/env python3
"""Artifacts scanner for the Herdr app's `artifacts` mod.

For each herdr-core terminal given (or every live one), find the Claude Code
session running in it and build one picture of the claude.ai artifacts that
conversation made, linked or looked at. Prints one JSON object.

    python3 artifacts.py [--terminals t1,t2]

Sources, merged (each fills what the others miss):
- Terminal -> session: the hook's record for the terminal (state/terminals/<t>.json,
  written at SessionStart), else the Claude process in the terminal and its
  `<profile>/sessions/<pid>.json`.
- The session transcript (`<profile>/projects/<cwd>/<session>.jsonl`): every
  Artifact call and result, the agent's replies (links it gave you), compaction
  summaries (work from before the transcript was cut).
- Subagent transcripts (`<session>/subagents/agent-*.jsonl`): artifacts a
  native subagent published.
- Hook events (state/events/<session>.jsonl, from hook.py): structured Artifact
  calls, including any the transcript lacks or words differently.
- Child terminals (`hc run` from this agent, e.g. hsub subagents): their
  publishes roll up under the parent, marked "via".
- Titles: the publish input, the Claude desktop app's records, the page's
  <title>, the comment monitor, Artifact `list` results, the agent's own link
  text, and a registry of every title seen in any session (cache/titles.json),
  so a bare URL still gets its name.
Profiles: ~/.claude, $CLAUDE_CONFIG_DIR and every ~/.claude-<name> (one per account).

Each artifact is one of: published (this conversation, or a child, made or
updated it), linked (the agent gave you a link to it in a reply), or seen (it
only read, opened or mentioned it in passing).

Results are cached under ~/.cache/herdr-artifacts by the size and mtime of
every input file. Standard library only.
"""

from __future__ import annotations

import glob
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime

ARTIFACT_URL_RE = re.compile(r"https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+")
PUBLISHED_RE = re.compile(
    r"^(?:Published .+? at|Updated the Artifact at|Created a new Artifact at) "
    r"(https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+)",
    re.S,
)
# "Version 12," or "Version 12)" is a version number; "version 1793114885-93b0" is a version id.
VERSION_RE = re.compile(r"\(Version (\d+)(?=[,)])")
OPENED_RE = re.compile(r"^Opened the Artifact at (https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+)")
# A row of an Artifact `list` result: "- (mine) Title — https://claude.ai/artifact/X — updated …"
LIST_ROW_RE = re.compile(r"^- (?:\([^)\n]*\) )?(.+?) [—-] (https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+)", re.M)
HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
# A title next to a link in prose: [Title](url), "Title" (url), **Title** (url).
MENTION_RES = [
    re.compile(r"\[([^\]\n]{2,120})\]\((https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+)[^)]*\)"),
    re.compile(r"[\"“]([^\"”\n]{2,120})[\"”][^()\n]{0,40}\((?:[^()\n]{0,60}, )?(https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+)[^)]*\)"),
    re.compile(r"\*\*([^*\n]{2,120})\*\*[^()\n]{0,40}\((?:[^()\n]{0,60}, )?(https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+)[^)]*\)"),
]
PARSER_VERSION = 7
# Links the agent wrote in its replies: markdown links first, then bare URLs.
MD_LINK_RE = re.compile(r"\[([^\]\n]{1,160})\]\((https?://[^\s)]+)\)")
BARE_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`]+")
LINK_MAX = 80
CHILD_MAX = 24  # terminals to check for children of the shown agents

CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "herdr-artifacts")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "herdr-artifacts")
# Which title to believe, best first.
TITLE_RANK = {"input": 9, "desktop": 8, "server": 7, "html": 6, "monitor": 5, "mention": 4, "file": 2, "id": 0, "": 0}


def claude_config_dirs() -> list[str]:
    """Every Claude Code profile on this machine: $CLAUDE_CONFIG_DIR, ~/.claude, and
    any ~/.claude-<name> (one per account, e.g. ~/.claude-work) that has sessions."""
    dirs = [os.environ.get("CLAUDE_CONFIG_DIR") or "", os.path.expanduser("~/.claude")]
    dirs += sorted(d for d in glob.glob(os.path.expanduser("~/.claude-*")) if os.path.isdir(os.path.join(d, "sessions")))
    out: list[str] = []
    for d in dirs:
        if d and os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def canonical_url(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/")


def parse_ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e11 else 1.0)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Terminals -> sessions
# --------------------------------------------------------------------------- #


def hc(*args: str):
    try:
        proc = subprocess.run(["hc", *args, "--json"], capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        return None


def session_for_pid(pid: int) -> dict | None:
    for base in claude_config_dirs():
        data = read_json(os.path.join(base, "sessions", f"{pid}.json"))
        if isinstance(data, dict) and isinstance(data.get("sessionId"), str):
            return {**data, "profile": base}
    return None


def child_pids(pid: int) -> list[int]:
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=3, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def claude_session_in(terminal: dict) -> dict | None:
    """The Claude Code session running in a terminal. Two ways, checked against
    each other: the hook's SessionStart record for this terminal, and the Claude
    process in it (the foreground process or a child: claude started through a
    wrapper or `npx`) with its sessions/<pid>.json."""
    by_pid = None
    fg = terminal.get("foreground") or {}
    queue = [p for p in (fg.get("pid"), terminal.get("pid")) if isinstance(p, int)]
    seen: set[int] = set()
    for _depth in range(4):
        nxt = []
        for pid in queue:
            if pid in seen:
                continue
            seen.add(pid)
            by_pid = session_for_pid(pid)
            if by_pid:
                break
            nxt.extend(child_pids(pid))
        if by_pid or not nxt:
            break
        queue = nxt
    address = terminal.get("address") or ""
    hooked = read_json(os.path.join(STATE_DIR, "terminals", f"{address}.json")) if address and "/" not in address else None
    if isinstance(hooked, dict) and hooked.get("session") and pid_alive(hooked.get("pid")):
        hook_session = {"sessionId": hooked["session"], "pid": hooked.get("pid"), "profile": hooked.get("profile") or "",
                        "transcript": hooked.get("transcript") or "", "cwd": hooked.get("cwd") or "", "via_hook": True}
        if not by_pid:
            return hook_session
        # Same process, different session: /clear or /resume started a new one; the
        # newer record wins.
        if by_pid.get("pid") == hooked.get("pid") and by_pid["sessionId"] != hooked["session"]:
            if float(hooked.get("ts") or 0) > parse_ts(by_pid.get("updatedAt") or by_pid.get("startedAt")):
                return hook_session
        if by_pid["sessionId"] == hooked["session"]:
            by_pid["transcript"] = hooked.get("transcript") or ""
    return by_pid


def transcript_path(session: dict) -> str:
    sid = session.get("sessionId") or ""
    if not sid or "/" in sid or sid.startswith("."):
        return ""
    known = session.get("transcript") or ""
    if known and os.path.exists(known):
        return known
    bases = [session["profile"]] if session.get("profile") else []
    bases += [b for b in claude_config_dirs() if b not in bases]
    for base in bases:
        matches = glob.glob(os.path.join(base, "projects", "*", f"{sid}.jsonl"))
        if matches:
            return max(matches, key=os.path.getmtime)
    return ""


# --------------------------------------------------------------------------- #
# Sources -> one picture
# --------------------------------------------------------------------------- #


class Collector:
    """Everything known about one conversation's artifacts, fed by transcripts,
    subagent transcripts and hook events, then finished into lists."""

    def __init__(self) -> None:
        self.artifacts: dict[str, dict] = {}
        self.mentions: dict[str, dict] = {}   # url -> {title, ts, summary, reply}
        self.links: dict[str, dict] = {}      # url -> {url, title, first_ts, last_ts, count}
        self.titles: dict[str, tuple[str, str]] = {}  # url -> (title, source) learned here
        self.monitor_titles: dict[str, str] = {}
        self.tool_ids: set[str] = set()
        self.cwd = ""
        self.sources: dict[str, int] = {"transcript": 0, "subagents": 0, "hook_events": 0}

    def art(self, url: str) -> dict:
        url = canonical_url(url)
        a = self.artifacts.get(url)
        if a is None:
            a = self.artifacts[url] = {
                "url": url, "title": "", "title_source": "", "file_path": "", "description": "", "icon": "",
                "type_url": "", "publishes": 0, "version": 0, "first_ts": 0.0, "last_ts": 0.0, "touched": 0,
                "linked": 0, "summarized": False, "via": "",
            }
        return a

    def learn_title(self, url: str, title: str, source: str) -> None:
        url, title = canonical_url(url), (title or "").strip()
        if not title:
            return
        cur = self.titles.get(url)
        if cur is None or TITLE_RANK.get(source, 0) >= TITLE_RANK.get(cur[1], 0):
            self.titles[url] = (title, source)

    def on_result(self, inp: dict, ts: float, text: str, via: str = "") -> None:
        action = inp.get("action") or "publish"
        if action == "list":
            for m in LIST_ROW_RE.finditer(text):
                self.learn_title(m.group(2), m.group(1), "server")
            return
        if action == "publish" and not inp.get("asset"):
            m = PUBLISHED_RE.match(text)
            if m:
                a = self.art(m.group(1))
                a["publishes"] += 1
                vm = VERSION_RE.search(text)
                a["version"] = max(a["version"], int(vm.group(1)) if vm else a["publishes"])
                a["first_ts"] = a["first_ts"] or ts
                a["last_ts"] = max(a["last_ts"], ts)
                if via and not a["via"]:
                    a["via"] = via
                for key in ("file_path", "description", "icon"):
                    if isinstance(inp.get(key), str) and inp[key]:
                        a[key] = inp[key]
                if isinstance(inp.get("type_url"), str):
                    a["type_url"] = canonical_url(inp["type_url"])
                if isinstance(inp.get("title"), str) and inp["title"]:
                    a["title"], a["title_source"] = inp["title"], "input"
                    self.learn_title(a["url"], inp["title"], "input")
                return
        url = ""
        if isinstance(inp.get("url"), str):
            m = ARTIFACT_URL_RE.search(inp["url"])
            url = m.group(0) if m else ""
        if not url and action == "open":
            m = OPENED_RE.match(text)
            url = m.group(1) if m else ""
        if not url and action == "read":
            m = ARTIFACT_URL_RE.search(text[:400])
            url = m.group(0) if m else ""
        if url and action in ("open", "read", "publish", "pin", "unpin"):
            a = self.art(url)
            a["touched"] += 1
            a["first_ts"] = a["first_ts"] or ts
            if a["publishes"] == 0:
                a["last_ts"] = max(a["last_ts"], ts)

    def note_links(self, text: str, ts: float) -> None:
        """Every web link the agent put in a reply, except artifacts (listed on
        their own). A markdown link keeps its text as the title."""
        titles: dict[str, str] = {}
        for m in MD_LINK_RE.finditer(text):
            titles.setdefault(clean_link(m.group(2)), m.group(1).strip())
        for m in BARE_URL_RE.finditer(text):
            url = clean_link(m.group(0))
            if not url or ARTIFACT_URL_RE.match(url):
                continue
            cur = self.links.get(url)
            if cur is None:
                self.links[url] = {"url": url, "title": titles.get(url, ""), "first_ts": ts, "last_ts": ts, "count": 1}
            else:
                cur["count"] += 1
                cur["last_ts"] = max(cur["last_ts"], ts)
                cur["first_ts"] = min(cur["first_ts"], ts) if cur["first_ts"] else ts
                cur["title"] = cur["title"] or titles.get(url, "")

    def note_mentions(self, text: str, ts: float, summary: bool, reply: bool) -> None:
        """Artifact URLs in prose. In a reply the person reads (main thread), the
        agent is pointing them at it: that is a link, not a glance."""
        titles: dict[str, str] = {}
        for rx in MENTION_RES:
            for m in rx.finditer(text):
                titles.setdefault(canonical_url(m.group(2)), m.group(1).strip())
        for m in ARTIFACT_URL_RE.finditer(text):
            url = canonical_url(m.group(0))
            cur = self.mentions.setdefault(url, {"title": "", "ts": ts, "summary": False, "reply": False})
            cur["title"] = cur["title"] or titles.get(url, "")
            cur["ts"] = max(cur["ts"], ts)
            cur["summary"] = cur["summary"] or summary
            cur["reply"] = cur["reply"] or reply
            if titles.get(url):
                self.learn_title(url, titles[url], "mention")

    def feed_transcript(self, path: str, via: str = "") -> None:
        """One transcript: the session's own, or a subagent's (via = "subagent")."""
        pending: dict[str, tuple[dict, float]] = {}
        try:
            fh = open(path, "rb")
        except OSError:
            return
        self.sources["subagents" if via else "transcript"] += 1
        with fh:
            for raw in fh:
                if b"rtifact" not in raw and b'"cwd"' not in raw:
                    continue
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(entry, dict):
                    continue
                if not self.cwd and not via and isinstance(entry.get("cwd"), str):
                    self.cwd = entry["cwd"]
                if entry.get("type") == "artifact-comment-monitor":
                    for uuid, info in (entry.get("artifacts") or {}).items():
                        if isinstance(info, dict) and isinstance(info.get("title"), str):
                            self.monitor_titles[uuid] = info["title"]
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                ts = parse_ts(entry.get("timestamp"))
                summary = bool(entry.get("isCompactSummary"))
                main_reply = entry.get("type") == "assistant" and not entry.get("isSidechain") and not via
                if summary and isinstance(message.get("content"), str):
                    self.note_mentions(message["content"], ts, True, False)
                if not isinstance(message.get("content"), list):
                    continue
                for block in message["content"]:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str) and (summary or entry.get("type") == "assistant"):
                        self.note_mentions(block["text"], ts, summary, main_reply)
                        if main_reply:
                            self.note_links(block["text"], ts)
                    if block.get("type") == "tool_use" and block.get("name") == "Artifact":
                        inp = block.get("input")
                        tid = str(block.get("id"))
                        self.tool_ids.add(tid)
                        pending[tid] = (inp if isinstance(inp, dict) else {}, ts)
                    elif block.get("type") == "tool_result":
                        use = pending.pop(str(block.get("tool_use_id")), None)
                        if use is not None:
                            self.on_result(use[0], use[1] or ts, result_text(block).strip(), via)

    def feed_hook_events(self, path: str) -> None:
        """Hook records: only calls the transcripts did not already show."""
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            return
        with fh:
            for raw in fh:
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict) or ev.get("tool") != "Artifact":
                    continue
                self.sources["hook_events"] += 1
                tid = str(ev.get("tool_use_id") or "")
                if tid and tid in self.tool_ids:
                    continue
                self.on_result(ev.get("input") or {}, float(ev.get("ts") or 0), str(ev.get("result") or "").strip(),
                               "subagent" if ev.get("agent") else "")

    def finish(self) -> dict:
        for url, m in self.mentions.items():
            a = self.art(url)
            if a["publishes"] == 0:
                if m["summary"] and m["title"]:
                    a["summarized"] = True  # named in a compaction summary: made before the cut
                elif m["reply"]:
                    a["linked"] += 1
                else:
                    a["touched"] += 1
                a["first_ts"] = a["first_ts"] or m["ts"]
                a["last_ts"] = max(a["last_ts"], m["ts"])
            if not a["title"] and m["title"]:
                a["title"], a["title_source"] = m["title"], "mention"
        for l in self.links.values():
            l["title"] = l["title"] or link_label(l["url"])
            l["host"] = re.sub(r"^www\.", "", (re.match(r"https?://([^/]+)", l["url"]) or [None, ""])[1])
        return {
            "cwd": self.cwd,
            "artifacts": list(self.artifacts.values()),
            "monitor_titles": self.monitor_titles,
            "titles": {u: list(t) for u, t in self.titles.items()},
            "links": sorted(self.links.values(), key=lambda l: -l["last_ts"])[:LINK_MAX],
            "sources": self.sources,
        }


def session_inputs(transcript: str, session_id: str) -> list[tuple[str, str]]:
    """(kind, path) of every file that feeds one session's picture."""
    out = [("transcript", transcript)] if transcript else []
    if transcript:
        out += [("subagent", p) for p in sorted(glob.glob(os.path.join(transcript[:-len(".jsonl")], "subagents", "*.jsonl")))]
    events = os.path.join(STATE_DIR, "events", f"{session_id}.jsonl")
    if os.path.exists(events):
        out.append(("events", events))
    return out


def collect(transcript: str, session_id: str) -> dict:
    inputs = session_inputs(transcript, session_id)
    sig = []
    for _kind, path in inputs:
        try:
            st = os.stat(path)
            sig.append([path, st.st_size, st.st_mtime])
        except OSError:
            pass
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[-120:]
    cache_file = os.path.join(CACHE_DIR, f"session-{key}.json")
    cached = read_json(cache_file)
    if isinstance(cached, dict) and cached.get("sig") == sig and cached.get("parser") == PARSER_VERSION:
        return cached["data"]
    c = Collector()
    for kind, path in inputs:
        if kind == "transcript":
            c.feed_transcript(path)
        elif kind == "subagent":
            c.feed_transcript(path, via="subagent")
    for kind, path in inputs:
        if kind == "events":
            c.feed_hook_events(path)
    data = c.finish()
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"sig": sig, "parser": PARSER_VERSION, "data": data}, fh)
        os.replace(tmp, cache_file)
    except OSError:
        pass
    return data


# --------------------------------------------------------------------------- #
# Titles
# --------------------------------------------------------------------------- #


class TitleRegistry:
    """Every artifact title seen in any session, so a bare URL in one
    conversation still gets the name another conversation learned."""

    PATH = os.path.join(CACHE_DIR, "titles.json")

    def __init__(self) -> None:
        data = read_json(self.PATH)
        self.titles: dict[str, list] = data if isinstance(data, dict) else {}
        self.dirty = False

    def learn(self, url: str, title: str, source: str) -> None:
        if not title or source in ("file", "id", ""):
            return
        cur = self.titles.get(url)
        if cur is None or (TITLE_RANK.get(source, 0) >= TITLE_RANK.get(cur[1], 0) and cur[0] != title):
            self.titles[url] = [title, source]
            self.dirty = True

    def get(self, url: str):
        return self.titles.get(url)

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = self.PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.titles, fh)
            os.replace(tmp, self.PATH)
        except OSError:
            pass


def clean_link(url: str) -> str:
    """A URL as written in prose, without the punctuation that followed it."""
    url = url.rstrip(".,;:!?*_")
    while url.endswith((")", "]", "'", '"', "`", ">")) and url.count("(") < url.count(")"):
        url = url[:-1]
    url = url.rstrip(".,;:!?*_'\"`>")
    return url if re.match(r"https?://[^/\s]+", url) else ""


def link_label(url: str) -> str:
    """A short name for a bare URL: the host and the tail of its path."""
    m = re.match(r"https?://([^/?#]+)([^?#]*)", url)
    if not m:
        return url
    host = re.sub(r"^www\.", "", m.group(1))
    path = m.group(2).rstrip("/")
    gh = re.match(r"^/([^/]+)/([^/]+)/(?:pull|issues)/(\d+)", path) if host == "github.com" else None
    if gh:
        return f"{gh.group(1)}/{gh.group(2)} #{gh.group(3)}"
    if not path:
        return host
    tail = path if len(path) <= 48 else "…" + path[-47:]
    return f"{host}{tail}"


def html_title(path: str) -> str:
    if not path or not path.lower().endswith((".html", ".htm")):
        return ""
    try:
        with open(path, "rb") as fh:
            head = fh.read(65536).decode("utf-8", "replace")
    except OSError:
        return ""
    m = HTML_TITLE_RE.search(head)
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""


def desktop_titles(session_id: str) -> dict[str, tuple[str, float]]:
    """Titles the Claude desktop app recorded for a session it started."""
    out: dict[str, tuple[str, float]] = {}
    home = os.path.expanduser("~")
    bases = [
        os.path.join(home, "Library", "Application Support", "Claude", "claude-code-sessions"),
        os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "Claude", "claude-code-sessions"),
    ]
    for base in bases:
        for path in glob.glob(os.path.join(base, "*", "*", "local_*.json")):
            rec = read_json(path)
            if not isinstance(rec, dict) or rec.get("cliSessionId") != session_id:
                continue
            for item in rec.get("publishedArtifacts") or []:
                if isinstance(item, dict) and isinstance(item.get("url"), str):
                    out[canonical_url(item["url"])] = (str(item.get("title") or ""), parse_ts(item.get("updatedAt")))
    return out


def resolve_titles(data: dict, desktop: dict[str, tuple[str, float]], registry: TitleRegistry) -> None:
    """Give every artifact the best title any source knows, and teach the registry."""
    learned = {u: tuple(t) for u, t in (data.get("titles") or {}).items()}
    for a in data["artifacts"]:
        url = a["url"]
        candidates = []
        if a["title_source"] == "input":
            candidates.append((a["title"], "input"))
        hit = desktop.get(url)
        if hit and hit[0]:
            candidates.append((hit[0], "desktop"))
            a["last_ts"] = max(a["last_ts"], hit[1] or 0.0)
        if url in learned:
            candidates.append(learned[url])
        page = html_title(a["file_path"])
        if page:
            candidates.append((page, "html"))
        short = url.rsplit("/", 1)[-1]
        if short in data["monitor_titles"]:
            candidates.append((data["monitor_titles"][short], "monitor"))
        if a["title_source"] == "mention" and a["title"]:
            candidates.append((a["title"], "mention"))
        known = registry.get(url)
        if known:
            candidates.append((known[0], known[1]))
        if a["file_path"]:
            candidates.append((os.path.basename(a["file_path"]), "file"))
        candidates.append((short, "id"))
        title, source = max(candidates, key=lambda c: TITLE_RANK.get(c[1], 0))
        a["title"], a["title_source"] = title, source
        registry.learn(url, title, source)


def short_path(path: str, cwd: str) -> str:
    if not path:
        return ""
    home = os.path.expanduser("~")
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1:]
    if "/scratchpad/" in path:
        return "scratchpad/" + path.split("/scratchpad/", 1)[1]
    if path.startswith(home + "/"):
        return "~/" + path[len(home) + 1:]
    return path


# --------------------------------------------------------------------------- #


def scan_terminal(address: str, detail: dict, listed: dict, registry: TitleRegistry) -> dict:
    session = claude_session_in({**detail, "address": address})
    entry = {
        "address": address,
        "title": detail.get("title") or listed.get("title") or "",
        "cwd": (detail.get("foreground") or {}).get("cwd") or detail.get("cwd") or "",
        "status": (detail.get("status") or {}).get("state") if isinstance(detail.get("status"), dict) else listed.get("status"),
        "session": None, "session_name": "", "transcript": "", "profile": "",
        "published": [], "linked": [], "touched": [], "links": [], "sources": {},
        "watch": [os.path.join(STATE_DIR, "terminals", f"{address}.json")],
    }
    if not session:
        return entry
    sid = session["sessionId"]
    entry.update(session=sid, session_name=session.get("name") or "", profile=session.get("profile") or "")
    path = transcript_path(session)
    entry["transcript"] = path
    # Files whose change should rescan this session (the panel watches their folders).
    entry["watch"] = [f for f in (path, path[:-len(".jsonl")] + "/" if path else "", os.path.join(STATE_DIR, "events", f"{sid}.jsonl"),
                                  os.path.join(STATE_DIR, "terminals", f"{address}.json")) if f]
    if not path and not os.path.exists(os.path.join(STATE_DIR, "events", f"{sid}.jsonl")):
        return entry
    data = collect(path, sid)
    resolve_titles(data, desktop_titles(sid), registry)
    cwd = data.get("cwd") or entry["cwd"]
    for a in data["artifacts"]:
        a = dict(a)
        a["path"] = short_path(a["file_path"], cwd)
        if a["publishes"] > 0 or a.get("summarized"):
            entry["published"].append(a)
        elif a.get("linked"):
            entry["linked"].append(a)
        else:
            entry["touched"].append(a)
    for key in ("published", "linked", "touched"):
        entry[key].sort(key=lambda a: -a["last_ts"])
    entry["links"] = data.get("links", [])
    entry["sources"] = {**data.get("sources", {}), "hook_session": bool(session.get("via_hook"))}
    return entry


def main(argv: list[str]) -> int:
    wanted: list[str] = []
    for i, arg in enumerate(argv):
        if arg == "--terminals" and i + 1 < len(argv):
            wanted = [t for t in argv[i + 1].split(",") if t]
    listing = hc("ls")
    terminals = listing.get("terminals", []) if isinstance(listing, dict) else (listing or [])
    live = {t.get("address"): t for t in terminals if t.get("address") and t.get("state") in (None, "live")}
    registry = TitleRegistry()
    os.makedirs(STATE_DIR, exist_ok=True)
    watch_dirs = [d for d in (os.path.join(b, "projects") for b in claude_config_dirs()) if os.path.isdir(d)] + [STATE_DIR]
    out = {"terminals": [], "errors": [], "watch_dirs": watch_dirs}
    shown = [a for a in live if not wanted or a in wanted]
    details: dict[str, dict] = {}
    for address in shown:
        d = hc("get", address)
        if isinstance(d, dict):
            details[address] = d
        else:
            out["errors"].append(f"{address}: hc get failed")
    # Children: agents started from a shown agent with `hc run` (hsub subagents and
    # the like) are their own terminals; their publishes roll up under the parent.
    children: dict[str, list[str]] = {}
    for address in [a for a in live if a not in details and live[a].get("agent")][:CHILD_MAX]:
        d = hc("get", address)
        if isinstance(d, dict) and d.get("parent") in details:
            details[address] = d
            children.setdefault(d["parent"], []).append(address)
    scanned = {a: scan_terminal(a, d, live.get(a, {}), registry) for a, d in details.items()}
    for address in shown:
        if address not in scanned:
            continue
        entry = scanned[address]
        have = {a["url"] for a in entry["published"]}
        for child in children.get(address, []):
            c = scanned[child]
            name = details[child].get("name") or c.get("session_name") or child
            for a in c["published"]:
                if a["url"] not in have:
                    entry["published"].append({**a, "via": name})
                    have.add(a["url"])
        entry["published"].sort(key=lambda a: -a["last_ts"])
        entry["linked"] = [a for a in entry["linked"] if a["url"] not in have]
        entry["touched"] = [a for a in entry["touched"] if a["url"] not in have]
        entry["children"] = children.get(address, [])
        out["terminals"].append(entry)
    registry.save()
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

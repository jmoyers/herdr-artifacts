#!/usr/bin/env python3
"""Artifacts scanner for the Herdr app's `artifacts` mod.

For each herdr-core terminal given (or every live one), find the Claude Code
process running in it, its session, and the session transcript, then list the
claude.ai artifacts that conversation published. Prints one JSON object.

    python3 artifacts.py [--terminals t1,t2] [--all-touched]

The terminal -> session mapping needs no hook: `hc get <t>` names the
foreground process, and Claude Code writes `~/.claude/sessions/<pid>.json`
with the session id. Transcripts live at
`~/.claude/projects/<cwd>/<session>.jsonl`; every Artifact tool call is in
there with its result ("Published ... at <url> (Version N ...)").

Parsed transcripts are cached under ~/.cache/herdr-artifacts keyed by file
size and mtime, so a refresh costs one `hc get` per terminal plus a stat.
Standard library only.
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
HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
# A title next to a link in prose: [Title](url), "Title" (url), **Title** (url), Title artifact (url).
MENTION_RES = [
    re.compile(r"\[([^\]\n]{2,120})\]\((https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+)[^)]*\)"),
    re.compile(r"[\"“]([^\"”\n]{2,120})[\"”][^()\n]{0,40}\((?:[^()\n]{0,60}, )?(https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+)[^)]*\)"),
    re.compile(r"\*\*([^*\n]{2,120})\*\*[^()\n]{0,40}\((?:[^()\n]{0,60}, )?(https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+)[^)]*\)"),
]
PARSER_VERSION = 5
# Links the agent wrote in its replies: markdown links first, then bare URLs.
MD_LINK_RE = re.compile(r"\[([^\]\n]{1,160})\]\((https?://[^\s)]+)\)")
BARE_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`]+")
LINK_MAX = 80

CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "herdr-artifacts")


def claude_config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


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
    path = os.path.join(claude_config_dir(), "sessions", f"{pid}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("sessionId"), str) else None


def child_pids(pid: int) -> list[int]:
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=3, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def claude_session_in(terminal: dict) -> dict | None:
    """The Claude Code session running in a terminal: the foreground process,
    or one of its children (claude started through a wrapper or `npx`)."""
    fg = terminal.get("foreground") or {}
    pids = []
    if isinstance(fg.get("pid"), int):
        pids.append(fg["pid"])
    if isinstance(terminal.get("pid"), int):
        pids.append(terminal["pid"])
    seen = set()
    queue = list(pids)
    depth = 0
    while queue and depth < 4:
        depth += 1
        next_queue = []
        for pid in queue:
            if pid in seen:
                continue
            seen.add(pid)
            session = session_for_pid(pid)
            if session:
                return session
            next_queue.extend(child_pids(pid))
        queue = next_queue
    return None


def transcript_path(session_id: str) -> str:
    if not session_id or "/" in session_id or session_id.startswith("."):
        return ""
    matches = glob.glob(os.path.join(claude_config_dir(), "projects", "*", f"{session_id}.jsonl"))
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0] if matches else ""


# --------------------------------------------------------------------------- #
# Transcript -> artifacts
# --------------------------------------------------------------------------- #


def parse_transcript(path: str) -> dict:
    pending: dict[str, tuple[dict, float]] = {}
    artifacts: dict[str, dict] = {}
    monitor_titles: dict[str, str] = {}
    mentions: dict[str, dict] = {}  # url -> {title, ts, summary: bool}
    links: dict[str, dict] = {}     # url -> {url, title, first_ts, last_ts, count}
    cwd = ""

    def note_links(text: str, ts: float) -> None:
        """Every web link the agent put in a reply, except artifacts (listed
        on their own). A markdown link keeps its text as the title."""
        titles: dict[str, str] = {}
        for m in MD_LINK_RE.finditer(text):
            titles.setdefault(clean_link(m.group(2)), m.group(1).strip())
        for m in BARE_URL_RE.finditer(text):
            url = clean_link(m.group(0))
            if not url or ARTIFACT_URL_RE.match(url):
                continue
            cur = links.get(url)
            if cur is None:
                links[url] = {"url": url, "title": titles.get(url, ""), "first_ts": ts, "last_ts": ts, "count": 1}
            else:
                cur["count"] += 1
                cur["last_ts"] = max(cur["last_ts"], ts)
                cur["first_ts"] = min(cur["first_ts"], ts) if cur["first_ts"] else ts
                cur["title"] = cur["title"] or titles.get(url, "")

    def note_mentions(text: str, ts: float, summary: bool) -> None:
        titles: dict[str, str] = {}
        for rx in MENTION_RES:
            for m in rx.finditer(text):
                titles.setdefault(canonical_url(m.group(2)), m.group(1).strip())
        for m in ARTIFACT_URL_RE.finditer(text):
            url = canonical_url(m.group(0))
            cur = mentions.get(url)
            if cur is None:
                mentions[url] = {"title": titles.get(url, ""), "ts": ts, "summary": summary}
            else:
                cur["title"] = cur["title"] or titles.get(url, "")
                cur["ts"] = max(cur["ts"], ts)
                cur["summary"] = cur["summary"] or summary

    def art(url: str) -> dict:
        url = canonical_url(url)
        a = artifacts.get(url)
        if a is None:
            a = artifacts[url] = {
                "url": url, "title": "", "title_source": "", "file_path": "", "description": "", "icon": "",
                "type_url": "", "publishes": 0, "version": 0, "first_ts": 0.0, "last_ts": 0.0, "touched": 0,
            }
        return a

    def on_result(inp: dict, ts: float, text: str) -> None:
        action = inp.get("action") or "publish"
        if action == "publish" and not inp.get("asset"):
            m = PUBLISHED_RE.match(text)
            if m:
                a = art(m.group(1))
                a["publishes"] += 1
                vm = VERSION_RE.search(text)
                a["version"] = max(a["version"], int(vm.group(1)) if vm else a["publishes"])
                a["first_ts"] = a["first_ts"] or ts
                a["last_ts"] = max(a["last_ts"], ts)
                for key in ("file_path", "description", "icon"):
                    if isinstance(inp.get(key), str) and inp[key]:
                        a[key] = inp[key]
                if isinstance(inp.get("type_url"), str):
                    a["type_url"] = canonical_url(inp["type_url"])
                if isinstance(inp.get("title"), str) and inp["title"]:
                    a["title"], a["title_source"] = inp["title"], "input"
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
        if url and action in ("open", "read", "list", "publish", "pin", "unpin"):
            a = art(url)
            a["touched"] += 1
            a["first_ts"] = a["first_ts"] or ts
            if a["publishes"] == 0:
                a["last_ts"] = max(a["last_ts"], ts)

    with open(path, "rb") as fh:
        for raw in fh:
            if b"Artifact" not in raw and b"artifact" not in raw and b'"cwd"' not in raw:
                continue
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(entry, dict):
                continue
            if not cwd and isinstance(entry.get("cwd"), str):
                cwd = entry["cwd"]
            if entry.get("type") == "artifact-comment-monitor":
                for uuid, info in (entry.get("artifacts") or {}).items():
                    if isinstance(info, dict) and isinstance(info.get("title"), str):
                        monitor_titles[uuid] = info["title"]
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            ts = parse_ts(entry.get("timestamp"))
            # The conversation's own prose: a compaction summary names what was built
            # before the record was cut; an assistant message names what it just made.
            summary = bool(entry.get("isCompactSummary"))
            if summary and isinstance(message.get("content"), str):
                note_mentions(message["content"], ts, True)
            if not isinstance(message.get("content"), list):
                continue
            for block in message["content"]:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str) and (summary or entry.get("type") == "assistant"):
                    note_mentions(block["text"], ts, summary)
                    if entry.get("type") == "assistant" and not entry.get("isSidechain"):
                        note_links(block["text"], ts)
                if block.get("type") == "tool_use" and block.get("name") == "Artifact":
                    inp = block.get("input")
                    pending[str(block.get("id"))] = (inp if isinstance(inp, dict) else {}, ts)
                elif block.get("type") == "tool_result":
                    use = pending.pop(str(block.get("tool_use_id")), None)
                    if use is not None:
                        on_result(use[0], use[1] or ts, result_text(block).strip())
    # Artifacts only known from prose: named in a compaction summary, they were
    # made before the transcript was cut ("summarized"); named only in assistant
    # text, they count as seen.
    for url, m in mentions.items():
        a = art(url)
        if a["publishes"] == 0:
            # A summary that names the artifact ("Launch plan" (url)) is
            # describing this conversation's work; a bare URL in it was likely read.
            if m["summary"] and m["title"]:
                a["summarized"] = True
            else:
                a["touched"] += 1
            a["first_ts"] = a["first_ts"] or m["ts"]
            a["last_ts"] = max(a["last_ts"], m["ts"])
        if not a["title"] and m["title"]:
            a["title"], a["title_source"] = m["title"], "mention"
    for a in artifacts.values():
        a.setdefault("summarized", False)
    for l in links.values():
        l["title"] = l["title"] or link_label(l["url"])
        l["host"] = re.sub(r"^www\.", "", (re.match(r"https?://([^/]+)", l["url"]) or [None, ""])[1])
    link_list = sorted(links.values(), key=lambda l: -l["last_ts"])[:LINK_MAX]
    return {"cwd": cwd, "artifacts": list(artifacts.values()), "monitor_titles": monitor_titles, "links": link_list}


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


def cached_parse(path: str) -> dict:
    try:
        st = os.stat(path)
    except OSError:
        return {"cwd": "", "artifacts": [], "monitor_titles": {}, "links": []}
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", path)[-120:]
    cache_file = os.path.join(CACHE_DIR, key + ".json")
    try:
        with open(cache_file, encoding="utf-8") as fh:
            cached = json.load(fh)
        if cached.get("size") == st.st_size and cached.get("mtime") == st.st_mtime and cached.get("parser") == PARSER_VERSION:
            return cached["data"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        pass
    data = parse_transcript(path)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"size": st.st_size, "mtime": st.st_mtime, "parser": PARSER_VERSION, "data": data}, fh)
        os.replace(tmp, cache_file)
    except OSError:
        pass
    return data


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
            try:
                with open(path, encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(rec, dict) or rec.get("cliSessionId") != session_id:
                continue
            for item in rec.get("publishedArtifacts") or []:
                if isinstance(item, dict) and isinstance(item.get("url"), str):
                    out[canonical_url(item["url"])] = (str(item.get("title") or ""), parse_ts(item.get("updatedAt")))
    return out


def resolve_titles(data: dict, desktop: dict[str, tuple[str, float]]) -> None:
    for a in data["artifacts"]:
        if a["title_source"] == "input":
            continue
        hit = desktop.get(a["url"])
        if hit and hit[0]:
            a["title"], a["title_source"] = hit[0], "desktop"
            a["last_ts"] = max(a["last_ts"], hit[1] or 0.0)
            continue
        page = html_title(a["file_path"])
        if page:
            a["title"], a["title_source"] = page, "html"
            continue
        short = a["url"].rsplit("/", 1)[-1]
        if short in data["monitor_titles"]:
            a["title"], a["title_source"] = data["monitor_titles"][short], "monitor"
            continue
        if a["title_source"] == "mention" and a["title"]:
            continue
        if a["file_path"]:
            a["title"], a["title_source"] = os.path.basename(a["file_path"]), "file"
        elif not a["title"]:
            a["title"], a["title_source"] = short, "id"


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


def main(argv: list[str]) -> int:
    wanted: list[str] = []
    for i, arg in enumerate(argv):
        if arg == "--terminals" and i + 1 < len(argv):
            wanted = [t for t in argv[i + 1].split(",") if t]
    listing = hc("ls")
    terminals = listing.get("terminals", []) if isinstance(listing, dict) else (listing or [])
    out = {"terminals": [], "errors": []}
    for t in terminals:
        address = t.get("address")
        if not address or (wanted and address not in wanted):
            continue
        if t.get("state") not in (None, "live"):
            continue
        detail = hc("get", address)
        if not isinstance(detail, dict):
            out["errors"].append(f"{address}: hc get failed")
            continue
        session = claude_session_in(detail)
        entry = {
            "address": address,
            "title": detail.get("title") or t.get("title") or "",
            "cwd": (detail.get("foreground") or {}).get("cwd") or detail.get("cwd") or "",
            "status": (detail.get("status") or {}).get("state") if isinstance(detail.get("status"), dict) else t.get("status"),
            "session": None,
            "session_name": "",
            "transcript": "",
            "published": [],
            "touched": [],
            "links": [],
        }
        if session:
            sid = session["sessionId"]
            entry["session"] = sid
            entry["session_name"] = session.get("name") or ""
            path = transcript_path(sid)
            entry["transcript"] = path
            if path:
                data = cached_parse(path)
                resolve_titles(data, desktop_titles(sid))
                cwd = data.get("cwd") or entry["cwd"]
                for a in data["artifacts"]:
                    a = dict(a)
                    a["path"] = short_path(a["file_path"], cwd)
                    (entry["published"] if a["publishes"] > 0 or a.get("summarized") else entry["touched"]).append(a)
                entry["published"].sort(key=lambda a: -a["last_ts"])
                entry["touched"].sort(key=lambda a: -a["last_ts"])
                entry["links"] = data.get("links", [])
        out["terminals"].append(entry)
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

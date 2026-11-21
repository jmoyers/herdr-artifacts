#!/usr/bin/env python3
"""Add (or remove) the artifacts panel's Claude Code hook in every profile.

    python3 ~/.config/herdr-app/mods/artifacts/install.py            # add
    python3 ~/.config/herdr-app/mods/artifacts/install.py --remove   # remove
    python3 ~/.config/herdr-app/mods/artifacts/install.py --check    # show

Profiles: ~/.claude, $CLAUDE_CONFIG_DIR, and every ~/.claude-<name> (one per
account). Each settings.json gets a SessionStart hook and a PostToolUse hook
for the Artifact tools, both running hook.py from this folder. Other hooks are
left alone; running it again changes nothing. A copy of each settings.json is
kept as settings.json.bak-herdr-artifacts before the first change.
New sessions pick the hooks up; running sessions keep what they started with
(the panel still reads their transcripts).
"""

import glob
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "hook.py")
MARK = "herdr-artifacts-hook"  # in the command, so we can find our entries again
COMMAND = f'python3 -I "{HOOK}" # {MARK}'
MATCHER = "Artifact|ArtifactComments|ArtifactData"


def profiles() -> list[str]:
    dirs = [os.path.expanduser("~/.claude"), os.environ.get("CLAUDE_CONFIG_DIR") or ""]
    dirs += sorted(glob.glob(os.path.expanduser("~/.claude-*")))
    out = []
    for d in dirs:
        d = d.rstrip("/")
        if d and os.path.isdir(d) and d not in out and (os.path.exists(os.path.join(d, "settings.json")) or os.path.isdir(os.path.join(d, "projects"))):
            out.append(d)
    return out


def ours(group: dict) -> bool:
    return any(MARK in str(h.get("command", "")) for h in group.get("hooks", []) if isinstance(h, dict))


def update(settings: dict, add: bool) -> dict:
    hooks = settings.setdefault("hooks", {})
    for event, matcher in (("SessionStart", None), ("PostToolUse", MATCHER)):
        groups = [g for g in hooks.get(event, []) if isinstance(g, dict) and not ours(g)]
        if add:
            group = {"hooks": [{"type": "command", "command": COMMAND, "timeout": 5}]}
            if matcher:
                group = {"matcher": matcher, **group}
            groups.append(group)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks")
    return settings


def main(argv: list[str]) -> int:
    mode = "remove" if "--remove" in argv else "check" if "--check" in argv else "add"
    for prof in profiles():
        path = os.path.join(prof, "settings.json")
        try:
            with open(path, encoding="utf-8") as fh:
                settings = json.load(fh)
        except FileNotFoundError:
            settings = {}
        except json.JSONDecodeError as e:
            print(f"{path}: not valid JSON ({e}); left alone")
            continue
        installed = any(ours(g) for ev in ("SessionStart", "PostToolUse") for g in settings.get("hooks", {}).get(ev, []) if isinstance(g, dict))
        if mode == "check":
            print(f"{prof}: {'installed' if installed else 'not installed'}")
            continue
        before = json.dumps(settings, sort_keys=True)
        after_settings = update(json.loads(before), add=(mode == "add"))
        if json.dumps(after_settings, sort_keys=True) == before:
            print(f"{prof}: unchanged")
            continue
        if os.path.exists(path) and not os.path.exists(path + ".bak-herdr-artifacts"):
            shutil.copy2(path, path + ".bak-herdr-artifacts")
        tmp = path + ".tmp-herdr-artifacts"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(after_settings, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
        print(f"{prof}: {'added' if mode == 'add' else 'removed'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

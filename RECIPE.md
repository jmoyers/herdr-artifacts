# Artifacts

A panel beside Files and Changes that lists every claude.ai artifact the
selected Claude Code agent's conversation has published, so the links don't
scroll away in the terminal. A switch widens it to every agent in the space.

![Artifacts](preview.png)

## What you see

- A third button, **Artifacts**, at the right end of the tab bar next to
  Files and Changes, with a page icon. Clicking it shows the panel in the
  right-hand column (368 px wide, the same column Files and Changes use) and
  hides it again on a second click.
- The panel is a card: theme `background`, 1 px `border`, 10 px radius.
- Header, 42 px: "Artifacts" in semibold 13 px, a small bordered count badge
  (10 px text, `muted`), then on the right: an "N seen" toggle (only when the
  conversation read or opened artifacts it didn't publish), an **Agent /
  Space** segmented switch (10.5 px, `selected` background on the active
  side), and a refresh icon that turns while a scan runs.
- Under the header, a search box (30 px, 8 px radius, 1 px `border` that
  turns `accent` while focused, a magnifier icon, placeholder "Search
  artifacts and links", and at the right "this agent" or "whole space" when
  idle, an × to clear while searching). Typing replaces the body with
  results: a count line ("3 artifacts · 2 links · whole space · ↵ opens"),
  then 46 px two-line rows: icon (`accent` for artifacts, `muted` for links),
  title in `foreground`, and a `dim` 11 px detail line joining the tab name
  (Space mode only), the path or URL, and the description; on the right the
  tag (`v12`, "earlier", "seen", or a link's host) over the age. Every word
  of the query must match a field; artifacts rank above links, title matches
  above path, URL, description and tab matches, then newest first. Enter
  opens the selected result, up/down move, hover selects, Esc clears and
  returns focus. The box also focuses when `artifacts.search` runs or when
  shared `artifacts.focusSearch` changes (the tab bar's
  `tabs-artifacts.artifacts-search` sets it after showing the panel); when
  the box already has focus, either one flips the scope instead and keeps
  the query.
- In Space mode, a section header per agent (30 px, 8 px above all but the
  first): a chevron, a 7 px status dot in the agent's status color, the tab
  name at the rows' 13 px in medium weight and `foreground`, and a count
  pill on the right (18 px high, `raised` background, 10.5 px `muted`).
  Clicking it collapses the section; its rows are indented 20 px.
  Agent mode has no section header, just the rows.
- One row per artifact (30 px): page icon in `accent` (`dim` for "seen"
  rows), title in `foreground` (truncated), then two right-aligned columns:
  the tag (46 px: `v12` in `muted`, or "earlier" / "seen" in `dim`) and how
  long ago (30 px, 11 px `dim`: `now`, `5m`, `3h`, `2d`, else `Nov 2`).
- A **Links** section, closed by default: a label row (26 px, chevron,
  "Links" in 11 px medium `dim`, the count). Open, it lists one row per
  link the agent wrote in a reply (30 px): link icon in `muted`, the
  markdown link text or host and path in `foreground`, the host in `dim`
  when the title doesn't show it, the age, and the same hover-revealed copy
  button. Six links show; "N more" / "Show fewer" toggles the
  rest. Clicking a link row opens it in the browser. Artifact URLs never
  appear here.
- In Space view the body is two sections: every agent's artifacts first,
  grouped under tab headers, then one closed **Links** section above a
  1 px `border` rule, which opens into per-tab groups (28 px rows with a
  chevron, status dot and the tab name in 12.5 px medium `muted`), each
  collapsible, each with its link rows indented 28 px.
- Clicking an artifact or link row opens it in the browser. Hovering a row
  reveals a copy button in a reserved 20 px column at its right edge; clicking
  it copies the URL (without opening) and shows a check mark in the theme's
  `done` color for 1.5 s. Nothing expands in place.
- Empty states, in 11 px `dim`: no agent in the space; a session Herdr can't
  read; no transcript yet; nothing published yet.

## How it behaves

- Default scope is **Agent**: the selected agent's conversation only. If a
  plain shell is selected, it shows the space's first agent and says so.
  **Space** lists every agent in the space, grouped, the selected one first,
  then pinned ones, then by tab. The choice is remembered on this screen.
- A new selection or an agent's status change (a turn ending is when
  artifacts appear) rescans at once; otherwise every 4 s.
- Agent status colors follow the theme's `working`, `blocked`, `done`,
  `idle`. The panel itself doesn't change an agent's status.
- Rows open in the system browser via `cx.open_url`; the copy button uses
  `cx.write_to_clipboard`.
- `artifacts.refresh`, `artifacts.toggle-scope`, `artifacts.toggle-links`,
  `artifacts.toggle-seen`, `artifacts.search` and `artifacts.open-latest` are mod commands for `keys.json`.
  `tabs-artifacts.artifacts` toggles the panel (bound to `cmd-shift-a` in
  the recipe's install); `tabs-artifacts.artifacts-search` shows it and
  focuses the search box (`cmd-shift-f`).

## Built from

- `mods/artifacts/main.js`: a `View` with `init`, `refresh`, `row`,
  `button`, `scopeSwitch`, `render`. Imports `View, div, svg` from gpui-kit,
  `v_flex, h_flex` from gpui-base, and `spaces, agents, focused, exec,
  appHome, theme, nextCommand` from herdr. Icons: `artifact.svg`,
  `copy.svg`, `check.svg`, `search.svg`, `link.svg`, `refresh.svg` in the mod folder, plus the shipped
  `mods/files/chevron-*.svg` and `mods/workspace-chrome/spinner.svg`.
- `mods/artifacts/artifacts.py` (standard library only), run with
  `exec("python3", ["-I", "<appHome>/mods/artifacts/artifacts.py",
  "--terminals", "t1,t2"])`. For each terminal: `hc get <t> --json` →
  `foreground.pid` (then its children, 4 deep) → `~/.claude/sessions/<pid>.json`
  → `sessionId` → `~/.claude/projects/*/<sessionId>.jsonl`. It joins
  `Artifact` tool_use blocks with their tool_result by id; a result starting
  "Published … at <url>", "Updated the Artifact at <url>" or "Created a new
  Artifact at <url>" is a publish, "(Version N" its version. URLs named in
  a compaction summary count as published earlier; URLs only in assistant
  text, and reads/opens/lists, count as seen. Every other `http(s)` URL in
  an assistant text block (not sidechains, not tool results) is a link, with
  the markdown link text as its title, trailing punctuation stripped,
  GitHub pull/issue URLs named `owner/repo #N`; the newest 80 are kept. Titles: the publish's `title`,
  the Claude desktop app's record of the session, the page's `<title>`, the
  comment monitor's title, a title next to the link in prose, the file name.
  Results are cached per transcript under `~/.cache/herdr-artifacts`.
- Layout: a clone of `main` (`layout.clone`) with the `panel-body` stack
  gaining `{ "id": "artifacts", "type": "block", "content": { "mod":
  "artifacts" } }` and the `tabs` block pointing at `tabs-artifacts`.
- `mods/tabs-artifacts`: a clone of the shipped `tabs` with
  `panelButton("artifacts", "Artifacts", "mods/artifacts/artifact.svg")`
  appended after Changes, `"artifacts"` always in the enabled panel list,
  and a `nextCommand()` loop that toggles a panel by id with
  `layout.select {stack: "panel-body", child}` and `layout.show/hide {id:
  "panel"}` (see `patches/tabs.diff`).

## Watch out

- `herdr-app check` renders once with no agents: never call `cx.notify()`
  on the path `render` → `refresh` takes when there is nothing to scan.
- `readTextFile` stops at 1 MiB; transcripts are bigger, so parse them in
  the Python script, not in the mod.
- A brand-new session has no transcript until its first message, and a
  compacted or teleported one has lost its publish records: only what its
  summary names is recoverable.
- Clones of `main` and `tabs` don't follow Herdr updates.
- Version numbers come from "(Version N" with N followed by `,` or `)`;
  "version 1793114885-93b0" is a version id, not a number.
- Mod commands don't appear in the command palette (herdr-app 0.1.x), so
  the panel toggle needs a key.

## Done when

- With a Claude Code agent selected that has published, its artifacts list
  with titles, `vN` and a time; clicking a row shows its URL and path, Open
  launches the browser, Copy link puts the URL on the clipboard.
- Selecting another agent changes the list within a second.
- The Space side of the switch shows every agent in the space with its own
  section and count; collapsing a section hides its rows.
- The Artifacts button in the tab bar shows and hides the panel, as does
  `cmd-shift-a`; `cmd-shift-f` shows it with the search box focused, and
  typing a word lists matching artifacts above matching links with their
  tab, path and description.
- `herdr-app check ~/.config/herdr-app/mods/artifacts` passes and
  `herdr-app status` lists no errors.

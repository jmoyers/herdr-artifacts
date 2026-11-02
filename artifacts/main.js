// Artifacts: the claude.ai artifacts a Claude Code conversation published, as
// a panel beside Files and Changes. By default it follows the selected agent;
// the "Space" toggle in its header shows every agent in the space, grouped.
// Click a row for its URL, path and publish history, with Open and Copy link.
//
// Data comes from artifacts.py (next to this file): it maps each terminal to
// its Claude Code session through the process id, reads the session
// transcript, and returns the publishes. It runs every few seconds and on
// every agent status change (a turn ending is when artifacts appear).
import { View, div, svg } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { spaces, agents, focused, exec, appHome, theme, nextCommand } from "herdr";

const REFRESH_MS = 4000;
const SCRIPT = () => `${appHome()}/mods/artifacts/artifacts.py`;
const SCOPE_KEY = "artifacts.scope"; // "agent" | "space", remembered on this screen

function ago(ts) {
  if (!ts) return "";
  const d = Math.max(0, Date.now() / 1000 - ts);
  if (d < 60) return "now";
  if (d < 3600) return `${Math.floor(d / 60)}m`;
  if (d < 86400) return `${Math.floor(d / 3600)}h`;
  if (d < 86400 * 30) return `${Math.floor(d / 86400)}d`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function when(ts) {
  return ts ? new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
}

// The core address without a machine prefix, as hc knows it.
const local = (address) => (address.includes("/") ? address.split("/")[1] : address);

// The agents of the active space: the selected one first, then pinned, then by tab.
function spaceAgents() {
  const space = spaces().find((s) => s.active);
  const f = focused();
  const list = agents().filter((a) => !space || a.space === space.id);
  const isSelected = (a) => a.address === f?.agent || a.address === f?.terminal || a.focused;
  list.sort((a, b) => Number(isSelected(b)) - Number(isSelected(a)) || Number(b.pinned) - Number(a.pinned) || a.tab_index - b.tab_index);
  return { list, selected: list.find(isSelected) ?? null };
}

export default class Artifacts extends View {
  init(_props, cx) {
    this.byAddress = {};      // address -> scan entry
    this.loading = false;
    this.error = null;
    this.showTouched = false;
    this.open = null;         // url of the expanded row
    this.collapsed = {};      // address -> true (space view)
    this.moreLinks = {};      // address -> true: show every link, not the first few
    this.linksOpen = {};      // address -> true: Agent view's Links section expanded
    this.collapsedLinks = {}; // address -> true: a Space-view links group folded
    this.spaceLinksOpen = false;
    this.lastKey = "";
    let scope = null;
    try { scope = localStorage.getItem(SCOPE_KEY); } catch {}
    this.scope = scope === "space" ? "space" : "agent";
    this.refresh(cx);
    cx.timer.every(REFRESH_MS, (cx) => this.refresh(cx));
    // Keys bound to "artifacts.<command>" in keys.json.
    cx.spawn(async (cx) => {
      for (;;) {
        const c = await nextCommand();
        if (c === "refresh") this.refresh(cx, true);
        else if (c === "toggle-scope") this.setScope(this.scope === "agent" ? "space" : "agent", cx);
        else if (c === "toggle-seen") { this.showTouched = !this.showTouched; cx.notify(); }
        else if (c === "toggle-links") {
          if (this.scope === "space") this.spaceLinksOpen = !this.spaceLinksOpen;
          else for (const a of this.shown()) this.linksOpen[a.address] = !this.linksOpen[a.address];
          cx.notify();
        }
        else if (c === "open-latest") {
          const first = this.shown().map((a) => this.byAddress[local(a.address)]).find((e) => e?.published?.length);
          if (first) cx.open_url(first.published[0].url);
        }
      }
    });
  }

  setScope(scope, cx) {
    this.scope = scope;
    try { localStorage.setItem(SCOPE_KEY, scope); } catch {}
    this.refresh(cx, true);
    cx.notify();
  }

  // The agents this view shows: the selected one, or every agent in the space.
  shown() {
    const { list, selected } = spaceAgents();
    if (this.scope === "space") return list;
    if (selected) return [selected];
    return list.slice(0, 1); // a shell is selected: fall back to the space's first agent
  }

  refresh(cx, force = false) {
    const list = this.shown();
    const addresses = list.map((a) => local(a.address));
    if (!force && this.loading) return;
    if (!force && this.inflightAt && Date.now() - this.inflightAt < REFRESH_MS - 200) return;
    if (!addresses.length) { this.byAddress = {}; this.error = null; return; } // called from render too: no notify here
    this.loading = true; this.inflightAt = Date.now();
    cx.spawn(async (cx) => {
      try {
        const r = await exec("python3", ["-I", SCRIPT(), "--terminals", addresses.join(",")]);
        if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim().split("\n").pop() || `exit ${r.code}`);
        const data = JSON.parse(r.stdout);
        const next = { ...this.byAddress };
        for (const t of data.terminals ?? []) next[t.address] = t;
        this.byAddress = next;
        this.error = data.errors?.length ? data.errors.join("; ") : null;
      } catch (e) {
        this.error = `Scan failed: ${e}`;
      } finally {
        this.loading = false;
        cx.notify();
      }
    });
  }

  row(a, t, indent = 0) {
    const open = this.open === a.url;
    // "summarized": named in a compaction summary, so published before the
    // transcript was cut; its versions are not on record.
    const published = a.publishes > 0 || Boolean(a.summarized);
    const tag = published ? (a.version ? `v${a.version}` : "earlier") : "seen";
    return v_flex().id(`art-${a.url}`).flex_none().rounded(7).when(open, (e) => e.bg(t.selected)).hover((s) => s.bg(open ? t.selected : t.hover))
      .child(h_flex().h(30).pl(8 + indent).pr(8).gap(8).items_center().cursor_pointer()
        .on_click((_e, cx) => { this.open = open ? null : a.url; cx.notify(); })
        .child(svg("mods/artifacts/artifact.svg").size(14).flex_none().text_color(published ? t.accent : t.dim))
        .child(div().flex_1().min_w(0).truncate().text_color(published ? t.foreground : t.muted).child(a.title || a.url))
        .child(div().flex_none().w(46).text_right().text_size(11).text_color(published && a.version ? t.muted : t.dim).child(tag))
        .child(div().flex_none().w(30).text_right().text_size(11).text_color(t.dim).child(ago(a.last_ts))))
      .when(open, (e) => e.child(v_flex().pr(8).pb(8).pl(30 + indent).gap(6)
        .child(div().text_size(11).text_color(t.accent).truncate().cursor_pointer().on_click((_e, cx) => cx.open_url(a.url)).child(a.url))
        .when(Boolean(a.path), (el) => el.child(div().text_size(11).text_color(t.dim).truncate().child(a.path)))
        .child(div().text_size(11).text_color(t.dim).child(a.publishes > 0
          ? `${a.publishes} publish${a.publishes === 1 ? "" : "es"} · first ${when(a.first_ts)} · last ${when(a.last_ts)}`
          : a.summarized ? `published before the conversation was compacted · named ${when(a.last_ts)}`
          : `read or opened ${a.touched}× · ${when(a.last_ts)}`))
        .when(Boolean(a.description), (el) => el.child(div().text_size(11).text_color(t.muted).child(a.description)))
        .child(h_flex().gap(6).pt(2)
          .child(this.button(`open-${a.url}`, "open", "Open", t, (cx) => cx.open_url(a.url), true))
          .child(this.button(`copy-${a.url}`, "copy", "Copy link", t, (cx) => { cx.write_to_clipboard(a.url); this.copied = a.url; cx.notify(); cx.timer.after(1500, (cx) => { this.copied = null; cx.notify(); }); }))
          .when(this.copied === a.url, (el) => el.child(div().text_size(11).text_color(t.done ?? t.accent).child("copied"))))));
  }

  linkRow(l, t, indent) {
    return h_flex().id(`link-${l.url}`).h(30).pl(8 + indent).pr(6).gap(8).items_center().rounded(7).cursor_pointer().hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => cx.open_url(l.url))
      .child(svg("mods/artifacts/link.svg").size(13).flex_none().text_color(t.muted))
      .child(div().flex_1().min_w(0).truncate().text_color(t.foreground).child(l.title || l.url))
      .when(Boolean(l.host) && l.title !== l.host && !l.title.startsWith(l.host), (e) => e.child(div().flex_none().max_w(120).truncate().text_size(11).text_color(t.dim).child(l.host)))
      .child(div().flex_none().w(30).text_right().text_size(11).text_color(t.dim).child(ago(l.last_ts)))
      .child(div().id(`copy-link-${l.url}`).size(20).flex_none().flex().items_center().justify_center().rounded(5).text_color(t.dim).hover((s) => s.bg(t.raised).text_color(t.foreground))
        .on_click((_e, cx) => { cx.stop_propagation(); cx.write_to_clipboard(l.url); this.copied = l.url; cx.notify(); cx.timer.after(1500, (cx) => { this.copied = null; cx.notify(); }); })
        .child(this.copied === l.url ? div().text_size(10).text_color(t.done ?? t.accent).child("✓") : svg("mods/artifacts/copy.svg").size(12)));
  }

  // The link rows of one agent, with a "more" toggle past the first six.
  linkRows(agent, links, t, indent) {
    const all = Boolean(this.moreLinks[agent.address]);
    const shown = all ? links : links.slice(0, 6);
    return v_flex().flex_none().gap(1)
      .children(shown.map((l) => this.linkRow(l, t, indent)))
      .when(links.length > 6, (e) => e.child(div().id(`more-links-${agent.address}`).h(24).pl(8 + indent + 21).flex().items_center().text_size(11).text_color(t.muted).cursor_pointer().hover((s) => s.text_color(t.foreground))
        .on_click((_e, cx) => { this.moreLinks[agent.address] = !all; cx.notify(); })
        .child(all ? "Show fewer" : `${links.length - 6} more`)));
  }

  button(id, icon, label, t, onClick, primary = false) {
    return h_flex().id(id).h(24).px(8).gap(5).items_center().rounded(6).cursor_pointer().text_size(11)
      .bg(primary ? t.solid : t.raised).text_color(primary ? t.solid_foreground : t.foreground).hover((s) => s.bg(primary ? t.solid : t.hover))
      .on_click((_e, cx) => onClick(cx))
      .child(svg(`mods/artifacts/${icon}.svg`).size(12)).child(label);
  }

  // The header's two-way switch: this agent, or every agent in the space.
  scopeSwitch(t) {
    const option = (scope, label) => {
      const on = this.scope === scope;
      return div().id(`scope-${scope}`).px(7).py(2).rounded(4).cursor_pointer().text_size(10.5)
        .bg(on ? t.selected : t.background).text_color(on ? t.foreground : t.dim).hover((s) => s.bg(on ? t.selected : t.hover))
        .on_click((_e, cx) => this.setScope(scope, cx)).child(label);
    };
    return h_flex().p(2).gap(1).rounded(6).border_1().border_color(t.border).child(option("agent", "Agent")).child(option("space", "Space"));
  }

  empty(text, t) {
    return div().p(10).text_color(t.dim).text_size(12).child(text);
  }

  render(cx) {
    const t = theme();
    const { list: all, selected } = spaceAgents();
    const list = this.shown();
    const key = `${this.scope}|${list.map((a) => `${a.address}:${a.status}`).join(",")}`;
    // A new selection or a turn ending (status change) rescans at once.
    if (key !== this.lastKey) { this.lastKey = key; this.refresh(cx, true); }
    const entries = list.map((a) => ({ agent: a, scan: this.byAddress[local(a.address)] }));
    const total = entries.reduce((n, e) => n + (e.scan?.published?.length ?? 0), 0);
    const seen = entries.reduce((n, e) => n + (e.scan?.touched?.length ?? 0), 0);
    const single = this.scope === "agent";

    const header = h_flex().h(42).flex_none().px(14).gap(8).items_center().border_b(1).border_color(t.border)
      .child(div().font_semibold().child("Artifacts"))
      .child(div().px(5).py(1).rounded(4).border_1().border_color(t.border).text_size(10).text_color(t.muted).child(`${total}`))
      .child(div().flex_1())
      .when(seen > 0, (el) => el.child(div().id("toggle-seen").px(6).py(2).rounded(5).cursor_pointer().text_size(10.5).text_color(this.showTouched ? t.foreground : t.dim).bg(this.showTouched ? t.selected : t.background).hover((s) => s.bg(t.hover))
        .on_click((_e, cx) => { this.showTouched = !this.showTouched; cx.notify(); }).child(`${seen} seen`)))
      .when(all.length > 1 || this.scope === "space", (el) => el.child(this.scopeSwitch(t)))
      .child(div().id("refresh").size(20).flex().items_center().justify_center().rounded(5).cursor_pointer().text_color(t.dim).hover((s) => s.bg(t.hover))
        .on_click((_e, cx) => this.refresh(cx, true))
        .child(this.loading ? svg("mods/workspace-chrome/spinner.svg").size(12).spin(900) : svg("mods/artifacts/refresh.svg").size(12)));

    const agentLine = (agent, scan, folded, first) => h_flex().id(`agent-${agent.address}`).h(30).when(!first, (e) => e.mt(8)).px(8).gap(8).items_center().rounded(7)
      .cursor_pointer().hover((s) => s.bg(t.hover)).on_click((_e, cx) => { this.collapsed[agent.address] = !folded; cx.notify(); })
      .child(svg(`mods/files/chevron-${folded ? "right" : "down"}.svg`).size(12).flex_none().text_color(t.dim))
      .child(div().size(7).rounded(4).flex_none().bg(t[agent.status] ?? t.idle))
      .child(div().flex_1().min_w(0).truncate().font_medium().text_color(t.foreground).child(agent.tab_name || agent.title || scan?.session_name || agent.address))
      .child(scan && !scan.session
        ? div().flex_none().text_size(11).text_color(t.dim).child("no session")
        : div().flex_none().h(18).px(7).rounded(9).bg(t.raised).flex().items_center().text_size(10.5).font_medium().text_color(t.muted).child(scan ? `${scan.published.length}` : "…"));

    const note = (text, pl) => div().pl(pl).py(4).text_size(11.5).text_color(t.dim).child(text);
    const emptyFor = (scan, items, pl) => {
      if (scan && !scan.session) return note("Not a Claude Code session Herdr can read yet.", pl);
      if (scan?.session && !scan.transcript) return note("No transcript yet; it appears after the first message.", pl);
      if (scan?.transcript && !items.length) return note("Nothing published in this conversation yet.", pl);
      return null;
    };
    // A collapsible section label: "Links  10" with a chevron. Links start closed in
    // both views; artifacts are the point of the panel and stay open.
    const sectionLabel = (id, label, count, open, onToggle, pl = 8) => h_flex().id(id).h(26).pl(pl).pr(8).gap(6).items_center().rounded(6).cursor_pointer().hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => onToggle(cx))
      .child(svg(`mods/files/chevron-${open ? "down" : "right"}.svg`).size(12).flex_none().text_color(t.dim))
      .child(div().text_size(11).font_medium().text_color(t.dim).child(label))
      .child(div().text_size(11).text_color(t.dim).child(`${count}`));

    const artifactItems = (scan) => [...(scan?.published ?? []), ...(this.showTouched ? scan?.touched ?? [] : [])];
    const totalLinks = entries.reduce((n, e) => n + (e.scan?.links?.length ?? 0), 0);

    let children;
    if (single) {
      const { agent, scan } = entries[0] ?? {};
      const items = artifactItems(scan);
      const linksOpen = Boolean(agent && this.linksOpen[agent.address]);
      children = [
        agent ? emptyFor(scan, items, 8) : null,
        ...items.map((a) => this.row(a, t, 0)),
        agent && scan?.links?.length
          ? v_flex().flex_none().gap(1).mt(items.length ? 8 : 2)
              .child(sectionLabel(`links-${agent.address}`, "Links", scan.links.length, linksOpen, (cx) => { this.linksOpen[agent.address] = !linksOpen; cx.notify(); }))
              .when(linksOpen, (e) => e.child(this.linkRows(agent, scan.links, t, 0)))
          : null,
      ].filter(Boolean);
    } else {
      // Space view: every agent's artifacts first, then every agent's links, each
      // grouped by tab. Artifacts are the finished work; links are the trail.
      const artifactGroups = entries.map(({ agent, scan }, i) => {
        const items = artifactItems(scan);
        const folded = Boolean(this.collapsed[agent.address]);
        return v_flex().flex_none().gap(1)
          .child(agentLine(agent, scan, folded, i === 0))
          .when(!folded, (el) => el.child(emptyFor(scan, items, 8 + 20 + 22) ?? div()))
          .when(!folded, (el) => el.children(items.map((a) => this.row(a, t, 20))));
      });
      const withLinks = entries.filter((e) => e.scan?.links?.length);
      const linkGroups = withLinks.map(({ agent, scan }) => {
        const folded = Boolean(this.collapsedLinks[agent.address]);
        return v_flex().flex_none().gap(1)
          .child(h_flex().id(`links-agent-${agent.address}`).h(28).pl(16).pr(8).gap(8).items_center().rounded(7).cursor_pointer().hover((s) => s.bg(t.hover))
            .on_click((_e, cx) => { this.collapsedLinks[agent.address] = !folded; cx.notify(); })
            .child(svg(`mods/files/chevron-${folded ? "right" : "down"}.svg`).size(12).flex_none().text_color(t.dim))
            .child(div().size(7).rounded(4).flex_none().bg(t[agent.status] ?? t.idle))
            .child(div().flex_1().min_w(0).truncate().text_size(12.5).font_medium().text_color(t.muted).child(agent.tab_name || agent.title || scan.session_name || agent.address))
            .child(div().flex_none().text_size(11).text_color(t.dim).child(`${scan.links.length}`)))
          .when(!folded, (el) => el.child(this.linkRows(agent, scan.links, t, 28)));
      });
      children = [
        ...artifactGroups,
        totalLinks
          ? v_flex().flex_none().gap(1).mt(12).pt(8).border_t(1).border_color(t.border)
              .child(sectionLabel("links-space", "Links", totalLinks, this.spaceLinksOpen, (cx) => { this.spaceLinksOpen = !this.spaceLinksOpen; cx.notify(); }))
              .when(this.spaceLinksOpen, (e) => e.children(linkGroups))
          : null,
      ].filter(Boolean);
    }

    const body = v_flex().id("artifacts-list").flex_1().min_h(0).overflow_y_scrollbar().p(6).gap(2)
      .when(!all.length, (el) => el.child(this.empty("No Claude Code agent in this space yet. Start one and its artifacts appear here as it publishes them.", t)))
      .when(Boolean(this.error), (el) => el.child(div().p(10).text_color(t.ansi1).text_size(11).child(this.error)))
      .when(single && all.length > 0 && !selected, (el) => el.child(div().px(10).pt(4).pb(2).text_size(11).text_color(t.dim).child("Select an agent to see its artifacts; showing the space's first one.")))
      .children(children);

    return v_flex().id("artifacts").size_full().min_h(0).rounded(10).overflow_hidden().bg(t.background).border_1().border_color(t.border).text_color(t.foreground).text_size(13)
      .child(header).child(body);
  }
}

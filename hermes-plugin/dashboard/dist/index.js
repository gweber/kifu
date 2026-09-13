/**
 * kifu — Hermes dashboard plugin: the ideas left behind in your Claude Code sessions.
 *
 * Plain IIFE, no build step. React and every UI primitive come from window.__HERMES_PLUGIN_SDK__.
 * All data comes from /api/plugins/kifu/, which forwards to the kifu service.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useState, useEffect, useCallback, useRef } = SDK.hooks;
  const C = SDK.components;
  const h = React.createElement;

  const API = "/api/plugins/kifu";

  function useAsync(fn, deps) {
    const [state, setState] = useState({ loading: true, error: null, data: null });
    const run = useCallback(() => {
      let alive = true;
      setState((s) => ({ ...s, loading: true }));
      fn().then(
        (data) => alive && setState({ loading: false, error: null, data }),
        (err) => alive && setState({ loading: false, error: String((err && err.message) || err), data: null }),
      );
      return () => { alive = false; };
    }, deps); // eslint-disable-line react-hooks/exhaustive-deps
    useEffect(run, [run]);
    return [state, run];
  }

  const send = (method, path, body) => SDK.fetchJSON(`${API}${path}`, {
    method, headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });

  function Note({ message, tone }) {
    if (!message) return null;
    const cls = tone === "error" ? "border-destructive text-destructive" : "border-border text-muted-foreground";
    return h("div", { className: `mb-4 rounded-md border p-3 text-sm whitespace-pre-wrap ${cls}` }, message);
  }

  function Muted({ children, className }) {
    return h("span", { className: `text-xs text-muted-foreground ${className || ""}` }, children);
  }

  const STATUS_VARIANT = { started: "default", proposed: "secondary", parked: "outline" };

  function CopyCommand({ command }) {
    const [copied, setCopied] = useState(false);
    if (!command) return null;
    const copy = () => {
      if (navigator.clipboard) navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    };
    return h("div", { className: "mt-1 flex items-center gap-2" },
      h("code", { className: "rounded bg-muted px-2 py-0.5 text-xs text-foreground break-all" }, command),
      h(C.Button, { size: "sm", variant: "ghost", onClick: copy }, copied ? "Copied" : "Copy"));
  }

  // ── one idea ────────────────────────────────────────────────────────
  function Trail({ anchor }) {
    const [{ loading, error, data }] = useAsync(() => SDK.fetchJSON(`${API}/lines/${encodeURIComponent(anchor)}`), [anchor]);
    if (loading) return h(Muted, null, "Loading the trail…");
    if (error) return h(Note, { message: error, tone: "error" });
    return h("div", { className: "mt-3 border-t pt-3 space-y-3" },
      data.summary && h("p", { className: "text-sm" }, data.summary),
      data.next && h("p", { className: "text-sm" }, h("strong", null, "Next: "), data.next),
      data.loose.length > 3 && h("ul", { className: "list-disc pl-5 text-sm" },
        data.loose.slice(3).map((x, i) => h("li", { key: i }, x))),
      h("div", { className: "space-y-3" }, data.threads.map((t, i) => h("div", { key: i, className: "text-sm" },
        h("div", null, h(Muted, null, `${t.first.slice(0, 10)} · ${t.project} · ${t.status}`)),
        h("div", { className: "font-medium" }, t.title),
        t.quote && h("div", { className: "italic text-muted-foreground" }, `“${t.quote}”`),
        h(CopyCommand, { command: t.resume })))));
  }

  // What git says happened after the idea went quiet (kifu verify).
  function Activity({ activity }) {
    if (!activity) return null;
    const lc = activity.latest_commit;
    const subject = lc && lc.subject.length > 100 ? lc.subject.slice(0, 97) + "…" : lc && lc.subject;
    const n = `${activity.commits_since}${activity.commits_capped ? "+" : ""}`;
    const text = activity.commits_since
      ? `${n} commit${activity.commits_since > 1 ? "s" : ""} touched its files since it went quiet` +
        (lc ? ` · latest ${lc.date.slice(0, 10)}: “${subject}”` : "")
      : "No commits touched its files since it went quiet";
    return h("div", { className: "mt-2 rounded-md border px-3 py-2 text-xs" },
      activity.likely_done && h(C.Badge, { variant: "secondary", className: "mr-2" }, "looks done"),
      text,
      activity.missing_files ? ` · ${activity.missing_files} of its files are gone` : "",
      activity.note && h("div", { className: "mt-1 text-muted-foreground" }, activity.note));
  }

  function IdeaCard({ idea, onMarked }) {
    const [open, setOpen] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState(null);
    const mark = (state) => {
      setBusy(true);
      setError(null);
      send("PUT", `/lines/${encodeURIComponent(idea.anchor)}/mark`, { state }).then(
        () => { setBusy(false); onMarked(idea, state); },
        (err) => { setBusy(false); setError(String((err && err.message) || err)); });
    };
    const meta = [idea.project, `${idea.sessions} session${idea.sessions === 1 ? "" : "s"}`,
      `quiet ${idea.quiet_days} day${idea.quiet_days === 1 ? "" : "s"}`, `since ${idea.first}`];
    return h(C.Card, { className: idea.mark ? "opacity-60" : "" },
      h(C.CardContent, { className: "p-4" },
        h("div", { className: "flex flex-wrap items-start gap-2" },
          h("div", { className: "flex-1 min-w-0" },
            h("div", { className: "font-semibold" }, idea.title),
            h("div", { className: "mt-0.5 flex flex-wrap items-center gap-2" },
              h(C.Badge, { variant: STATUS_VARIANT[idea.status] || "outline" }, idea.mark || idea.status),
              h(Muted, null, meta.join(" · ")))),
          h("div", { className: "flex gap-1" },
            idea.mark
              ? h(C.Button, { size: "sm", variant: "ghost", disabled: busy, onClick: () => mark("open") }, "Undo")
              : [h(C.Button, { key: "d", size: "sm", variant: "outline", disabled: busy, onClick: () => mark("done") }, "Done"),
                 h(C.Button, { key: "x", size: "sm", variant: "ghost", disabled: busy, onClick: () => mark("dismissed") }, "Dismiss")])),
        error && h(Note, { message: error, tone: "error" }),
        idea.verdict && h("p", { className: "mt-2 text-sm" }, idea.verdict),
        h(Activity, { activity: idea.activity }),
        idea.loose_ends.length > 0 && h("ul", { className: "mt-2 list-disc pl-5 text-sm" },
          idea.loose_ends.map((x, i) => {
            const settled = idea.activity && idea.activity.settled.includes(x);
            return h("li", { key: i, className: settled ? "line-through text-muted-foreground" : "" }, x);
          })),
        h("div", { className: "mt-2" },
          h(C.Button, { size: "sm", variant: "ghost", onClick: () => setOpen(!open) },
            open ? "Hide trail" : (idea.more_loose_ends ? `Trail and ${idea.more_loose_ends} more loose ends` : "Trail"))),
        open && h(Trail, { anchor: idea.anchor })));
  }

  // ── open ideas ──────────────────────────────────────────────────────
  function Ideas({ projects }) {
    const [q, setQ] = useState("");
    const [query, setQuery] = useState("");
    const [area, setArea] = useState("");
    const [marked, setMarked] = useState(false);
    const [hidden, setHidden] = useState({});
    const timer = useRef(null);
    const onSearch = (e) => {
      const value = e.target ? e.target.value : e;
      setQ(value);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setQuery(value), 250);
    };
    const [{ loading, error, data }, reload] = useAsync(() => SDK.fetchJSON(
      `${API}/lines?q=${encodeURIComponent(query)}&area=${encodeURIComponent(area)}&marked=${marked}&limit=100`),
      [query, area, marked]);
    const onMarked = (idea, state) => {
      if ((state === "open") === marked) setHidden((x) => ({ ...x, [idea.anchor]: true }));
    };
    const items = data ? data.items.filter((i) => !hidden[i.anchor]) : [];
    return h("div", null,
      h("div", { className: "mb-4 flex flex-wrap items-center gap-2" },
        h(C.Input, { value: q, placeholder: "Search ideas, quotes, loose ends…", onChange: onSearch, className: "max-w-sm" }),
        h(C.Select, { value: area, onChange: (e) => setArea(e.target ? e.target.value : e) },
          h(C.SelectOption, { value: "" }, "All projects"),
          projects.map((p) => h(C.SelectOption, { key: p, value: p }, p))),
        h(C.Button, { size: "sm", variant: marked ? "default" : "outline", onClick: () => { setHidden({}); setMarked(!marked); } },
          marked ? "Showing done & dismissed" : "Show done & dismissed"),
        h(C.Button, { size: "sm", variant: "ghost", onClick: () => { setHidden({}); reload(); } }, "Reload")),
      error && h(Note, { message: error, tone: "error" }),
      loading && !data && h(Muted, null, "Loading…"),
      data && h("div", { className: "mb-2" }, h(Muted, null,
        `${items.length} ${marked ? "marked" : "open"} idea${items.length === 1 ? "" : "s"}${query ? ` matching “${query}”` : ""}`)),
      data && items.length === 0 && h(Note, { message: marked ? "Nothing marked yet." : "No open ideas. Either you finish everything, or kifu has not run yet." }),
      h("div", { className: "space-y-3" }, items.map((idea) => h(IdeaCard, { key: idea.anchor, idea, onMarked }))));
  }

  // ── how you work ────────────────────────────────────────────────────
  function Stat({ label, value, sub }) {
    return h(C.Card, null, h(C.CardContent, { className: "p-4" },
      h("div", { className: "text-xs text-muted-foreground" }, label),
      h("div", { className: "text-2xl font-semibold" }, value),
      sub && h("div", { className: "text-xs text-muted-foreground" }, sub)));
  }

  const num = (v) => (v === null || v === undefined ? "–" : Math.round(v * 10) / 10);

  function Habits({ overview }) {
    const s = overview.summary || {};
    const left = overview.left_behind || [];
    if (!s.focus_minutes) return h(Note, { message: "No habits yet: kifu has not analyzed any sessions." });
    return h("div", { className: "space-y-4" },
      h("div", { className: "grid gap-3", style: { gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))" } },
        h(Stat, { label: "Focus stretch", value: `${num(s.focus_minutes.recent)} min`, sub: `last 4 weeks · ${num(s.focus_minutes.first)} min in the first 4` }),
        h(Stat, { label: "Time to answer", value: `${num(s.reply_minutes.recent)} min`, sub: `last 4 weeks · ${num(s.reply_minutes.first)} min in the first 4` }),
        h(Stat, { label: "“Go on” prompts", value: `${s.go_on_pct.recent}%`, sub: `last 4 weeks · ${s.go_on_pct.first}% in the first 4` }),
        h(Stat, { label: "Questions left behind", value: `${s.questions_left_pct}%`, sub: `${s.questions_left} of ${s.questions_asked} replies ending in a question` }),
        h(Stat, { label: "Recommendation taken", value: `${s.recommendation_taken_pct}%`, sub: `${s.recommendation_taken} of ${s.recommendation_offered} · ${s.dialogs_dismissed_pct}% of dialogs dismissed` })),
      h(C.Card, null, h(C.CardHeader, null, h(C.CardTitle, null, "Questions left behind")),
        h(C.CardContent, { className: "space-y-3" },
          left.length === 0 && h(Muted, null, "None."),
          left.map((x, i) => h("div", { key: i, className: "text-sm" },
            h("div", { className: "italic" }, `“${x.question}”`),
            h(Muted, null, `${x.ts.slice(0, 10)} · ${x.outcome === "session ended" ? "the session ended" : `next: ${x.next}`}`))))),
      h(Muted, null, `Timeline, rhythm heatmap and weekly charts: kifu's own web app at ${overview.kifu_url}`));
  }

  // ── jobs ────────────────────────────────────────────────────────────
  function Jobs({ current, onDone }) {
    const [job, setJob] = useState(current);
    const [error, setError] = useState(null);
    useEffect(() => {
      if (!job || job.status !== "running") return undefined;
      const t = setInterval(() => SDK.fetchJSON(`${API}/jobs/${job.id}`).then((j) => {
        setJob(j);
        if (j.status !== "running") { clearInterval(t); if (j.status === "done") onDone(); }
      }, (err) => { clearInterval(t); setError(String((err && err.message) || err)); }), 2000);
      return () => clearInterval(t);
    }, [job && job.id, job && job.status]); // eslint-disable-line react-hooks/exhaustive-deps
    const start = (kind) => {
      setError(null);
      send("POST", "/jobs", { kind }).then((j) => setJob(j), (err) => setError(String((err && err.message) || err)));
    };
    const running = job && job.status === "running";
    const last = job && job.log && job.log.length ? job.log[job.log.length - 1] : "";
    return h("div", { className: "flex flex-wrap items-center gap-2" },
      h(C.Button, { size: "sm", variant: "outline", disabled: running, onClick: () => start("pull") }, "Pull"),
      h(C.Button, { size: "sm", variant: "outline", disabled: running, onClick: () => start("run") }, "Run"),
      job && h(Muted, null, running ? `${job.kind}: ${last || "running…"}` : `${job.kind} ${job.status}`),
      error && h(Muted, { className: "text-destructive" }, error));
  }

  // ── page ────────────────────────────────────────────────────────────
  function KifuPage() {
    const [tab, setTab] = useState("ideas");
    const [{ loading, error, data }, reload] = useAsync(() => SDK.fetchJSON(`${API}/overview`), []);
    if (loading && !data) return h("div", { className: "p-4" }, h(Muted, null, "Loading…"));
    if (error) return h("div", { className: "p-4" }, h(Note, { message: error, tone: "error" }));
    const st = data.stats;
    const projects = Array.from(new Set((data.top || []).map((i) => i.areas[0]).concat(st.areas || []))).sort();
    const TABS = [["ideas", "Open ideas"], ["habits", "How you work"]];
    return h("div", { className: "p-4" },
      h("div", { className: "mb-4 flex flex-wrap items-end justify-between gap-3" },
        h("div", null,
          h("h2", { className: "text-xl font-semibold" }, "Ideas"),
          h(Muted, null, `${st.sessions} sessions · ${st.open} open ideas · ${st.loose} loose ends` +
            (st.marked ? ` · ${st.marked} marked` : "") + (data.config && data.config.demo ? " · demo data" : ""))),
        h(Jobs, { current: data.job, onDone: reload })),
      // C.Tabs takes a render function (active, setActive); an element array blanks the page.
      h(C.Tabs, { defaultValue: tab }, (active, setActive) => {
        const current = active || tab;
        const go = (key) => { setActive(key); setTab(key); };
        return [
          h(C.TabsList, { key: "tabs" }, TABS.map(([key, label]) => h(C.TabsTrigger, {
            key, value: key, active: current === key, onClick: () => go(key) }, label))),
          h("div", { key: "panel", className: "mt-4" },
            current === "habits" ? h(Habits, { overview: data }) : h(Ideas, { projects })),
        ];
      }));
  }

  window.__HERMES_PLUGINS__.register("kifu", KifuPage);
})();

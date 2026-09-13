/**
 * Render the kifu dashboard bundle for real, off the dashboard.
 *
 * `node --check` proves only that the bundle parses. What breaks a tab is a ReferenceError on a render or
 * click path, so this mounts the page with stubbed SDK components against API responses dumped from a demo
 * store, then clicks through: both tabs, a trail, Done, the marked view, a job.
 *
 *   python tests/dump_plugin_fixtures.py /tmp/kifu-fixtures.json
 *   KIFU_FIXTURES=/tmp/kifu-fixtures.json node hermes-plugin/dashboard/render_check.js
 *
 * React and jsdom: $KIFU_NODE_MODULES, a local `npm install react react-dom jsdom`, or a Hermes install's own.
 */
const path = require("path");
const fs = require("fs");

const NM = [
  process.env.KIFU_NODE_MODULES,
  path.join(__dirname, "..", "..", "node_modules"),
  process.env.HERMES_HOME && path.join(process.env.HERMES_HOME, "hermes-agent/node_modules"),
  path.join(process.env.HOME || "", ".hermes/hermes-agent/node_modules"),
].filter(Boolean).find((d) => fs.existsSync(path.join(d, "react")) && fs.existsSync(path.join(d, "jsdom")));
if (!NM) { console.error("No react/react-dom/jsdom: `npm install --no-save react react-dom jsdom` in the repo root"); process.exit(2); }
const React = require(path.join(NM, "react"));

const FIX = process.env.KIFU_FIXTURES;
if (!FIX || !fs.existsSync(FIX)) { console.error("set KIFU_FIXTURES (tests/dump_plugin_fixtures.py writes it)"); process.exit(2); }
const fixtures = JSON.parse(fs.readFileSync(FIX, "utf8"));

// ── stub SDK ───────────────────────────────────────────────────────────
const used = new Set();
const passthrough = (name, tag = "div") => function Stub(props) {
  used.add(name);
  const { children, ...rest } = props || {};
  const safe = {};
  for (const k of ["className", "style", "value", "disabled", "placeholder"]) if (rest[k] !== undefined) safe[k] = rest[k];
  for (const k of Object.keys(rest)) if (k.startsWith("on") && typeof rest[k] === "function") safe[k] = rest[k];
  if (tag === "input" && safe.value !== undefined && !safe.onChange) safe.readOnly = true;
  return React.createElement(tag, safe, children);
};
const components = {
  Card: passthrough("Card"), CardHeader: passthrough("CardHeader"), CardTitle: passthrough("CardTitle"),
  CardContent: passthrough("CardContent"), Badge: passthrough("Badge", "span"), Button: passthrough("Button", "button"),
  Input: passthrough("Input", "input"), Select: passthrough("Select", "select"), SelectOption: passthrough("SelectOption", "option"),
  TabsList: passthrough("TabsList"), TabsTrigger: passthrough("TabsTrigger", "button"),
  Tabs: function Tabs(props) {
    used.add("Tabs");
    if (typeof props.children !== "function") throw new Error("Tabs was passed " + typeof props.children + ", not a render function");
    const [active, setActive] = React.useState(props.defaultValue);
    return React.createElement("div", null, props.children(active, setActive));
  },
};

const calls = [];
function fetchJSON(url, init) {
  const u = String(url).replace("/api/plugins/kifu", "");
  const method = (init && init.method) || "GET";
  calls.push({ url: u, method, body: init && init.body });
  const clean = u.split("?")[0];
  if (method === "PUT") {
    const anchor = decodeURIComponent(clean.split("/")[2]);
    return Promise.resolve({ ...fixtures["/lines/"][anchor], mark: { state: JSON.parse(init.body).state } });
  }
  if (method === "POST" && clean === "/jobs") return Promise.resolve({ id: "job1", kind: JSON.parse(init.body).kind, status: "running", log: [] });
  if (clean.startsWith("/jobs/")) return Promise.resolve({ id: "job1", kind: "pull", status: "done", log: ["pulled"] });
  if (clean === "/overview") return Promise.resolve(fixtures["/overview"]);
  if (clean === "/lines") return Promise.resolve(u.includes("marked=true") ? fixtures["/lines?marked"] : fixtures["/lines"]);
  if (clean.startsWith("/lines/")) {
    const d = fixtures["/lines/"][decodeURIComponent(clean.slice(7))];
    return d ? Promise.resolve(d) : Promise.reject(new Error("404"));
  }
  return Promise.reject(new Error("no fixture for " + u));
}

const registered = {};
global.window = {
  __HERMES_PLUGIN_SDK__: {
    React, components, fetchJSON,
    hooks: { useState: React.useState, useEffect: React.useEffect, useCallback: React.useCallback,
      useMemo: React.useMemo, useRef: React.useRef },
    utils: { cn: (...a) => a.filter(Boolean).join(" ") },
  },
  __HERMES_PLUGINS__: { register: (name, c) => { registered[name] = c; } },
};
new Function(fs.readFileSync(path.join(__dirname, "dist/index.js"), "utf8"))();
if (!registered.kifu) { console.error("FAIL: bundle registered nothing under 'kifu'"); process.exit(1); }

(async () => {
  const { JSDOM } = require(path.join(NM, "jsdom"));
  const dom = new JSDOM("<!doctype html><div id=root></div>", { pretendToBeVisual: true });
  for (const [k, v] of Object.entries({
    document: dom.window.document, navigator: dom.window.navigator, HTMLElement: dom.window.HTMLElement,
    Element: dom.window.Element, Node: dom.window.Node, IS_REACT_ACT_ENVIRONMENT: true,
  })) Object.defineProperty(global, k, { value: v, configurable: true, writable: true });
  const { createRoot } = require(path.join(NM, "react-dom/client"));
  const { act } = React;
  let failed = 0;
  const errors = [];
  const realError = console.error;
  console.error = (...a) => { const t = String(a[0] || ""); if (!/not wrapped in act|unique "key"/i.test(t)) errors.push(t.slice(0, 300)); };
  const settle = (ms = 40) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });
  const click = (el) => act(async () => { el.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true })); });
  const button = (root, re) => [...root.querySelectorAll("button")].find((b) => re.test(b.textContent));
  const check = (ok, label) => { if (ok) realError.call(console, `  ok   ${label}`); else { failed++; realError.call(console, `  FAIL ${label}`); } };

  const el = document.createElement("div");
  document.body.appendChild(el);
  const root = createRoot(el);
  try {
    await act(async () => { root.render(React.createElement(registered.kifu)); });
    await settle();
    const firstTitle = fixtures["/lines"].items[0].title;
    check(el.textContent.includes(firstTitle), "open ideas tab lists the top idea");
    check(el.querySelectorAll("button").length > 5, "cards offer Done, Dismiss and Trail");
    check(/touched its files since it went quiet/.test(el.textContent), "git activity is shown on a card");
    check(el.querySelectorAll("li.line-through").length === 1, "a settled loose end is struck through");

    const trail = button(el, /^Trail/);
    await click(trail); await settle();
    check(/resume/.test(el.textContent) || /claude --resume/.test(el.textContent), "a trail shows resume commands");

    const done = button(el, /^Done$/);
    await click(done); await settle();
    check(calls.some((c) => c.method === "PUT" && /\/mark$/.test(c.url) && /done/.test(c.body)), "Done PUTs a mark");
    check(!el.textContent.includes(firstTitle), "a marked idea leaves the open list");

    await click(button(el, /Show done & dismissed/)); await settle();
    check(calls.some((c) => /marked=true/.test(c.url)), "the marked view asks for marked ideas");
    check(!!button(el, /^Undo$/), "marked ideas offer Undo");

    await click(button(el, /How you work/)); await settle();
    check(/Focus stretch/.test(el.textContent) && /Questions left behind/.test(el.textContent), "habits tab renders");

    await click(button(el, /^Pull$/)); await settle(2200);
    check(calls.some((c) => c.method === "POST" && c.url === "/jobs"), "Pull starts a job");
    check(calls.some((c) => c.url.startsWith("/jobs/job1")), "the job is polled");
  } catch (e) {
    failed++;
    realError.call(console, `  FAIL mount: ${e.message}`);
    if (process.env.VERBOSE) realError.call(console, e.stack);
  }
  await act(async () => root.unmount());
  if (errors.length) { failed += errors.length; errors.slice(0, 5).forEach((e) => realError.call(console, "  console.error: " + e)); }
  realError.call(console, `\ncomponents used: ${[...used].sort().join(", ")}`);
  realError.call(console, failed ? `\n${failed} FAILED` : "\nall render checks passed");
  process.exit(failed ? 1 : 0);
})();

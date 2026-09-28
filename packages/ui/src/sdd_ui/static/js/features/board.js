/* Board: the project's tasks by what they need, plus the plans tree.
 *
 * Columns follow the server's attention reason: Queue (paused, waiting),
 * In progress, Needs you (answers, blockers) and Done. Every card says why it
 * is (not) moving and offers the one action that moves it. Dragging between
 * Queue and In progress pauses or resumes; nothing can be dragged into Done. */

import * as api from "../core/api.js";
import { h, memo, replace } from "../core/dom.js";
import { money, t } from "../core/i18n.js";
import { recall, remember } from "../core/storage.js";
import {
  childrenOf,
  hasScope,
  kindOf,
  KINDS,
  laneOf,
  meta,
  refresh,
  runnerOf,
  runs,
  stepOf,
  store,
  titleOf,
} from "../core/store.js";
import { attempt } from "../ui/toast.js";
import { primaryAction } from "./commands.js";
import { welcome } from "./onboarding.js";
import { openTask, registerView } from "./shell.js";
import {
  attentionText,
  KIND_GLYPH,
  kindLabel,
  stepName,
} from "./vocabulary.js";

const LANES = ["queue", "running", "needs", "done"];
const LIMIT = 40;

const filters = {
  mode: recall("board-mode", "live"),
  kind: recall("kind-filter", ""),
  query: recall("task-search", ""),
  plan: recall("plan-filter", ""),
};
const openPlans = new Set(recall("open-plans", []));
let root = null;
let body = null;
let toolbar = null;
const render = memo();

function setFilter(key, value, storageKey) {
  filters[key] = value;
  remember(storageKey, value);
  draw(true);
}

function matches(run) {
  const m = meta(run);
  return (
    (!filters.plan || m.plan === filters.plan) &&
    (!filters.kind || kindOf(run) === filters.kind) &&
    [run.id, m.title, run.reason]
      .join(" ")
      .toLowerCase()
      .includes(filters.query.toLowerCase())
  );
}

/* Toolbar -------------------------------------------------------------------- */

function renderToolbar(all) {
  const counts = Object.fromEntries(
    KINDS.map((k) => [k, all.filter((r) => kindOf(r) === k).length]),
  );
  const plans = [
    ...new Set(all.map((r) => meta(r).plan).filter(Boolean)),
  ].sort();
  const segmented = h(
    "div",
    { class: "segmented", role: "group", "aria-label": t("board.view") },
    ["live", "plans"].map((mode) =>
      h(
        "button",
        {
          type: "button",
          id: mode === "live" ? "live-board" : "plans-board",
          class: filters.mode === mode ? "active" : "",
          "aria-pressed": String(filters.mode === mode),
          onclick: () => setFilter("mode", mode, "board-mode"),
        },
        t("board.mode." + mode),
      ),
    ),
  );
  const chips =
    counts.requirement + counts.ticket
      ? h(
          "div",
          { class: "chips", role: "group", "aria-label": t("board.kind") },
          ["", ...KINDS]
            .filter((k) => !k || counts[k])
            .map((k) =>
              h(
                "button",
                {
                  type: "button",
                  class: "chip" + (k ? " kind-" + k : ""),
                  "aria-pressed": String(filters.kind === k),
                  onclick: () => setFilter("kind", k, "kind-filter"),
                },
                h("span", {}, k ? t("kind.plural." + k) : t("board.all")),
                h("span", { class: "chip-count" }, k ? counts[k] : all.length),
              ),
            ),
        )
      : null;
  const search = h("input", {
    id: "task-search",
    type: "search",
    value: filters.query,
    placeholder: t("board.searchPlaceholder"),
    "aria-label": t("board.search"),
    oninput: (e) => setFilter("query", e.target.value, "task-search"),
  });
  const plan = plans.length
    ? h(
        "select",
        {
          id: "plan-filter",
          "aria-label": t("board.plan"),
          onchange: (e) => setFilter("plan", e.target.value, "plan-filter"),
        },
        h("option", { value: "" }, t("board.allPlans")),
        plans.map((p) =>
          h("option", { value: p, selected: p === filters.plan }, planTitle(p)),
        ),
      )
    : null;
  const focused = document.activeElement?.id;
  replace(
    toolbar,
    segmented,
    chips,
    h("span", { class: "spacer" }),
    search,
    plan,
  );
  if (focused === "task-search") {
    const input = toolbar.querySelector("#task-search");
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
}

function planTitle(id) {
  const plans =
    store.state.plans?.[store.project] ||
    Object.values(store.state.plans || {}).flat();
  return plans.find((p) => p.id === id)?.title || t("board.planId", { id });
}

/* Cards ---------------------------------------------------------------------- */

function card(run) {
  const kind = kindOf(run);
  const m = meta(run);
  const step = stepOf(run);
  const facts = [];
  if (kind === "requirement") {
    const children = childrenOf(run);
    if (children.length)
      facts.push(
        t("card.ticketsDone", {
          done: children.filter((c) => c.status === "accepted").length,
          count: children.length,
        }),
      );
  }
  if (kind === "ticket" && m.parent)
    facts.push(t("card.from", { id: m.parent }));
  if (run.calls) facts.push(t("card.calls", { count: run.calls }));
  const spent = store.state.usage?.per_run_usd?.[run.id];
  if (spent) facts.push("≈ " + money(spent));
  const open = () => openTask(run.id);
  const action = primaryAction(run, open);
  const node = h(
    "article",
    {
      class: `card kind-${kind} tone-${run.attention?.tone || "idle"}`,
      tabindex: "0",
      role: "button",
      "aria-label": titleOf(run),
      dataset: { run: run.id },
      draggable: run.status !== "accepted" ? "true" : false,
      onclick: open,
      onkeydown: (e) => {
        if ((e.key === "Enter" || e.key === " ") && e.target === node) {
          e.preventDefault();
          open();
        }
      },
    },
    h(
      "div",
      { class: "card-head" },
      h(
        "span",
        { class: "kind-tag" },
        KIND_GLYPH[kind] + " " + kindLabel(kind),
      ),
      m.plan ? h("span", { class: "plan-chip" }, m.plan) : null,
    ),
    h("strong", { class: "card-title" }, titleOf(run)),
    h(
      "div",
      { class: "card-step" },
      h("span", {}, stepName(step || run.step)),
      h("span", { class: "runner" }, runnerOf(step, t)),
    ),
    h("p", { class: "card-why" }, attentionText(run)),
    facts.length || action
      ? h(
          "div",
          { class: "card-foot" },
          h("small", { class: "card-facts" }, facts.join(" · ")),
          action,
        )
      : null,
  );
  node.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/plain", run.id);
    e.dataTransfer.effectAllowed = "move";
    root.classList.add("dragging");
  });
  node.addEventListener("dragend", () => root.classList.remove("dragging"));
  return node;
}

function lane(key, items) {
  const empty =
    filters.query || filters.plan || filters.kind
      ? t("board.noMatches")
      : t("board.empty." + key);
  const section = h(
    "section",
    {
      class: "lane lane-" + key,
      dataset: { lane: key },
      "aria-label": t("board.lane." + key),
    },
    h(
      "h2",
      {},
      h("span", {}, t("board.lane." + key)),
      h("span", { class: "lane-count" }, items.length),
    ),
    items.slice(0, LIMIT).map(card),
    items.length > LIMIT
      ? h(
          "p",
          { class: "hint" },
          t("board.more", { count: items.length - LIMIT }),
        )
      : null,
    items.length ? null : h("div", { class: "empty" }, empty),
  );
  if (key === "queue" || key === "running") {
    section.addEventListener("dragover", (e) => {
      e.preventDefault();
      section.classList.add("drag-over");
    });
    section.addEventListener("dragleave", () =>
      section.classList.remove("drag-over"),
    );
    section.addEventListener("drop", (e) => {
      e.preventDefault();
      section.classList.remove("drag-over");
      drop(e.dataTransfer.getData("text/plain"), key);
    });
  }
  return section;
}

function drop(id, key) {
  const run = store.state.runs.find((r) => r.id === id);
  if (!run) return;
  const wanted = key === "queue" ? "pause" : "resume";
  if (wanted === "pause" && run.paused) return;
  if (wanted === "resume" && !run.paused) return;
  attempt(
    async () => {
      if (run.status === "blocked") throw Error(t("board.dropBlocked"));
      await api.command(wanted, run);
      await refresh();
    },
    t("command.done." + wanted),
  );
}

/* Plans ---------------------------------------------------------------------- */

function plansView(items) {
  const byPlan = new Map();
  for (const run of items) {
    const plan = meta(run).plan || "";
    if (!byPlan.has(plan)) byPlan.set(plan, []);
    byPlan.get(plan).push(run);
  }
  if (!byPlan.size) return h("p", { class: "empty" }, t("plans.empty"));
  return h(
    "div",
    { class: "plans" },
    [...byPlan.entries()].map(([id, list]) => {
      const done = list.filter((r) => r.status === "accepted").length;
      const waiting = list.filter((r) => laneOf(r) === "needs").length;
      const details = h(
        "details",
        {
          class: "plan",
          open: openPlans.has(id),
          ontoggle: (e) => {
            if (e.target.open) openPlans.add(id);
            else openPlans.delete(id);
            remember("open-plans", [...openPlans]);
          },
        },
        h(
          "summary",
          {},
          h(
            "span",
            { class: "plan-name" },
            id ? planTitle(id) : t("plans.none"),
          ),
          h(
            "span",
            { class: "plan-bar" },
            h("span", {
              style: { width: Math.round((100 * done) / list.length) + "%" },
            }),
          ),
          h("span", { class: "plan-progress" }, `${done} / ${list.length}`),
          waiting
            ? h(
                "span",
                { class: "pill tone-attention" },
                t("plans.waiting", { count: waiting }),
              )
            : null,
        ),
        tree(list),
      );
      return details;
    }),
  );
}

function tree(list) {
  const ids = new Set(list.map((r) => r.id));
  const roots = list.filter((r) => !ids.has(meta(r).parent));
  const row = (run, depth) =>
    h(
      "button",
      {
        type: "button",
        class: "tree-row kind-" + kindOf(run),
        style: { "--depth": depth },
        onclick: () => openTask(run.id),
      },
      h("span", { class: "tree-glyph" }, KIND_GLYPH[kindOf(run)]),
      h("span", { class: "tree-title" }, titleOf(run)),
      h("span", { class: "tree-why" }, attentionText(run)),
    );
  const rows = [];
  const walk = (run, depth) => {
    rows.push(row(run, depth));
    for (const child of list.filter((r) => meta(r).parent === run.id))
      walk(child, depth + 1);
  };
  roots.forEach((r) => walk(r, 0));
  return h("div", { class: "plan-tree" }, rows);
}

/* View ----------------------------------------------------------------------- */

function draw(force = false) {
  if (!root) return;
  if (!hasScope()) {
    render(
      [
        "welcome",
        store.state?.profile_config,
        store.state?.projects,
        window.ffaiPreferences.language,
      ],
      () => replace(root, welcome()),
    );
    return;
  }
  const all = runs();
  const items = all.filter(matches);
  const inputs = [
    filters,
    items,
    store.state.settings,
    store.state.profile_config,
    store.project,
    window.ffaiPreferences.language,
  ];
  if (force) render(null, () => {});
  render(inputs, () => {
    if (!toolbar || !root.contains(toolbar)) {
      toolbar = h("div", { class: "board-toolbar" });
      body = h("div", { id: "board", class: "board" });
      replace(root, toolbar, body);
    }
    renderToolbar(all);
    body.classList.toggle("as-plans", filters.mode === "plans");
    if (filters.mode === "plans") replace(body, plansView(items));
    else
      replace(
        body,
        LANES.map((key) =>
          lane(
            key,
            items.filter((r) => laneOf(r) === key),
          ),
        ),
      );
  });
}

registerView({
  id: "board",
  order: 1,
  title: "nav.board",
  glyph: "▤",
  mount(container) {
    root = container;
    toolbar = null;
    draw(true);
  },
  update() {
    draw();
  },
  unmount() {
    root = null;
  },
});

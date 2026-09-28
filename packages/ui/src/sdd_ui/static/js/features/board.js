/* Board: the project's work as a tree, as status columns or as plan rows.
 *
 * "Tree" (the default) shows work as it is broken down: features hold their
 * tickets, a ticket split further holds its own, and a parent is done only when
 * its children are. Focus chips narrow it to one lane (what needs you, what
 * runs). "Kanban" shows what does work itself in columns that follow the
 * server's attention reason: Queue (paused, waiting), In progress, Needs you
 * (answers, blockers) and Done; a parent past its planning is left to its
 * tickets there. "By plan" shows one collapsible row per plan with the same
 * columns inside and the plan's own Start and Pause. Columns page by PAGE
 * cards. Dragging between Queue and In progress pauses or resumes; nothing can
 * be dragged into Done. */

import { h, memo, replace } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { remember } from "../core/storage.js";
import {
  boardFilter,
  hasScope,
  laneOf,
  meta,
  needsElsewhere,
  planTitle,
  runs,
  selectProject,
  store,
} from "../core/store.js";
import { openBulkResume, pauseAll } from "./bulk.js";
import { lanes } from "../board/cards.js";
import { plansView } from "../board/plans.js";
import { filters, LANES, MODES, openPlans, shown, view } from "../board/state.js";
import { laneCounts, treeView } from "../board/tree.js";
import { welcome } from "./onboarding.js";
import { go, registerView } from "./shell.js";

function syncBoardFilter() {
  boardFilter.kind = "";
  boardFilter.plan = filters.plan;
}
syncBoardFilter();
let root = null;
let body = null;
let toolbar = null;
let notice = null;
const render = memo();

function setFilter(key, value, storageKey) {
  filters[key] = value;
  shown.clear();
  syncBoardFilter();
  remember(storageKey, value);
  draw(true);
}

/** Show one plan's row on the board (from the overview's plan list). */
export function showPlan(id) {
  filters.mode = "plans";
  filters.plan = "";
  shown.clear();
  syncBoardFilter();
  remember("board-mode", "plans");
  remember("plan-filter", "");
  if (id) {
    openPlans.add(id);
    remember("open-plans", [...openPlans]);
  }
  go("board");
  draw(true);
  if (id)
    requestAnimationFrame(() =>
      root
        ?.querySelector(`.plan[data-plan="${CSS.escape(id)}"]`)
        ?.scrollIntoView({ block: "start" }),
    );
}

/** The plan and search filters every view applies. */
function matches(run) {
  const m = meta(run);
  return (
    (!filters.plan || m.plan === filters.plan) &&
    [run.id, m.title, run.reason]
      .join(" ")
      .toLowerCase()
      .includes(filters.query.toLowerCase())
  );
}

/** The tree's own filters on top: one lane, or everything but what is done. */
function inFocus(run) {
  const lane = laneOf(run);
  return (
    matches(run) &&
    (!filters.focus || lane === filters.focus) &&
    !(filters.hideDone && lane === "done")
  );
}

/** A parent past its planning moves through its tickets, not by itself. */
const delivers = (run) => !!run.progress && run.status === "accepted";

/* Toolbar -------------------------------------------------------------------- */

function renderToolbar(all) {
  const plans = [
    ...new Set(all.map((r) => meta(r).plan).filter(Boolean)),
  ].sort();
  const segmented = h(
    "div",
    { class: "segmented", role: "group", "aria-label": t("board.view") },
    MODES.map((mode) =>
      h(
        "button",
        {
          type: "button",
          id: mode + "-board",
          class: filters.mode === mode ? "active" : "",
          "aria-pressed": String(filters.mode === mode),
          title: t("board.modeHint." + mode),
          onclick: () => setFilter("mode", mode, "board-mode"),
        },
        t("board.mode." + mode),
      ),
    ),
  );
  const counts = laneCounts(all.filter(matches));
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const chips =
    filters.mode === "tree"
      ? h(
          "div",
          { class: "chips", role: "group", "aria-label": t("board.focus") },
          ["", ...LANES].map((lane) =>
            h(
              "button",
              {
                type: "button",
                class: "chip" + (lane ? " lane-" + lane : ""),
                "aria-pressed": String(filters.focus === lane),
                onclick: () => setFilter("focus", lane, "board-focus"),
              },
              h("span", {}, lane ? t("board.lane." + lane) : t("board.all")),
              h("span", { class: "chip-count" }, lane ? counts[lane] : total),
            ),
          ),
          h(
            "label",
            { class: "check-inline" },
            h("input", {
              type: "checkbox",
              id: "hide-done",
              checked: filters.hideDone,
              onchange: (e) =>
                setFilter("hideDone", e.target.checked, "board-hide-done"),
            }),
            t("board.hideDone"),
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
  const bulk = h(
    "div",
    { class: "toolbar" },
    h(
      "button",
      { type: "button", id: "resume-many", onclick: () => openBulkResume() },
      t("bulk.open"),
    ),
    h(
      "button",
      { type: "button", class: "ghost", onclick: () => pauseAll() },
      t("bulk.pause"),
    ),
  );
  replace(
    toolbar,
    segmented,
    chips,
    search,
    plan,
    h("span", { class: "spacer" }),
    bulk,
  );
  if (focused === "task-search") {
    const input = toolbar.querySelector("#task-search");
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
}

/** Needs-you in other projects is invisible from this one; say so and link there. */
function renderNotice() {
  const elsewhere = needsElsewhere();
  if (!elsewhere.length) return replace(notice);
  replace(
    notice,
    h(
      "p",
      { class: "attention tone-attention elsewhere" },
      h("span", {}, t("board.elsewhere")),
      elsewhere.map(({ id, name, count }) =>
        h(
          "button",
          {
            type: "button",
            class: "primary-soft",
            onclick: () => selectProject(id),
          },
          `${name || t("project.loose")} · ${count}`,
        ),
      ),
    ),
  );
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
    store.state.plans,
    needsElsewhere(),
    window.ffaiPreferences.language,
  ];
  if (force) render(null, () => {});
  render(inputs, () => {
    if (!toolbar || !root.contains(toolbar)) {
      toolbar = h("div", { class: "board-toolbar" });
      notice = h("div", { class: "board-notice" });
      body = h("div", { id: "board", class: "board" });
      replace(root, toolbar, notice, body);
    }
    renderToolbar(all);
    renderNotice();
    body.classList.toggle("as-plans", filters.mode === "plans");
    body.classList.toggle("as-tree", filters.mode === "tree");
    const working = items.filter((r) => !delivers(r));
    if (filters.mode === "plans") replace(body, plansView(working));
    else if (filters.mode === "tree")
      replace(
        body,
        treeView(
          all,
          inFocus,
          !!(filters.query || filters.plan || filters.focus || filters.hideDone),
        ),
      );
    else replace(body, lanes(working, ""));
  });
}

registerView({
  id: "board",
  order: 1,
  title: "nav.board",
  glyph: "▤",
  mount(container) {
    root = container;
    view.root = container;
    view.redraw = () => draw(true);
    toolbar = null;
    draw(true);
  },
  update() {
    draw();
  },
  unmount() {
    root = null;
    view.root = null;
  },
});

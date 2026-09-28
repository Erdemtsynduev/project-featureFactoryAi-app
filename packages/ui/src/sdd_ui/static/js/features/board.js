/* Board: the project's tasks by what they need, as status columns or plan rows.
 *
 * Columns follow the server's attention reason: Queue (paused, waiting),
 * In progress, Needs you (answers, blockers) and Done. Every card says why it
 * is (not) moving and offers the one action that moves it. "By plan" shows one
 * collapsible row per plan with the same columns inside and the plan's own
 * Start and Pause, so a plan starts as a unit. Columns page by PAGE cards.
 * Dragging between Queue and In progress pauses or resumes; nothing can be
 * dragged into Done. */

import { h, memo, replace } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { remember } from "../core/storage.js";
import {
  boardFilter,
  hasScope,
  kindOf,
  KINDS,
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
import { filters, openPlans, shown, view } from "../board/state.js";
import { welcome } from "./onboarding.js";
import { go, registerView } from "./shell.js";

function syncBoardFilter() {
  boardFilter.kind = filters.kind;
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
    counts.feature + counts.ticket
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
    if (filters.mode === "plans") replace(body, plansView(items));
    else replace(body, lanes(items, ""));
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

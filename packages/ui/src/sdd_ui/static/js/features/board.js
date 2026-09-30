/* Board: the project's work as a tree or as status columns.
 *
 * "Tree" (the default) shows work as it is broken down: a draft holds the
 * features cut from it, a feature its tickets, and a parent is done only when
 * its children are. Focus chips narrow it to one lane (what needs you, what
 * runs). "Kanban" shows what does work itself in columns that follow the
 * server's attention reason: Queue (paused, waiting), In progress, Needs you
 * (answers, blockers) and Done; a parent past its planning is left to its
 * children there. A label narrows both views, and the bulk actions with them.
 * Columns page by PAGE cards. Dragging between Queue and In progress pauses or
 * resumes; nothing can be dragged into Done. */

import { h, memo, replace } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { remember } from "../core/storage.js";
import {
  boardFilter,
  currentProject,
  hasScope,
  labelsOf,
  laneOf,
  meta,
  needsElsewhere,
  projectLabels,
  runs,
  selectProject,
  store,
} from "../core/store.js";
import { openBulkResume, pauseAll } from "./bulk.js";
import { lanes } from "../board/cards.js";
import { intakePanel } from "../board/intake.js";
import { filters, LANES, MODES, shown, view } from "../board/state.js";
import { laneCounts, treeView } from "../board/tree.js";
import { welcome } from "./onboarding.js";
import { registerView } from "./shell.js";

function syncBoardFilter() {
  boardFilter.kind = "";
  boardFilter.label = filters.label;
}
syncBoardFilter();
let root = null;
let body = null;
let toolbar = null;
let notice = null;
let intake = null;
const render = memo();

function setFilter(key, value, storageKey) {
  filters[key] = value;
  shown.clear();
  syncBoardFilter();
  remember(storageKey, value);
  draw(true);
}

/** The label and search filters every view applies. */
function matches(run) {
  return (
    (!filters.label || labelsOf(run).includes(filters.label)) &&
    [run.id, meta(run).title, run.reason]
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

/** A parent past its planning moves through its children, not by itself. */
const delivers = (run) => !!run.progress && run.status === "accepted";

/* Toolbar -------------------------------------------------------------------- */

function renderToolbar(all) {
  const labels = projectLabels();
  // A label that left the project no longer narrows anything.
  if (filters.label && !labels.includes(filters.label)) {
    filters.label = "";
    syncBoardFilter();
  }
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
  const label = labels.length
    ? h(
        "select",
        {
          id: "label-filter",
          "aria-label": t("board.label"),
          onchange: (e) => setFilter("label", e.target.value, "label-filter"),
        },
        h("option", { value: "" }, t("board.allLabels")),
        labels.map((name) =>
          h("option", { value: name, selected: name === filters.label }, name),
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
    label,
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
    currentProject(),
    needsElsewhere(),
    window.ffaiPreferences.language,
  ];
  if (force) render(null, () => {});
  render(inputs, () => {
    if (!toolbar || !root.contains(toolbar)) {
      toolbar = h("div", { class: "board-toolbar" });
      notice = h("div", { class: "board-notice" });
      intake = h("div", { class: "board-intake" });
      body = h("div", { id: "board", class: "board" });
      replace(root, toolbar, notice, intake, body);
    }
    renderToolbar(all);
    renderNotice();
    // A project is what has sources; loose tasks have nothing to import.
    replace(intake, currentProject() ? intakePanel() : null);
    body.classList.toggle("as-tree", filters.mode === "tree");
    if (filters.mode === "tree")
      replace(
        body,
        treeView(
          all,
          inFocus,
          !!(filters.query || filters.label || filters.focus || filters.hideDone),
        ),
      );
    else
      replace(
        body,
        lanes(items.filter((r) => !delivers(r))),
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

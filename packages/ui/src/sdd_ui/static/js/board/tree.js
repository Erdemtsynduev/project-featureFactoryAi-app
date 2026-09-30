/* Tree: work as it is broken down, like sub-issues. A draft row holds the
 * features cut from it, a feature its tickets and the lead's reviews of them; each
 * row says where the work stands and offers the action that moves it. A parent shows its children's
 * progress. Filters keep the ancestors of a match, dimmed, so a ticket is never
 * shown out of context. Rows that still need something start unfolded. */

import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { remember } from "../core/storage.js";
import { kindOf, laneOf, meta, stepOf, titleOf } from "../core/store.js";
import { primaryAction } from "../features/commands.js";
import { openTask } from "../features/shell.js";
import {
  attentionText,
  KIND_GLYPH,
  kindLabel,
  labelChips,
  planOrder,
  stepName,
  ticketPlace,
} from "../features/vocabulary.js";
import { closable, closeButton } from "../features/work.js";
import { progressLine } from "./cards.js";
import { folds, LANES, PAGE, shown, view } from "./state.js";

const ORDER = ["needs", "running", "queue", "done"];

/** Children by parent within `all`: a breakdown's children in its order (wave, then
 * number), other work oldest first (the order it was cut in). A review of a
 * feature's tickets sits under that feature. */
function hierarchy(all) {
  const ids = new Set(all.map((r) => r.id));
  const children = new Map();
  const roots = [];
  for (const run of [...all].reverse()) {
    const parent = meta(run).parent || meta(run).reviews;
    if (parent && ids.has(parent) && parent !== run.id) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(run);
    } else roots.push(run);
  }
  for (const list of children.values()) list.sort(planOrder);
  return { children, roots };
}

/**
 * The tree of `all` restricted to runs that `matches` accepts, with their
 * ancestors. Returns rows to draw and the number of matching top rows.
 */
export function treeView(all, matches, filtered) {
  const { children, roots } = hierarchy(all);
  const hit = new Map();
  const visit = (run, seen) => {
    if (hit.has(run.id)) return hit.get(run.id);
    if (seen.has(run.id)) return false;
    seen.add(run.id);
    let found = matches(run);
    for (const child of children.get(run.id) || [])
      found = visit(child, seen) || found;
    hit.set(run.id, found);
    return found;
  };
  const top = roots
    .filter((run) => visit(run, new Set()))
    .sort((a, b) => ORDER.indexOf(laneOf(a)) - ORDER.indexOf(laneOf(b)));
  const limit = shown.get("tree") || PAGE;
  const rows = [];
  const walk = (run, depth, seen) => {
    if (seen.has(run.id)) return;
    seen.add(run.id);
    const kids = (children.get(run.id) || []).filter((c) => hit.get(c.id));
    const open = isOpen(run, filtered);
    rows.push(row(run, depth, kids.length, open, !matches(run)));
    if (open) for (const kid of kids) walk(kid, depth + 1, seen);
  };
  for (const run of top.slice(0, limit)) walk(run, 0, new Set());
  const rest = top.length - limit;
  return h(
    "div",
    { class: "tree", role: "tree", "aria-label": t("tree.label") },
    top.length
      ? rows
      : h(
          "div",
          { class: "empty" },
          filtered ? t("board.noMatches") : t("tree.empty"),
        ),
    rest > 0
      ? h(
          "div",
          { class: "lane-more" },
          h(
            "small",
            { class: "hint" },
            t("board.shown", { shown: limit, total: top.length }),
          ),
          h(
            "button",
            {
              type: "button",
              class: "ghost",
              onclick: () => {
                shown.set("tree", limit + PAGE);
                view.redraw();
              },
            },
            t("board.showMore", { count: Math.min(PAGE, rest) }),
          ),
        )
      : null,
  );
}

/** Folded as the operator left it; otherwise open while there is work left. */
function isOpen(run, filtered) {
  if (folds.has(run.id)) return folds.get(run.id);
  return filtered || laneOf(run) !== "done";
}

function toggle(run, open) {
  folds.set(run.id, !open);
  remember("tree-folds", Object.fromEntries(folds));
  view.redraw();
}

function row(run, depth, count, open, context) {
  const kind = kindOf(run);
  const step = stepOf(run);
  const tone = run.attention?.tone || "idle";
  // Accepted work and a parent past its planning need no step: the reason says it.
  const where = run.status === "accepted" ? "" : stepName(step || run.step);
  const progress = run.progress;
  const node = h(
    "div",
    {
      class: `tree-row kind-${kind} tone-${tone}` + (context ? " context" : ""),
      role: "treeitem",
      tabindex: "0",
      "aria-level": String(depth + 1),
      "aria-expanded": count ? String(open) : null,
      "aria-label": titleOf(run),
      style: { "--depth": depth },
      dataset: { run: run.id, lane: laneOf(run) },
      onclick: () => openTask(run.id),
      onkeydown: (e) => {
        if (e.target !== node) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openTask(run.id);
        } else if (
          count &&
          ((e.key === "ArrowRight" && !open) || (e.key === "ArrowLeft" && open))
        ) {
          e.preventDefault();
          toggle(run, open);
        }
      },
    },
    count
      ? h(
          "button",
          {
            type: "button",
            class: "twisty" + (open ? " open" : ""),
            "aria-label": t(open ? "tree.collapse" : "tree.expand"),
            onclick: (e) => (e.stopPropagation(), toggle(run, open)),
          },
          "›",
        )
      : h("span", { class: "twisty-space", "aria-hidden": "true" }),
    h(
      "span",
      { class: "tree-kind", title: kindLabel(kind), "aria-hidden": "true" },
      KIND_GLYPH[kind],
    ),
    h(
      "span",
      { class: "tree-main" },
      h(
        "span",
        { class: "tree-title", title: titleOf(run) },
        titleOf(run),
        // A ticket carries its feature's labels: showing them again is noise.
        kind === "ticket" ? null : labelChips(run),
      ),
      ticketPlace(run),
      h(
        "span",
        { class: "tree-why" },
        where ? h("span", { class: "tree-step" }, where) : null,
        attentionText(run),
      ),
    ),
    run.active && step?.kind !== "human" ? progressLine(run) : null,
    progress
      ? h(
          "span",
          {
            class: "tree-progress",
            title: t("tree.progress", progress),
          },
          h(
            "span",
            { class: "plan-bar" },
            h("span", {
              style: {
                width: Math.round((100 * progress.done) / progress.total) + "%",
              },
            }),
          ),
          h("span", { class: "plan-progress" }, `${progress.done}/${progress.total}`),
        )
      : null,
    h(
      "span",
      { class: "tree-actions" },
      primaryAction(run, () => openTask(run.id)),
      run.attention?.code === "partial" && closable(run) ? closeButton(run) : null,
    ),
    h("span", { class: "pill tone-" + tone }, t("board.lane." + laneOf(run))),
  );
  return node;
}

/** Runs per lane, for the focus chips. */
export function laneCounts(list) {
  return Object.fromEntries(
    LANES.map((key) => [key, list.filter((r) => laneOf(r) === key).length]),
  );
}

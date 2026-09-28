/* By plan: one collapsible row per plan with counts, progress, outside waits,
 * Start plan and Pause, and the same columns inside; the header explains how a
 * plan becomes work and refreshes or rebuilds features from the plan files. */

import * as api from "../core/api.js";
import { h, replace } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { recall, remember } from "../core/storage.js";
import {
  laneOf,
  meta,
  outsidePrerequisites,
  planInfo,
  planTitle,
  refresh,
  store,
} from "../core/store.js";
import { confirmDialog } from "../ui/dialog.js";
import { attempt } from "../ui/toast.js";
import { openBulkResume, pauseAll } from "../features/bulk.js";
import { lanes } from "./cards.js";
import { LANES, NO_PLAN, openPlans, view } from "./state.js";

function planOrder(a, b) {
  if (a === NO_PLAN || b === NO_PLAN) return a === NO_PLAN ? 1 : -1;
  return a.localeCompare(b, undefined, { numeric: true });
}

/** One row per plan: counts per column, progress, outside waits, Start and Pause. */
export function plansView(items) {
  const byPlan = new Map();
  for (const run of items) {
    const plan = meta(run).plan || NO_PLAN;
    if (!byPlan.has(plan)) byPlan.set(plan, []);
    byPlan.get(plan).push(run);
  }
  const header = plansHeader();
  if (!byPlan.size)
    return h("div", { class: "plans" }, header, h("p", { class: "empty" }, t("plans.empty")));
  const ids = [...byPlan.keys()].sort(planOrder);
  const toggleAll = (open) => {
    for (const id of ids) open ? openPlans.add(id) : openPlans.delete(id);
    remember("open-plans", [...openPlans]);
    view.redraw();
  };
  return h(
    "div",
    { class: "plans" },
    header,
    ids.length > 1
      ? h(
          "div",
          { class: "plans-tools" },
          h(
            "button",
            { type: "button", class: "ghost", onclick: () => toggleAll(true) },
            t("plans.expandAll"),
          ),
          h(
            "button",
            { type: "button", class: "ghost", onclick: () => toggleAll(false) },
            t("plans.collapseAll"),
          ),
        )
      : null,
    ids.map((id) => planRow(id, byPlan.get(id))),
  );
}

/** What a plan is and how it becomes work, folded after the first read; the actions. */
function plansHeader() {
  const about = h(
    "details",
    {
      class: "plans-about",
      open: recall("plans-about", true),
      ontoggle: (e) => remember("plans-about", e.target.open),
    },
    h("summary", {}, t("plans.about.title")),
    h(
      "ol",
      { class: "plans-about-steps" },
      t("plans.about.steps")
        .split(" | ")
        .map((line) => h("li", {}, line)),
    ),
  );
  const action = (id, message, confirm) => {
    const button = h(
      "button",
      {
        type: "button",
        id,
        class: id === "plans-rebuild" ? "ghost" : "",
        onclick: async () => {
          if (confirm && !(await confirmDialog(confirm))) return;
          button.disabled = true;
          await attempt(async () => {
            const result = await api.post(id, { project: store.project });
            await refresh();
            return result;
          }, message);
          button.disabled = false;
        },
      },
      t(id.replace("plans-", "plans.action.")),
    );
    return button;
  };
  return h(
    "div",
    { class: "plans-header" },
    about,
    h(
      "div",
      { class: "plans-actions" },
      action("plans-sync", (r) =>
        t("plans.synced", { plans: r.plans, created: r.created.length }),
      ),
      action(
        "plans-rebuild",
        (r) =>
          t("plans.rebuilt", { removed: r.removed, created: r.created.length }),
        {
          title: t("plans.rebuildTitle"),
          message: t("plans.rebuildText"),
          confirm: t("plans.action.rebuild"),
          danger: true,
        },
      ),
    ),
  );
}

function planRow(id, list) {
  const per = Object.fromEntries(
    LANES.map((key) => [key, list.filter((r) => laneOf(r) === key).length]),
  );
  const open = list.filter((r) => r.status !== "accepted");
  const outside = outsidePrerequisites(open).length;
  const paused = open.some(
    (r) => r.paused && r.status !== "blocked",
  );
  const moving = open.some((r) => !r.paused && !r.active);
  const name = id ? planTitle(id) : t("plans.none");
  const file = id ? planInfo(id) : null;
  const progress = file?.requirements
    ? { done: file.accepted || 0, total: file.requirements }
    : { done: per.done, total: list.length };
  const stop = (fn) => (e) => {
    // Buttons live in <summary>: act without toggling the row.
    e.preventDefault();
    e.stopPropagation();
    fn();
  };
  const target = id ? { plan: id, title: name } : null;
  const details = h(
    "details",
    {
      class: "plan",
      open: openPlans.has(id),
      dataset: { plan: id },
      ontoggle: (e) => {
        if (e.target.open === openPlans.has(id)) return;
        if (e.target.open) openPlans.add(id);
        else openPlans.delete(id);
        remember("open-plans", [...openPlans]);
        if (e.target.open) fill();
      },
    },
    h(
      "summary",
      {},
      h(
        "span",
        {
          class: "plan-name",
          title: file?.path
            ? `${name}\n${t("plans.file", {
                path: file.path,
                done: file.accepted ?? 0,
                partial: file.partial ?? 0,
                open: file.open ?? 0,
              })}`
            : name,
        },
        name,
      ),
      h(
        "span",
        { class: "plan-lanes" },
        LANES.filter((key) => per[key]).map((key) =>
          h(
            "span",
            { class: "plan-count lane-" + key, title: t("board.lane." + key) },
            h("i", { "aria-hidden": "true" }),
            per[key],
          ),
        ),
      ),
      outside
        ? h(
            "span",
            { class: "pill tone-waiting" },
            t("plans.external", { count: outside }),
          )
        : null,
      // Progress is the plan file's: rows done there out of all its rows.
      h(
        "span",
        { class: "plan-bar", title: t("plans.progressHint") },
        h("span", {
          style: { width: Math.round((100 * progress.done) / progress.total) + "%" },
        }),
      ),
      h(
        "span",
        { class: "plan-progress", title: t("plans.progressHint") },
        `${progress.done} / ${progress.total}`,
      ),
      target && paused
        ? h(
            "button",
            {
              type: "button",
              class: "primary-soft plan-start",
              onclick: stop(() => openBulkResume("", target)),
            },
            t("plans.start"),
          )
        : null,
      target && moving
        ? h(
            "button",
            {
              type: "button",
              class: "ghost",
              onclick: stop(() => pauseAll(target)),
            },
            t("plans.pause"),
          )
        : null,
    ),
  );
  // Columns render only for open rows: a hundred plans stay cheap.
  const inner = h("div", { class: "board plan-board" });
  const fill = () => replace(inner, lanes(list, "plan:" + id));
  details.append(inner);
  if (openPlans.has(id)) fill();
  return details;
}

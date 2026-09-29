/* Cards and columns: one card per task with its reason and action, columns that
 * page by PAGE cards, drag between Queue and In progress to pause or resume. */

import * as api from "../core/api.js";
import { h } from "../core/dom.js";
import { formatTime, money, t } from "../core/i18n.js";
import {
  kindOf,
  laneOf,
  meta,
  planTitle,
  refresh,
  run as findRun,
  runnerOf,
  stepOf,
  store,
  titleOf,
} from "../core/store.js";
import { attempt } from "../ui/toast.js";
import { primaryAction } from "../features/commands.js";
import { openTask } from "../features/shell.js";
import {
  attentionText,
  elapsed,
  KIND_GLYPH,
  kindLabel,
  stepName,
  ticketPlace,
} from "../features/vocabulary.js";
import { filters, LANES, PAGE, shown, view } from "./state.js";

function shorten(text, size = 48) {
  return text.length > size ? text.slice(0, size - 1) + "…" : text;
}

export function card(run, inPlan) {
  const kind = kindOf(run);
  const m = meta(run);
  const step = stepOf(run);
  const facts = [];
  if (run.progress)
    facts.push(
      t("card.ticketsDone", {
        done: run.progress.done,
        count: run.progress.total,
      }),
    );
  if (m.origin) {
    const origin = findRun(m.origin);
    facts.push(
      t("card.origin", { title: shorten(origin ? titleOf(origin) : m.origin) }),
    );
  }
  if (m.parent) {
    // The parent's title, or its id without the plan prefix when it is absent.
    const parent = findRun(m.parent);
    const prefix = m.plan + "_";
    const bare =
      m.plan && m.parent.startsWith(prefix)
        ? m.parent.slice(prefix.length)
        : m.parent;
    facts.push(
      t("card.from", { title: shorten(parent ? titleOf(parent) : bare) }),
    );
  }
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
      m.plan && !inPlan
        ? h(
            "span",
            { class: "plan-chip", title: planTitle(m.plan) },
            t("card.plan", { id: m.plan }),
          )
        : null,
    ),
    h("strong", { class: "card-title", title: titleOf(run) }, titleOf(run)),
    ticketPlace(run),
    h(
      "div",
      { class: "card-step" },
      h("span", {}, stepName(step || run.step)),
      h("span", { class: "runner" }, runnerOf(step, t)),
    ),
    h("p", { class: "card-why" }, attentionText(run)),
    run.active && step?.kind !== "human" ? progressLine(run) : null,
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
    view.root.classList.add("dragging");
  });
  node.addEventListener("dragend", () => view.root.classList.remove("dragging"));
  return node;
}

const startable = (r) =>
  r.paused && r.status !== "blocked" && !r.pending_dependencies?.length;

/** The queue lists what can start first; other lanes keep their order. */
function grouped(key, items, limit) {
  if (key !== "queue") return items.slice(0, limit);
  const ready = items.filter(startable);
  const later = items.filter((r) => !startable(r));
  const out = [];
  if (ready.length)
    out.push(
      t("board.group.startable", { count: ready.length }),
      ...ready.slice(0, limit),
    );
  const room = Math.max(0, limit - ready.length);
  if (later.length && room)
    out.push(
      t("board.group.later", { count: later.length }),
      ...later.slice(0, room),
    );
  return out;
}

export function lane(key, items, scope = "") {
  const id = scope + ":" + key;
  const limit = shown.get(id) || PAGE;
  const empty =
    filters.query || filters.plan
      ? t("board.noMatches")
      : t("board.empty." + key);
  const rest = items.length - limit;
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
    grouped(key, items, limit).map((entry) =>
      typeof entry === "string"
        ? h("h3", { class: "lane-group" }, entry)
        : card(entry, !!scope),
    ),
    rest > 0
      ? h(
          "div",
          { class: "lane-more" },
          h(
            "small",
            { class: "hint" },
            t("board.shown", { shown: limit, total: items.length }),
          ),
          h(
            "button",
            {
              type: "button",
              class: "ghost",
              onclick: () => {
                shown.set(id, limit + PAGE);
                view.redraw();
              },
            },
            t("board.showMore", { count: Math.min(PAGE, rest) }),
          ),
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

export function lanes(items, scope) {
  return LANES.map((key) =>
    lane(
      key,
      items.filter((r) => laneOf(r) === key),
      scope,
    ),
  );
}

/** A live line for a running attempt: spinner, elapsed time, share of its timeout. */
export function progressLine(run) {
  const { started, deadline } = run.active;
  const share = Math.min(
    100,
    Math.round((100 * (Date.now() / 1000 - started)) / (deadline - started)),
  );
  return h(
    "div",
    {
      class: "card-progress",
      title: t("live.timeoutAt", { time: formatTime(deadline) }),
    },
    h("span", { class: "spinner", "aria-hidden": "true" }),
    h(
      "span",
      { class: "elapsed", dataset: { since: String(started) } },
      elapsed(started),
    ),
    h(
      "span",
      { class: "progress-track" },
      h("span", { style: { width: share + "%" } }),
    ),
  );
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
      // Say what happens next: resumed is not the same as running.
      const moved = findRun(id);
      return moved ? attentionText(moved) : "";
    },
    (next) => t("command.done." + wanted) + (next ? " — " + next : ""),
  );
}

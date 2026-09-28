/* Overview: the project at a glance, and the default place to land.
 *
 * One screen answers "what needs me, what is running, is anything stuck, and
 * can the queue keep going": key numbers, the Needs-you list, live running
 * steps, the team office, recent events, agent health and plan progress.
 * Every row links to its canonical home (task drawer, board, agents, log). */

import * as api from "../core/api.js";
import { h, memo, replace } from "../core/dom.js";
import { formatTime, money, t } from "../core/i18n.js";
import { recall, remember } from "../core/storage.js";
import {
  hasScope,
  kindOf,
  laneOf,
  meta,
  needsElsewhere,
  planInfo,
  planTitle,
  runnerOf,
  runs,
  selectProject,
  stepOf,
  store,
  titleOf,
} from "../core/store.js";
import { showPlan } from "./board.js";
import { openBulkResume } from "./bulk.js";
import { primaryAction } from "./commands.js";
import { welcome } from "./onboarding.js";
import { go, openTask, registerView } from "./shell.js";
import { teamWidget } from "./team.js";
import { attentionText, elapsed, stepName } from "./vocabulary.js";

const LIST = 6;
let root = null;
let team = null;
let events = [];
let eventsFor;
const render = memo();

function numbers(list) {
  const open = list.filter((r) => r.status !== "accepted");
  return {
    running: list.filter((r) => r.active && stepOf(r)?.kind !== "human").length,
    needs: list.filter((r) => laneOf(r) === "needs").length,
    startable: open.filter(
      (r) =>
        r.paused && r.status !== "blocked" && !r.pending_dependencies?.length,
    ).length,
    waiting: open.filter(
      (r) => !r.paused && !r.active && r.status !== "blocked",
    ).length,
    accepted: list.length - open.length,
    total: list.length,
    split: ["feature", "ticket"].map((kind) => {
      const of = list.filter((r) => kindOf(r) === kind);
      const done = of.filter((r) => r.status === "accepted").length;
      return `${done}/${of.length}`;
    }),
    mixed: list.some((r) => kindOf(r) === "feature") &&
      list.some((r) => kindOf(r) === "ticket"),
  };
}

function tile(label, value, hint, onclick, tone = "") {
  return h(
    onclick ? "button" : "div",
    {
      type: onclick ? "button" : undefined,
      class: `tile ${tone}`,
      onclick,
      title: hint || "",
    },
    h("small", {}, label),
    h("strong", {}, value),
    hint ? h("span", { class: "tile-hint" }, hint) : null,
  );
}

function panel(title, action, ...children) {
  return h(
    "section",
    { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", {}, title), action),
    children,
  );
}

function row(run, extra) {
  return h(
    "li",
    { class: "row tone-" + (run.attention?.tone || "idle") },
    h("span", { class: "notice-dot", "aria-hidden": "true" }),
    h(
      "div",
      { class: "row-body" },
      h(
        "button",
        {
          type: "button",
          class: "link row-title",
          onclick: () => openTask(run.id),
        },
        titleOf(run),
      ),
      h("small", { class: "row-meta" }, extra),
    ),
    primaryAction(run, () => openTask(run.id)),
  );
}

function needsPanel(list) {
  const needs = list.filter((r) => laneOf(r) === "needs");
  const elsewhere = needsElsewhere();
  return panel(
    t("overview.needs", { count: needs.length }),
    needs.length > LIST
      ? h(
          "button",
          { type: "button", class: "ghost", onclick: () => go("board") },
          t("overview.all"),
        )
      : null,
    needs.length
      ? h(
          "ul",
          { class: "rows" },
          needs.slice(0, LIST).map((r) => row(r, attentionText(r))),
        )
      : h("p", { class: "hint" }, t("notify.nothingNeeded")),
    elsewhere.length
      ? h(
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
        )
      : null,
  );
}

function runningPanel(list) {
  const running = list.filter((r) => r.active && stepOf(r)?.kind !== "human");
  const queued = list.filter(
    (r) =>
      !r.paused &&
      !r.active &&
      r.status !== "blocked" &&
      r.status !== "accepted",
  );
  return panel(
    t("overview.running", { count: running.length }),
    null,
    running.length
      ? h(
          "ul",
          { class: "rows" },
          running.map((r) =>
            h(
              "li",
              { class: "row tone-working" },
              h("span", { class: "spinner", "aria-hidden": "true" }),
              h(
                "div",
                { class: "row-body" },
                h(
                  "button",
                  {
                    type: "button",
                    class: "link row-title",
                    onclick: () => openTask(r.id),
                  },
                  titleOf(r),
                ),
                h(
                  "small",
                  { class: "row-meta" },
                  `${stepName(stepOf(r) || r.step)} · ${runnerOf(stepOf(r), t)}`,
                ),
              ),
              h(
                "span",
                {
                  class: "elapsed mono",
                  dataset: { since: String(r.active.started) },
                },
                elapsed(r.active.started),
              ),
            ),
          ),
        )
      : h(
          "p",
          { class: "hint" },
          queued.length
            ? t("overview.queuedOnly", { count: queued.length })
            : t("overview.idle"),
        ),
  );
}

function eventsPanel() {
  return panel(
    t("notify.events"),
    h(
      "button",
      { type: "button", class: "ghost", onclick: () => go("journal") },
      t("journal.open"),
    ),
    events.length
      ? h(
          "ul",
          { class: "rows" },
          events
            .slice(0, 8)
            .map((e) =>
              h(
                "li",
                { class: "row compact event" },
                h("time", {}, formatTime(e.at)),
                h(
                  "div",
                  { class: "row-body" },
                  h(
                    "strong",
                    {},
                    t("log.kind." + e.kind) === "log.kind." + e.kind
                      ? e.kind
                      : t("log.kind." + e.kind),
                  ),
                  e.run
                    ? h(
                        "button",
                        {
                          type: "button",
                          class: "link row-meta",
                          onclick: () => openTask(e.run),
                        },
                        titleOf({ id: e.run }),
                      )
                    : null,
                ),
              ),
            ),
        )
      : h("p", { class: "hint" }, t("log.empty")),
  );
}

function agentsPanel() {
  const profiles = Object.entries(store.state.profile_config?.profiles || {});
  const resting = store.state.cooldowns || {};
  const usage = store.state.usage?.by_handler || {};
  return panel(
    t("overview.agents"),
    h(
      "button",
      { type: "button", class: "ghost", onclick: () => go("agents") },
      t("overview.manage"),
    ),
    profiles.length
      ? h(
          "ul",
          { class: "rows" },
          profiles.map(([name, profile]) =>
            h(
              "li",
              {
                class:
                  "row compact " +
                  (resting[name] ? "tone-waiting" : "tone-done"),
              },
              h("span", { class: "notice-dot", "aria-hidden": "true" }),
              h(
                "div",
                { class: "row-body" },
                h("strong", { class: "mono" }, name),
                h(
                  "small",
                  { class: "row-meta" },
                  resting[name]
                    ? t("rotation.resting", {
                        until: formatTime(resting[name].until, true),
                        reason: t("failure." + resting[name].reason),
                      })
                    : profile.model,
                ),
              ),
              h(
                "small",
                { class: "mono" },
                `${usage[name]?.calls || 0} · ${money(usage[name]?.usd)}`,
              ),
            ),
          ),
        )
      : h("p", { class: "hint" }, t("agents.noProfiles")),
  );
}

function plansPanel(list) {
  const plans = new Map();
  for (const run of list) {
    const plan = meta(run).plan;
    if (!plan) continue;
    const entry = plans.get(plan) || { total: 0, done: 0, needs: 0 };
    entry.needs += laneOf(run) === "needs" ? 1 : 0;
    entry.total++;
    entry.done += run.status === "accepted" ? 1 : 0;
    plans.set(plan, entry);
  }
  if (!plans.size) return null;
  // Plans that need you first, then the ones with the most work left.
  const top = [...plans.entries()]
    .sort(
      (a, b) =>
        b[1].needs - a[1].needs ||
        b[1].total - b[1].done - (a[1].total - a[1].done),
    )
    .slice(0, 8);
  return panel(
    t("overview.plans"),
    h(
      "button",
      {
        type: "button",
        class: "ghost",
        onclick: () => showPlan(""),
      },
      t("overview.plansAll"),
    ),
    h(
      "ul",
      { class: "rows" },
      top.map(([plan, runsOf]) => {
        // The plan file's own progress when known, else the board's.
        const file = planInfo(plan);
        const { total, done } = file?.requirements
          ? { total: file.requirements, done: file.accepted || 0 }
          : runsOf;
        return h(
          "li",
          { class: "row compact plan-row" },
          h(
            "button",
            {
              type: "button",
              class: "link row-title",
              title: planTitle(plan),
              onclick: () => showPlan(plan),
            },
            planTitle(plan),
          ),
          h(
            "span",
            { class: "progress-track" },
            h("span", {
              style: { width: Math.round((100 * done) / total) + "%" },
            }),
          ),
          h("small", { class: "mono" }, `${done}/${total}`),
        );
      }),
    ),
  );
}

function teamPanel(busy) {
  if (!team) team = teamWidget();
  // The office carries information only while someone works; fold it otherwise.
  const open = recall("overview-team", null) ?? busy;
  const details = h(
    "details",
    {
      class: "panel team-panel",
      open,
      ontoggle: (e) => remember("overview-team", e.target.open),
    },
    h(
      "summary",
      {},
      h("h2", {}, t("nav.team")),
      h("span", { class: "hint" }, t("team.lead")),
    ),
    team.element,
  );
  return details;
}

function draw() {
  if (!root) return;
  if (!hasScope()) {
    render(
      ["welcome", store.state?.projects, window.ffaiPreferences.language],
      () => replace(root, welcome()),
    );
    return;
  }
  const list = runs();
  const n = numbers(list);
  const { totals, settings } = store.state;
  const inputs = [
    list,
    needsElsewhere(),
    store.state.plans,
    events,
    store.state.cooldowns,
    store.state.usage,
    settings,
    window.ffaiPreferences.language,
  ];
  render(inputs, () => {
    const scroll = window.scrollY;
    replace(
      root,
      h(
        "div",
        { class: "summary-tiles overview-tiles" },
        tile(
          t("overview.tile.needs"),
          n.needs,
          "",
          n.needs ? () => go("board") : null,
          n.needs ? "tone-attention" : "",
        ),
        tile(
          t("overview.tile.running"),
          n.running,
          n.waiting ? t("overview.tile.queued", { count: n.waiting }) : "",
        ),
        tile(
          t("overview.tile.startable"),
          n.startable,
          n.startable ? t("overview.tile.startableHint") : "",
          n.startable ? () => openBulkResume() : null,
        ),
        tile(
          t("overview.tile.accepted"),
          `${n.accepted} / ${n.total}`,
          n.mixed
            ? t("overview.tile.split", { req: n.split[0], tix: n.split[1] })
            : "",
        ),
        tile(
          t("meter.calls"),
          `${totals.calls} / ${settings.max_calls}`,
          t("overview.tile.callsHint"),
          () => go("usage"),
        ),
        tile("≈ API", money(store.state.usage?.total_usd)),
      ),
      h(
        "div",
        { class: "overview-grid" },
        needsPanel(list),
        runningPanel(list),
      ),
      teamPanel(n.running > 0),
      h(
        "div",
        { class: "overview-grid three" },
        eventsPanel(),
        agentsPanel(),
        plansPanel(list),
      ),
    );
    window.scrollTo(0, scroll);
  });
  team?.update();
}

async function loadEvents() {
  const last = store.state?.last_transition;
  if (last === eventsFor && events.length) return;
  eventsFor = last;
  try {
    events = (await api.get("log", { limit: 60 }))
      .filter((e) => e.kind !== "action")
      .reverse();
    draw();
  } catch {
    /* Events are optional on the overview. */
  }
}

registerView({
  id: "overview",
  order: 0,
  title: "nav.overview",
  glyph: "◧",
  mount(container) {
    root = container;
    render(null, () => {});
    draw();
    loadEvents();
  },
  update() {
    draw();
    loadEvents();
  },
  unmount() {
    root = null;
  },
});

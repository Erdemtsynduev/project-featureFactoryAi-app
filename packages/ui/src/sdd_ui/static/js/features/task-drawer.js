/* Task drawer: everything about one task over the current view.
 *
 * Top: why it is (not) moving and the action that moves it, what it waits for,
 * the live attempt, all commands and the folded workflow path. Tabs live in
 * ../drawer/: Discussion (answers, results, messages to the next step), Details
 * (facts, dependencies, the feature's documents, memory, brief, lane) and Log. */

import * as api from "../core/api.js";
import { h, replace } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { recall, remember } from "../core/storage.js";
import {
  kindOf,
  meta,
  refresh,
  run as findRun,
  runnerOf,
  subscribe,
  titleOf,
} from "../core/store.js";
import { details, waitingPanel } from "../drawer/details.js";
import { discussion } from "../drawer/discussion.js";
import { livePanel, stopLive } from "../drawer/live.js";
import { logTab } from "../drawer/log.js";
import { render as renderPipeline } from "../graph/pipeline.js";
import { openDialog } from "../ui/dialog.js";
import { attempt, toastError } from "../ui/toast.js";
import { availableCommands, commandButton, primaryAction } from "./commands.js";
import { closeTaskRoute, onTaskRoute } from "./shell.js";
import {
  attentionText,
  KIND_GLYPH,
  kindLabel,
  stepName,
} from "./vocabulary.js";

let drawer = null; // { id, dialog, detail, version, tab }
let request = 0;

/* Header and actions ---------------------------------------------------------- */

/** The answer lives on the discussion tab: switch to it, then bring the answer's
 * buttons into view with the cursor in its first field. */
function goToAnswer() {
  if (!drawer?.detail) return;
  if (drawer.tab !== "discussion") {
    drawer.tab = "discussion";
    remember("drawer-tab", "discussion");
    renderDrawer(drawer.detail);
  }
  const panel = drawer.dialog.body.querySelector(".answer-panel");
  if (!panel) return;
  panel.querySelector("textarea, .option")?.focus({ preventScroll: true });
  panel.scrollIntoView({ block: "end", behavior: "smooth" });
}

function banner(run) {
  return h(
    "div",
    { class: "attention tone-" + (run.attention?.tone || "idle") },
    h("span", { class: "attention-text" }, attentionText(run)),
    primaryAction(run, goToAnswer),
  );
}

function actions(run) {
  // The banner already offers the attention action; do not repeat it here.
  const buttons = availableCommands(run)
    .filter(([name]) => name !== run.attention?.action)
    .map((command) => commandButton(command, run));
  const auto = h("input", {
    type: "checkbox",
    checked: !!run.auto_answer,
    disabled: run.status === "accepted",
    onchange: (e) =>
      attempt(
        async () => {
          await api.command(e.target.checked ? "auto" : "manual", run);
          await refresh();
        },
        e.target.checked ? t("answer.autoOn") : t("answer.autoOff"),
      ),
  });
  return h(
    "div",
    { class: "drawer-actions" },
    buttons,
    h("span", { class: "spacer" }),
    h(
      "label",
      { class: "switch", title: t("answer.autoHint") },
      auto,
      h("span", {}, t("answer.auto")),
    ),
  );
}

/* Drawer ---------------------------------------------------------------------- */

const TABS = ["discussion", "details", "log"];

function renderDrawer(detail) {
  // The board snapshot adds what the detail lacks: attention and dependencies.
  const listed = findRun(detail.run.id) || {};
  const run = {
    ...detail.run,
    attention: listed.attention,
    dependencies: listed.dependencies || [],
    pending_dependencies: listed.pending_dependencies || [],
  };
  detail.run = run;
  const step = detail.workflow.steps.find((s) => s.id === run.step);
  const tab = drawer.tab;
  const panes = {
    discussion: () => discussion(detail, step),
    details: () => details(detail, step),
    log: () => logTab(detail),
  };
  const pane = h(
    "div",
    { class: "drawer-pane", role: "tabpanel" },
    panes[tab](),
  );
  const tabs = h(
    "div",
    { class: "tabs", role: "tablist" },
    TABS.map((key) =>
      h(
        "button",
        {
          type: "button",
          role: "tab",
          class: key === tab ? "active" : "",
          "aria-selected": String(key === tab),
          onclick: () => {
            drawer.tab = key;
            remember("drawer-tab", key);
            renderDrawer(drawer.detail);
          },
        },
        t("detail.tab." + key),
      ),
    ),
  );
  const track = h("div", {
    class: "run-pipeline",
    "aria-label": t("detail.path"),
  });
  // The path is reference material: folded by default, the choice remembered.
  const path = h(
    "details",
    {
      class: "run-path",
      open: recall("drawer-path", false),
      ontoggle: (e) => remember("drawer-path", e.target.open),
    },
    h(
      "summary",
      {},
      t("detail.pathNow", { step: stepName(step || run.step) }),
    ),
    track,
  );
  renderPipeline(track, detail.workflow, {
    label: stepName,
    runner: (s) => runnerOf(s, t),
    zoom: 0.62,
    run,
    recoveryLabel: t("pipeline.recovery"),
    ariaLabel: t("detail.path"),
  });
  const kind = kindOf(run);
  drawer.dialog.setTitle(
    titleOf(run),
    `${KIND_GLYPH[kind]} ${kindLabel(kind)} · ${run.id}`,
  );
  const scroll = drawer.dialog.body.scrollTop;
  const working = run.active && step?.kind !== "human";
  replace(
    drawer.dialog.body,
    banner(run),
    waitingPanel(run),
    working ? livePanel(run, step, () => drawer?.id === run.id) : null,
    actions(run),
    path,
    tabs,
    pane,
  );
  drawer.dialog.body.scrollTop = scroll;
}

async function load(id) {
  const mine = ++request;
  try {
    const detail = await api.get("run", { id });
    if (mine !== request || drawer?.id !== id) return;
    drawer.detail = detail;
    drawer.version = detail.run.version;
    renderDrawer(detail);
  } catch (error) {
    toastError(error);
    close();
  }
}

function open(id) {
  if (drawer?.id === id) return;
  if (drawer) {
    // Another task replaces the content; the drawer and its history entry stay.
    Object.assign(drawer, { id, detail: null, version: null });
    drawer.dialog.setTitle(meta(id).title || id, "");
    load(id);
    return;
  }
  const dialog = openDialog({
    title: meta(id).title || id,
    variant: "drawer",
    label: t("detail.label"),
    body: [h("p", { class: "hint" }, t("log.loading"))],
    onClose: () => {
      drawer = null;
      stopLive();
      closeTaskRoute();
    },
  });
  drawer = {
    id,
    dialog,
    detail: null,
    version: null,
    tab: recall("drawer-tab", "discussion"),
  };
  // Work waiting for your answer opens where the answer is.
  if (!TABS.includes(drawer.tab) || findRun(id)?.attention?.action === "answer")
    drawer.tab = "discussion";
  load(id);
}

function close() {
  drawer?.dialog.close(true);
}

export function startTaskDrawer() {
  onTaskRoute((id) => (id ? open(id) : close()));
  subscribe(() => {
    if (!drawer?.detail) return;
    const current = findRun(drawer.id);
    if (!current) return;
    // Refetch when the task moved; otherwise redraw when its reason or waits changed.
    const shown = drawer.detail.run;
    if (current.version !== drawer.version) load(drawer.id);
    else if (
      JSON.stringify([current.attention, current.pending_dependencies]) !==
      JSON.stringify([shown.attention, shown.pending_dependencies])
    )
      renderDrawer(drawer.detail);
  });
}

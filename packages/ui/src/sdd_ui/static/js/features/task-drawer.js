/* Task drawer: everything about one task over the current view.
 *
 * Top: why it is (not) moving and the action that moves it, then all commands
 * and the task's path through its workflow. Tabs: Discussion (answers,
 * results, messages to the next step), Details (brief, memory, tickets,
 * workspace) and Log (journal, flight log, incident export and recovery). */

import * as api from "../core/api.js";
import { h, replace } from "../core/dom.js";
import { formatTime, money, t } from "../core/i18n.js";
import { persistentMap, recall, remember } from "../core/storage.js";
import {
  childrenOf,
  kindOf,
  meta,
  refresh,
  run as findRun,
  runnerOf,
  store,
  subscribe,
  titleOf,
} from "../core/store.js";
import { render as renderPipeline } from "../graph/pipeline.js";
import { openDialog } from "../ui/dialog.js";
import { attempt, toast, toastError } from "../ui/toast.js";
import { answerPanel } from "./answers.js";
import { availableCommands, commandButton, primaryAction } from "./commands.js";
import { closeTaskRoute, onTaskRoute, openTask } from "./shell.js";
import {
  attentionText,
  elapsed,
  KIND_GLYPH,
  kindLabel,
  statusPill,
  stepName,
} from "./vocabulary.js";

const messages = persistentMap("message-drafts");
let drawer = null; // { id, dialog, detail, version, tab }
let request = 0;

function section(title, ...children) {
  return h(
    "section",
    { class: "drawer-section" },
    title ? h("h3", {}, title) : null,
    children,
  );
}

/* Header and actions ---------------------------------------------------------- */

function banner(run) {
  return h(
    "div",
    { class: "attention tone-" + (run.attention?.tone || "idle") },
    h("span", { class: "attention-text" }, attentionText(run)),
    primaryAction(run, () =>
      drawer?.dialog.body
        .querySelector(".answer-panel textarea, .answer-panel .option")
        ?.focus(),
    ),
  );
}

function actions(run) {
  const buttons = availableCommands(run).map((command) =>
    commandButton(command, run),
  );
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

/* Discussion ------------------------------------------------------------------ */

function resultBubble(result) {
  const data =
    typeof result.data === "string" ? safeJson(result.data) : result.data || {};
  const usage = result.usage || {};
  const tokens = (usage.input_tokens || 0) + (usage.output_tokens || 0);
  return h(
    "article",
    { class: "message outcome-" + result.outcome },
    h(
      "header",
      {},
      h(
        "span",
        { class: "pill" },
        t("outcome." + result.outcome) === "outcome." + result.outcome
          ? result.outcome
          : t("outcome." + result.outcome),
      ),
      data.answer !== undefined
        ? h(
            "small",
            {},
            data.auto ? t("answer.autoAnswered") : t("answer.human"),
          )
        : null,
      tokens
        ? h("small", { class: "mono" }, t("detail.tokens", { count: tokens }))
        : null,
    ),
    h("p", { class: "message-text" }, result.reason),
    data.notes?.length
      ? h(
          "ul",
          { class: "notes" },
          data.notes.map((n) => h("li", {}, n)),
        )
      : null,
  );
}

function safeJson(text) {
  try {
    return JSON.parse(text || "{}");
  } catch {
    return {};
  }
}

function operatorMessages(detail) {
  return detail.events
    .filter((event) => event.kind === "operator_message")
    .map((event) => {
      const message = safeJson(event.detail);
      return h(
        "article",
        { class: "message from-operator" },
        h(
          "header",
          {},
          h("span", { class: "pill" }, t("detail.you")),
          h("small", {}, formatTime(event.at)),
        ),
        h("p", { class: "message-text" }, message.text),
        h(
          "small",
          { class: "hint" },
          detail.run.generation > message.after_generation
            ? t("message.delivered")
            : t("message.pending"),
        ),
      );
    });
}

function messageForm(run) {
  const input = h("textarea", {
    rows: "3",
    required: true,
    "aria-label": t("message.label"),
    placeholder: t("message.placeholder"),
    value: messages.get(run.id) || "",
    oninput: (e) => messages.set(run.id, e.target.value),
  });
  const send = h(
    "button",
    { type: "submit", class: "primary" },
    t("message.send"),
  );
  return h(
    "form",
    {
      class: "message-form",
      onsubmit: async (event) => {
        event.preventDefault();
        send.disabled = true;
        try {
          await api.command("message", run, { message: input.value });
          messages.delete(run.id);
          await refresh();
          toast(t("message.saved"), { tone: "success" });
        } catch (error) {
          toastError(error);
        } finally {
          send.disabled = false;
        }
      },
    },
    input,
    h(
      "div",
      { class: "form-actions" },
      h("small", { class: "hint" }, t("message.hint")),
      send,
    ),
  );
}

function discussion(detail, step) {
  const run = detail.run;
  const nodes = [];
  if (run.active && step?.kind === "human")
    nodes.push(answerPanel(detail, step));
  else if (run.status !== "accepted") nodes.push(messageForm(run));
  const history = [
    ...detail.results.map(resultBubble),
    ...operatorMessages(detail),
  ];
  if (history.length) nodes.push(section(t("detail.history"), history));
  else nodes.push(h("p", { class: "empty" }, t("detail.noResults")));
  return nodes;
}

/* Details --------------------------------------------------------------------- */

function memoryNotes(detail) {
  const seen = new Set();
  for (const result of [...detail.results].reverse())
    for (const note of safeJson(result.data).notes || []) seen.add(note);
  return [...seen];
}

function details(detail, step) {
  const run = detail.run;
  const m = detail.metadata || {};
  const children = childrenOf(run);
  const notes = memoryNotes(detail);
  const facts = [
    [t("detail.kind"), kindLabel(kindOf(run))],
    [
      t("detail.flow"),
      detail.workflow.id + " · " + run.workflow_digest.slice(0, 10),
    ],
    [t("detail.step"), `${stepName(step || run.step)} · ${runnerOf(step, t)}`],
    [
      t("detail.calls"),
      `${run.calls} (${t("detail.planning")}: ${run.planning_calls})`,
    ],
    [
      t("detail.tokensLabel"),
      run.tokens.toLocaleString() + (run.usage_unknown ? " + ?" : ""),
    ],
    [t("detail.cost"), money(store.state.usage?.per_run_usd?.[run.id])],
    [t("detail.workspace"), store.state.locations?.[run.id] || "—"],
  ];
  return [
    section(
      "",
      h(
        "dl",
        { class: "facts" },
        facts.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]),
      ),
    ),
    m.parent
      ? section(
          t("detail.parent"),
          h(
            "button",
            {
              type: "button",
              class: "link",
              onclick: () => openTask(m.parent),
            },
            findRun(m.parent) ? titleOf(findRun(m.parent)) : m.parent,
          ),
        )
      : null,
    children.length
      ? section(
          t("detail.children", { count: children.length }),
          h(
            "ul",
            { class: "child-list" },
            children.map((child) =>
              h(
                "li",
                {},
                h(
                  "button",
                  {
                    type: "button",
                    class: "link",
                    onclick: () => openTask(child.id),
                  },
                  titleOf(child),
                ),
                " ",
                statusPill(child),
              ),
            ),
          ),
        )
      : null,
    section(
      t("detail.memory"),
      notes.length
        ? h(
            "ul",
            { class: "notes" },
            notes.map((n) => h("li", {}, n)),
          )
        : h("p", { class: "hint" }, t("detail.memoryEmpty")),
    ),
    section(t("detail.brief"), h("pre", { class: "brief" }, detail.context)),
    detail.lane?.root ? laneInfo(detail.lane) : null,
    step?.prompt
      ? h(
          "details",
          {},
          h("summary", {}, t("detail.prompt")),
          h("pre", {}, step.prompt),
        )
      : null,
    h(
      "details",
      {},
      h("summary", {}, t("detail.raw")),
      h("pre", {}, JSON.stringify(detail.results, null, 2)),
    ),
  ];
}

function laneInfo(lane) {
  return section(
    lane.status === "removed" ? t("lane.merged") : t("lane.isolated"),
    h("p", { class: "mono" }, lane.root),
    h(
      "ul",
      {},
      (lane.repos || []).map((repo) =>
        h(
          "li",
          {},
          h("code", {}, repo.path),
          " ",
          repo.fresh
            ? t("lane.fresh")
            : t("lane.from", {
                origin: repo.origin || "HEAD",
                base: (repo.base || "").slice(0, 10),
              }),
          repo.branch ? h("code", {}, " " + repo.branch) : null,
        ),
      ),
    ),
  );
}

/* Log ------------------------------------------------------------------------- */

function logTab(detail) {
  const run = detail.run;
  const box = h(
    "div",
    { class: "log-box" },
    h("p", { class: "hint" }, t("log.loading")),
  );
  api
    .get("log", { run: run.id, limit: 200 })
    .then((entries) =>
      replace(
        box,
        entries.length
          ? h(
              "ol",
              { class: "timeline" },
              entries
                .slice()
                .reverse()
                .map((e) =>
                  h(
                    "li",
                    { class: "level-" + e.level },
                    h("time", {}, formatTime(e.at, true)),
                    h(
                      "strong",
                      {},
                      t("log.kind." + e.kind) === "log.kind." + e.kind
                        ? e.kind
                        : t("log.kind." + e.kind),
                    ),
                    h(
                      "span",
                      {},
                      [e.action, e.command, e.reason, e.error, e.outcome]
                        .filter(Boolean)
                        .join(" · "),
                    ),
                  ),
                ),
            )
          : h("p", { class: "hint" }, t("log.empty")),
      ),
    )
    .catch(toastError);
  const recoverable = availableCommands(run).some(
    ([name]) => name === "recover",
  );
  return [
    section(
      t("log.incident"),
      h("p", { class: "hint" }, t("log.incidentHint")),
      h(
        "div",
        { class: "form-actions" },
        h(
          "button",
          { type: "button", onclick: () => downloadIncident(run.id) },
          t("log.download"),
        ),
        recoverable
          ? commandButton(["recover", "command.recover", "primary"], run)
          : null,
      ),
    ),
    section(t("log.flight"), box),
    section(
      t("log.journal"),
      h(
        "ol",
        { class: "timeline" },
        detail.events
          .slice()
          .reverse()
          .map((e) =>
            h(
              "li",
              {},
              h("time", {}, formatTime(e.at, true)),
              h("strong", {}, e.kind),
              h(
                "span",
                { class: "mono" },
                String(e.detail || "").slice(0, 400),
              ),
            ),
          ),
      ),
    ),
  ];
}

async function downloadIncident(id) {
  try {
    const record = await api.get("incident", { id });
    const blob = new Blob([JSON.stringify(record, null, 2)], {
      type: "application/json",
    });
    const link = h("a", {
      href: URL.createObjectURL(blob),
      download: `incident-${id}.json`,
    });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  } catch (error) {
    toastError(error);
  }
}

/* Live progress ---------------------------------------------------------------- */

const STALE_OUTPUT = 300; // seconds without output before the panel says so
let liveTimer = null;

function stopLive() {
  clearTimeout(liveTimer);
  liveTimer = null;
}

/** What the running attempt is doing now: time, timeout, freshness and output. */
function livePanel(run, step) {
  const box = h("section", { class: "live" });
  const draw = (data) => {
    if (!data.active) return replace(box);
    const total = data.deadline - data.started;
    const share = Math.min(
      100,
      Math.round((100 * (data.now - data.started)) / total),
    );
    const lines = Object.values(data.streams).flatMap((stream) => stream.lines);
    const quiet =
      data.last_output && data.now - data.last_output > STALE_OUTPUT;
    const tail = h(
      "pre",
      { class: "live-tail", "aria-live": "polite" },
      lines.slice(-30).join("\n") || t("live.noOutput"),
    );
    replace(
      box,
      h(
        "div",
        { class: "live-head" },
        h("span", { class: "spinner", "aria-hidden": "true" }),
        h("strong", {}, t("live.now", { step: stepName(step || run.step) })),
        h("span", { class: "runner" }, runnerOf(step, t)),
        h("span", { class: "spacer" }),
        h(
          "span",
          { class: "elapsed mono", dataset: { since: String(data.started) } },
          elapsed(data.started, data.now),
        ),
      ),
      h(
        "div",
        {
          class: "live-meter",
          title: t("live.timeoutAt", { time: formatTime(data.deadline) }),
        },
        h(
          "span",
          { class: "progress-track wide" },
          h("span", { style: { width: share + "%" } }),
        ),
        h(
          "small",
          { class: "hint" },
          t("live.timeout", { time: formatTime(data.deadline) }),
        ),
      ),
      h(
        "small",
        { class: quiet ? "live-quiet" : "hint" },
        data.last_output
          ? h(
              "span",
              { dataset: { ago: String(data.last_output) } },
              t("live.ago", { time: elapsed(data.last_output, data.now) }),
            )
          : t("live.waitingOutput"),
        quiet ? " · " + t("live.quiet") : "",
        data.hosted ? "" : " · " + t("live.notHosted"),
      ),
      tail,
    );
    tail.scrollTop = tail.scrollHeight;
  };
  const poll = async () => {
    if (!drawer || drawer.id !== run.id || !box.isConnected) return stopLive();
    try {
      draw(await api.get("live", { id: run.id }));
    } catch {
      /* The next poll retries; the panel keeps its last picture. */
    }
    liveTimer = setTimeout(poll, 2000);
  };
  stopLive();
  liveTimer = setTimeout(poll, 0);
  return box;
}

/* Drawer ---------------------------------------------------------------------- */

const TABS = ["discussion", "details", "log"];

function renderDrawer(detail) {
  const run = { ...detail.run, attention: findRun(detail.run.id)?.attention };
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
    working ? livePanel(run, step) : null,
    actions(run),
    track,
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
  if (!TABS.includes(drawer.tab)) drawer.tab = "discussion";
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
    // Refetch when the task moved; otherwise refresh only the attention line.
    if (current.version !== drawer.version) load(drawer.id);
    else if (
      JSON.stringify(current.attention) !==
      JSON.stringify(drawer.detail.run.attention)
    )
      renderDrawer(drawer.detail);
  });
}

/* Workflow editor. The draft lives in this browser and is saved on every edit;
 * inspector fields apply as soon as they change, so switching steps never drops
 * an edit. Validation and publication are explicit: publishing creates an
 * immutable version, and existing tasks keep the version they were created with.
 *
 * Tasks do not need a hand-published flow: creating a task by its kind
 * publishes the project's version of the template. The editor is for custom
 * flows and for inspecting what each step does, so it opens read-only: fields
 * are disabled and the graph has no edit handles until Edit is chosen. */

import * as api from "../core/api.js";
import { h, replace } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { recall, remember } from "../core/storage.js";
import {
  currentProject,
  profileNames,
  refresh,
  runnerOf,
  store,
} from "../core/store.js";
import { render as renderPipeline } from "../graph/pipeline.js";
import { confirmDialog } from "../ui/dialog.js";
import { attempt, toast, toastError } from "../ui/toast.js";
import { go, registerView } from "./shell.js";
import {
  buildInspector,
  fillEdges,
  fillInspector,
} from "../flows/inspector.js";
import { editor, nodes, persist, showSaved } from "../flows/state.js";
import { stepName } from "./vocabulary.js";

const TEMPLATES = [
  "main-flow",
  "draft",
  "feature",
  "ticket",
  "approved-feature",
  "interview",
  "demo",
];
let root = null;

async function replaceDraft(flow, message) {
  if (editor.dirty && editor.flow) {
    const ok = await confirmDialog({
      title: t("flows.replaceTitle"),
      message: t("flows.replaceText"),
      confirm: t("flows.replace"),
      danger: true,
    });
    if (!ok) return false;
  }
  editor.flow = structuredClone(flow);
  editor.selected = 0;
  editor.selectedEdge = null;
  editor.connecting = null;
  editor.dirty = false;
  remember("flow-draft", { flow: editor.flow, saved: null });
  editor.saved = null;
  draw();
  fit();
  if (message) toast(message);
  return true;
}

/** Open a workflow and select one step (from the team view or elsewhere). */
export async function editStep(workflow, stepId) {
  go("flows");
  if (!workflow) return;
  if (
    editor.flow?.id !== workflow.id ||
    editor.flow.steps.length !== workflow.steps.length
  ) {
    if (!(await replaceDraft(workflow))) return;
  }
  editor.selected = Math.max(
    0,
    editor.flow.steps.findIndex((s) => s.id === stepId),
  );
  draw();
}

/* Graph ------------------------------------------------------------------------ */

function defaultOutcome(step) {
  // Never propose an outcome the step already routes: connecting must not rewire it.
  const used = new Set(step.transitions.map(([o]) => o));
  const first =
    step.kind === "condition"
      ? "true"
      : step.gate
        ? "passed"
        : step.kind === "human"
          ? "answered"
          : "done";
  const names = [
    first,
    step.kind === "condition" ? "false" : "failed",
    "questions",
    "retry",
  ];
  for (let n = 2; names.length < 50; n++) names.push("outcome" + n);
  return names.find((name) => !used.has(name));
}

function newStep(id, transitions = []) {
  return {
    id,
    kind: "agent",
    handler: "",
    profile: "default",
    prompt: "",
    transitions,
    config: "{}",
    timeout: 900,
    max_visits: 3,
    required: false,
    gate: false,
    mutates: false,
    condition_key: "",
    condition_value: "",
  };
}

function freeId() {
  let n = editor.flow.steps.length;
  while (editor.flow.steps.some((s) => s.id === "step" + n)) n++;
  return "step" + n;
}

function setEditing(on) {
  editor.editing = on;
  remember("flow-editing", on);
  editor.connecting = null;
  editor.selectedEdge = null;
  applyMode();
  drawGraph();
}

/** Viewing disables every field and hides the controls that change the draft. */
function applyMode() {
  const on = editor.editing;
  nodes.root.classList.toggle("viewing", !on);
  for (const field of nodes.root.querySelectorAll(
    ".flow-meta :is(input, select), #step-form :is(input, select, textarea)",
  ))
    field.disabled = !on;
  nodes.json.readOnly = !on;
  for (const [button, mode] of nodes.modes) {
    button.classList.toggle("active", mode === on);
    button.setAttribute("aria-pressed", String(mode === on));
  }
}

function drawGraph() {
  const focused = nodes.graph.contains(document.activeElement)
    ? document.activeElement.getAttribute("aria-label")
    : null;
  const flow = editor.flow;
  editor.size = renderPipeline(nodes.graph, flow, {
    label: stepName,
    runner: (step) => runnerOf(step, t),
    selected: editor.selected,
    zoom: editor.zoom,
    connecting: editor.connecting,
    selectedEdge: editor.selectedEdge,
    recoveryLabel: t("pipeline.recovery"),
    ariaLabel: t("flows.graph"),
    onSelect: (index) => {
      if (editor.connecting) {
        connect(
          editor.connecting,
          nodes.edgeOutcome.value,
          flow.steps[index].id,
        );
        editor.connecting = null;
        return;
      }
      editor.selected = index;
      editor.selectedEdge = null;
      draw();
    },
    ...(editor.editing ? editing(flow) : {}),
  });
  if (focused)
    (
      nodes.graph.querySelector(`[aria-label="${CSS.escape(focused)}"]`) ||
      nodes.graph
    ).focus();
}

/** Graph handlers that change the draft; absent in view mode. */
function editing(flow) {
  return {
    onEdge: (source, outcome, target) => {
      editor.selectedEdge = [source, outcome];
      nodes.edgeSource.value = source;
      nodes.edgeTarget.value = target;
      nodes.edgeOutcome.value = outcome;
      drawGraph();
      toast(t("flows.edgeSelected"));
    },
    onInsert: (source, outcome) => {
      const from = flow.steps.find((s) => s.id === source);
      const edge = from.transitions.find(([o]) => o === outcome);
      const id = freeId();
      flow.steps.push(newStep(id, [["done", edge[1]]]));
      edge[1] = id;
      editor.selected = flow.steps.length - 1;
      editor.selectedEdge = null;
      persist();
      draw();
      toast(t("flows.inserted"));
    },
    onPort: (source) => {
      editor.connecting = editor.connecting === source ? null : source;
      editor.selectedEdge = null;
      if (editor.connecting) {
        nodes.edgeSource.value = source;
        nodes.edgeOutcome.value = defaultOutcome(
          flow.steps.find((s) => s.id === source),
        );
        toast(t("flows.pickTarget", { outcome: nodes.edgeOutcome.value }));
      }
      drawGraph();
    },
    onBackground: () => {
      if (!editor.connecting && !editor.selectedEdge) return;
      editor.connecting = null;
      editor.selectedEdge = null;
      drawGraph();
    },
  };
}

function fit() {
  if (!nodes.graph) return;
  editor.zoom = Math.max(
    0.6,
    Math.min(1, (nodes.graph.clientWidth - 16) / editor.size.width),
  );
  drawGraph();
}

function connect(sourceId, outcome, target) {
  const source = editor.flow.steps.find((s) => s.id === sourceId);
  outcome = outcome.trim();
  if (!source || !target || !outcome)
    return toastError(t("flows.edgeIncomplete"));
  const existing = source.transitions.find(([o]) => o === outcome);
  if (existing) existing[1] = target;
  else source.transitions.push([outcome, target]);
  editor.selected = editor.flow.steps.indexOf(source);
  editor.selectedEdge = [source.id, outcome];
  persist();
  draw();
}

function removeEdge() {
  const source = editor.flow.steps.find((s) => s.id === nodes.edgeSource.value);
  if (!source) return toastError(t("flows.pickEdge"));
  source.transitions = source.transitions.filter(
    ([o, target]) =>
      !(o === nodes.edgeOutcome.value && target === nodes.edgeTarget.value),
  );
  editor.selectedEdge = null;
  persist();
  draw();
}

/* Meta and actions ------------------------------------------------------------- */

function captureMeta() {
  const flow = editor.flow;
  flow.id = nodes.flowId.value.trim();
  flow.entry = nodes.flowEntry.value;
  flow.max_calls = Number(nodes.flowCalls.value) || flow.max_calls;
  flow.max_planning_calls =
    nodes.flowPlanning.value === "" ? null : Number(nodes.flowPlanning.value);
  flow.max_tokens = nodes.flowTokens.value
    ? Number(nodes.flowTokens.value)
    : null;
  persist();
}

function fillMeta() {
  const flow = editor.flow;
  nodes.flowId.value = flow.id;
  replace(
    nodes.flowEntry,
    flow.steps.map((s) => h("option", { value: s.id }, stepName(s))),
  );
  nodes.flowEntry.value = flow.entry;
  nodes.flowCalls.value = flow.max_calls;
  nodes.flowPlanning.value = flow.max_planning_calls ?? "";
  nodes.flowTokens.value = flow.max_tokens ?? "";
}

async function loadTemplate() {
  await attempt(async () => {
    const flow = await api.post("template", {
      name: nodes.template.value,
      project: currentProject()?.id || "",
      language: window.ffaiPreferences.language,
    });
    remember("template", nodes.template.value);
    await replaceDraft(flow, t("flows.templateOpened"));
  });
}

async function check(publish) {
  await attempt(
    async () => {
      captureMeta();
      const result = await api.post(publish ? "publish" : "validate", {
        workflow: editor.flow,
      });
      if (publish) {
        editor.dirty = false;
        await refresh();
      }
      return result;
    },
    (result) =>
      publish
        ? t("flows.published", { digest: result.digest.slice(0, 12) })
        : t("flows.valid"),
  );
}

function addStep() {
  editor.flow.steps.push(newStep(freeId()));
  editor.selected = editor.flow.steps.length - 1;
  persist();
  draw();
}

async function deleteStep() {
  const removed = editor.flow.steps[editor.selected];
  if (!removed) return;
  const ok = await confirmDialog({
    title: t("flows.deleteTitle"),
    message: t("flows.deleteText", { step: stepName(removed) }),
    confirm: t("action.delete"),
    danger: true,
  });
  if (!ok) return;
  const flow = editor.flow;
  flow.steps.splice(editor.selected, 1);
  for (const step of flow.steps)
    step.transitions = step.transitions.filter(
      ([, target]) => target !== removed.id,
    );
  if (flow.entry === removed.id) flow.entry = flow.steps[0]?.id || "";
  editor.selected = 0;
  persist();
  draw();
}

/* View ------------------------------------------------------------------------- */

function build() {
  for (const key of Object.keys(nodes)) delete nodes[key];
  const n = nodes;
  n.template = h(
    "select",
    { id: "template", "aria-label": t("flows.template") },
    TEMPLATES.map((name) =>
      h("option", { value: name }, t("template." + name)),
    ),
  );
  n.template.value = recall("template", "main-flow");
  n.saved = h("small", { class: "draft-state" });
  n.versions = h("select", {
    id: "saved-flows",
    "aria-label": t("flows.versions"),
    onchange: async (e) => {
      const digest = e.target.value;
      e.target.value = "";
      if (!digest) return;
      try {
        // The board's snapshot omits prompts; the editor opens the full version.
        const workflow = await api.get("definition", { digest });
        await replaceDraft(workflow, t("flows.versionOpened"));
      } catch (error) {
        toastError(error);
      }
    },
  });
  n.flowId = h("input", { id: "flow-id", onchange: captureMeta });
  n.flowEntry = h("select", {
    id: "flow-entry",
    onchange: () => (captureMeta(), drawGraph()),
  });
  n.flowCalls = h("input", {
    id: "flow-calls",
    type: "number",
    min: "1",
    onchange: captureMeta,
  });
  n.flowPlanning = h("input", {
    id: "flow-planning",
    type: "number",
    min: "0",
    onchange: captureMeta,
  });
  n.flowTokens = h("input", {
    id: "flow-tokens",
    type: "number",
    min: "1",
    placeholder: t("flows.noLimit"),
    onchange: captureMeta,
  });
  n.graph = h("div", {
    id: "graph",
    tabindex: "0",
    onkeydown: (e) => {
      if (e.key === "Escape" && (editor.connecting || editor.selectedEdge)) {
        editor.connecting = null;
        editor.selectedEdge = null;
        drawGraph();
      } else if (
        (e.key === "Delete" || e.key === "Backspace") &&
        editor.selectedEdge
      )
        removeEdge();
    },
  });
  n.edgeSource = h("select", { id: "edge-source" });
  n.edgeTarget = h("select", { id: "edge-target" });
  n.edgeOutcome = h("input", { id: "edge-outcome", value: "done" });
  n.inspectorTitle = h("h2", { id: "inspector-title" });
  n.json = h("textarea", { id: "flow-json", rows: "14", spellcheck: "false" });
  const labelled = (key, node) =>
    h("label", { class: "field" }, h("span", {}, t(key)), node);
  n.modes = [
    [false, "flows.mode.view", "flow-view"],
    [true, "flows.mode.edit", "flow-edit"],
  ].map(([mode, key, id]) => [
    h(
      "button",
      { type: "button", id, onclick: () => setEditing(mode) },
      t(key),
    ),
    mode,
  ]);
  const zoom = (delta) => () => {
    editor.zoom = Math.min(1.6, Math.max(0.4, editor.zoom + delta));
    drawGraph();
  };
  n.root = h(
    "div",
    { class: "flows" },
    h(
      "div",
      { class: "view-intro" },
      h("p", { class: "lead" }, t("flows.lead")),
      h(
        "div",
        { class: "toolbar" },
        h(
          "div",
          { class: "segmented", role: "group" },
          n.modes.map(([button]) => button),
        ),
        n.template,
        h(
          "button",
          { type: "button", onclick: loadTemplate },
          t("flows.openTemplate"),
        ),
        n.versions,
      ),
    ),
    h(
      "div",
      { class: "editor-layout" },
      h(
        "section",
        { class: "panel editor" },
        h(
          "div",
          { class: "flow-meta" },
          labelled("flows.name", n.flowId),
          labelled("flows.entry", n.flowEntry),
          labelled("flows.calls", n.flowCalls),
          labelled("flows.planning", n.flowPlanning),
          labelled("flows.tokens", n.flowTokens),
        ),
        h(
          "div",
          { class: "graph-toolbar" },
          h(
            "span",
            { class: "legend", "aria-hidden": "true" },
            h("i", { class: "lg-ok" }),
            t("flows.legend.success"),
            h("i", { class: "lg-alt" }),
            t("flows.legend.failure"),
            h("i", { class: "lg-gate" }),
            t("flows.legend.gate"),
          ),
          h("span", { class: "spacer" }),
          h(
            "button",
            {
              type: "button",
              class: "icon-button",
              "aria-label": t("flows.zoomOut"),
              onclick: zoom(-0.1),
            },
            "−",
          ),
          h(
            "button",
            {
              type: "button",
              class: "icon-button",
              "aria-label": t("flows.zoomIn"),
              onclick: zoom(0.1),
            },
            "+",
          ),
          h("button", { type: "button", onclick: fit }, t("flows.fit")),
        ),
        n.graph,
        h("p", { class: "hint edit-only" }, t("flows.graphHint")),
        h("p", { class: "hint view-only" }, t("flows.viewHint")),
        h(
          "details",
          { class: "edge-details edit-only" },
          h("summary", {}, t("flows.manualEdge")),
          h(
            "form",
            {
              id: "edge-form",
              class: "edge-editor",
              onsubmit: (e) => {
                e.preventDefault();
                connect(
                  n.edgeSource.value,
                  n.edgeOutcome.value,
                  n.edgeTarget.value,
                );
              },
            },
            labelled("flows.edgeFrom", n.edgeSource),
            labelled("flows.edgeOutcome", n.edgeOutcome),
            labelled("flows.edgeTo", n.edgeTarget),
            h("button", { type: "submit" }, t("flows.connect")),
            h(
              "button",
              { type: "button", id: "edge-delete", onclick: removeEdge },
              t("flows.deleteEdge"),
            ),
          ),
        ),
        h(
          "div",
          { class: "form-actions" },
          h(
            "button",
            {
              type: "button",
              id: "add-step",
              class: "edit-only",
              onclick: addStep,
            },
            t("flows.addStep"),
          ),
          n.saved,
          h("span", { class: "spacer" }),
          h(
            "button",
            { type: "button", id: "validate", onclick: () => check(false) },
            t("flows.validate"),
          ),
          h(
            "button",
            {
              type: "button",
              id: "publish",
              class: "primary edit-only",
              onclick: () => check(true),
            },
            t("flows.publish"),
          ),
        ),
        h("p", { class: "hint" }, t("flows.publishHint")),
      ),
      h(
        "section",
        { class: "panel inspector" },
        h("span", { class: "eyebrow" }, t("flows.inspector")),
        n.inspectorTitle,
        buildInspector(() => drawGraph()),
        h(
          "div",
          { class: "form-actions edit-only" },
          h(
            "button",
            {
              type: "button",
              id: "delete-step",
              class: "danger-soft",
              onclick: deleteStep,
            },
            t("flows.deleteStep"),
          ),
        ),
        h(
          "details",
          {},
          h("summary", {}, t("flows.json")),
          n.json,
          h(
            "button",
            {
              type: "button",
              id: "import-json",
              class: "edit-only",
              onclick: async () => {
                try {
                  const flow = JSON.parse(n.json.value);
                  editor.dirty = false;
                  await replaceDraft(flow, t("flows.imported"));
                } catch (error) {
                  toastError(error);
                }
              },
            },
            t("flows.importJson"),
          ),
        ),
      ),
    ),
    h("datalist", { id: "profile-names" }),
    h("datalist", { id: "handler-names" }),
  );
  return n.root;
}

function fillLists() {
  const profiles = document.getElementById("profile-names");
  const handlers = document.getElementById("handler-names");
  if (profiles)
    replace(
      profiles,
      ["default", ...profileNames()].map((name) =>
        h("option", { value: name }),
      ),
    );
  if (handlers)
    replace(
      handlers,
      (store.state?.profiles || []).map((p) => h("option", { value: p.id })),
    );
  replace(
    nodes.versions,
    h("option", { value: "" }, t("flows.versionsPlaceholder")),
    (store.state?.definitions || []).map((d) =>
      h(
        "option",
        { value: d.digest },
        `${d.workflow.id} · ${d.digest.slice(0, 8)}`,
      ),
    ),
  );
}

function draw() {
  if (!root || !editor.flow) return;
  applyMode();
  fillMeta();
  drawGraph();
  fillEdges();
  fillInspector();
  nodes.json.value = JSON.stringify(editor.flow, null, 2);
  showSaved();
}

async function ensureDraft() {
  if (editor.flow) return;
  const saved = recall("flow-draft", null);
  if (saved?.flow) {
    editor.flow = saved.flow;
    editor.saved = saved.saved;
    editor.dirty = !!saved.saved;
    if (editor.dirty) editor.editing = true;
    return;
  }
  try {
    editor.flow = await api.post("template", {
      name: "main-flow",
      project: currentProject()?.id || "",
      language: window.ffaiPreferences.language,
    });
  } catch (error) {
    toastError(error);
  }
}

registerView({
  id: "flows",
  order: 3,
  title: "nav.flows",
  glyph: "⇢",
  async mount(container) {
    root = container;
    replace(root, build());
    fillLists();
    await ensureDraft();
    draw();
    requestAnimationFrame(fit);
  },
  update(_, reason) {
    if (!root) return;
    fillLists();
    if (reason === "language") {
      replace(root, build());
      fillLists();
      draw();
    }
  },
  unmount() {
    root = null;
  },
});

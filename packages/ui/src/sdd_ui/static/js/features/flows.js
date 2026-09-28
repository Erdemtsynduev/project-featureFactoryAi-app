/* Workflow editor. The draft lives in this browser and is saved on every edit;
 * inspector fields apply as soon as they change, so switching steps never drops
 * an edit. Validation and publication are explicit: publishing creates an
 * immutable version, and existing tasks keep the version they were created with.
 *
 * Tasks do not need a hand-published flow: creating a task by its kind
 * publishes the project's version of the template. The editor is for custom
 * flows and for inspecting what each step does. */

import * as api from "../core/api.js";
import { h, replace } from "../core/dom.js";
import { formatTime, t } from "../core/i18n.js";
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
import { stepName } from "./vocabulary.js";

const TEMPLATES = [
  "main-flow",
  "requirement",
  "ticket",
  "feature",
  "interview",
  "demo",
];
const KINDS = ["agent", "check", "human", "condition", "operation", "finish"];

const editor = {
  flow: null,
  selected: 0,
  zoom: 1,
  connecting: null,
  selectedEdge: null,
  size: { width: 1, height: 1 },
  saved: null, // time of the last local save
  dirty: false, // edited since loaded or published
};
let root = null;
let nodes = {};

/* Draft persistence ------------------------------------------------------------ */

function persist() {
  editor.saved = Date.now() / 1000;
  editor.dirty = true;
  remember("flow-draft", { flow: editor.flow, saved: editor.saved });
  showSaved();
}

function showSaved() {
  if (nodes.saved)
    nodes.saved.textContent = editor.saved
      ? t("flows.savedAt", { time: formatTime(editor.saved) })
      : "";
}

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
  });
  if (focused)
    (
      nodes.graph.querySelector(`[aria-label="${CSS.escape(focused)}"]`) ||
      nodes.graph
    ).focus();
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

/* Inspector -------------------------------------------------------------------- */

function input(name, attrs = {}) {
  return h("input", { name, ...attrs });
}

function buildInspector() {
  const fields = {
    id: input("id", { required: true, spellcheck: "false" }),
    kind: h(
      "select",
      { name: "kind" },
      KINDS.map((k) => h("option", { value: k }, t("kindStep." + k))),
    ),
    profile: input("profile", {
      list: "profile-names",
      placeholder: "default",
      spellcheck: "false",
    }),
    handler: input("handler", { list: "handler-names", spellcheck: "false" }),
    timeout: input("timeout", { type: "number", min: "1" }),
    max_visits: input("max_visits", { type: "number", min: "1" }),
    transitions: input("transitions", {
      spellcheck: "false",
      placeholder: "done=review, failed=diagnose",
    }),
    condition_key: input("condition_key", {
      placeholder: "answer.mode",
      spellcheck: "false",
    }),
    condition_value: input("condition_value"),
    prompt: h("textarea", { name: "prompt", rows: "8" }),
    config: h("textarea", { name: "config", rows: "3", spellcheck: "false" }),
    required: input("required", { type: "checkbox" }),
    gate: input("gate", { type: "checkbox" }),
    mutates: input("mutates", { type: "checkbox" }),
  };
  const label = (key, node, wide) =>
    h(
      "label",
      { class: wide ? "field wide" : "field" },
      h("span", {}, t("flows.field." + key)),
      node,
    );
  const form = h(
    "form",
    {
      id: "step-form",
      class: "form-grid",
      onsubmit: (e) => e.preventDefault(),
      onchange: applyStep,
    },
    label("id", fields.id),
    label("kind", fields.kind),
    label("profile", fields.profile),
    label("handler", fields.handler),
    label("timeout", fields.timeout),
    label("max_visits", fields.max_visits),
    label("transitions", fields.transitions, true),
    label("condition_key", fields.condition_key),
    label("condition_value", fields.condition_value),
    label("prompt", fields.prompt, true),
    label("config", fields.config, true),
    h(
      "div",
      { class: "wide checks" },
      ["required", "gate", "mutates"].map((key) =>
        h(
          "label",
          { title: t(`flows.field.${key}Hint`) },
          fields[key],
          h("span", {}, t("flows.field." + key)),
        ),
      ),
    ),
  );
  nodes.stepFields = fields;
  return form;
}

function fillInspector() {
  const step = editor.flow.steps[editor.selected];
  nodes.inspectorTitle.textContent = step
    ? `${stepName(step)} · ${step.kind}`
    : t("flows.pickStep");
  if (!step) return;
  const f = nodes.stepFields;
  for (const key of [
    "id",
    "kind",
    "handler",
    "timeout",
    "max_visits",
    "condition_key",
    "condition_value",
    "prompt",
    "config",
  ])
    if (document.activeElement !== f[key]) f[key].value = step[key] ?? "";
  f.profile.value = step.profile || "default";
  f.transitions.value = step.transitions
    .map((pair) => pair.join("="))
    .join(", ");
  for (const key of ["required", "gate", "mutates"])
    f[key].checked = !!step[key];
}

function applyStep() {
  const flow = editor.flow;
  const old = flow.steps[editor.selected];
  if (!old) return;
  const f = nodes.stepFields;
  try {
    const id = f.id.value.trim();
    if (!id) throw Error(t("flows.idRequired"));
    if (flow.steps.some((s, i) => i !== editor.selected && s.id === id))
      throw Error(t("flows.idTaken"));
    JSON.parse(f.config.value || "{}");
    const transitions = f.transitions.value.trim()
      ? f.transitions.value.split(",").map((part) => {
          const pair = part.split("=").map((s) => s.trim());
          if (pair.length !== 2 || !pair[0] || !pair[1])
            throw Error(t("flows.transitionFormat"));
          return pair;
        })
      : [];
    flow.steps[editor.selected] = {
      ...old,
      id,
      kind: f.kind.value,
      handler: f.handler.value.trim(),
      profile: f.profile.value.trim() || "default",
      timeout: Number(f.timeout.value) || old.timeout,
      max_visits: Number(f.max_visits.value) || old.max_visits,
      transitions,
      condition_key: f.condition_key.value,
      condition_value: f.condition_value.value,
      prompt: f.prompt.value,
      config: f.config.value || "{}",
      required: f.required.checked,
      gate: f.gate.checked,
      mutates: f.mutates.checked,
    };
    if (old.id !== id) renameStep(old.id, id);
    persist();
    drawGraph();
    fillEdges();
    nodes.json.value = JSON.stringify(flow, null, 2);
    nodes.inspectorTitle.textContent = `${stepName(flow.steps[editor.selected])} · ${f.kind.value}`;
  } catch (error) {
    toastError(error);
  }
}

function renameStep(from, to) {
  const flow = editor.flow;
  for (const step of flow.steps) {
    step.transitions = step.transitions.map(([o, target]) => [
      o,
      target === from ? to : target,
    ]);
    const config = JSON.parse(step.config || "{}");
    if (config.recovery_step === from) {
      config.recovery_step = to;
      step.config = JSON.stringify(config);
    }
  }
  if (flow.entry === from) flow.entry = to;
}

function fillEdges() {
  const options = () =>
    editor.flow.steps.map((s) => h("option", { value: s.id }, stepName(s)));
  for (const select of [nodes.edgeSource, nodes.edgeTarget]) {
    const value = select.value;
    replace(select, options());
    if (editor.flow.steps.some((s) => s.id === value)) select.value = value;
  }
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
  const n = (nodes = {});
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
      const item = store.state.definitions.find(
        (d) => d.digest === e.target.value,
      );
      e.target.value = "";
      if (item) await replaceDraft(item.workflow, t("flows.versionOpened"));
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
  const zoom = (delta) => () => {
    editor.zoom = Math.min(1.6, Math.max(0.4, editor.zoom + delta));
    drawGraph();
  };
  return h(
    "div",
    { class: "flows" },
    h(
      "div",
      { class: "view-intro" },
      h("p", { class: "lead" }, t("flows.lead")),
      h(
        "div",
        { class: "toolbar" },
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
        h("p", { class: "hint" }, t("flows.graphHint")),
        h(
          "details",
          { class: "edge-details" },
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
            { type: "button", id: "add-step", onclick: addStep },
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
              class: "primary",
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
        buildInspector(),
        h(
          "div",
          { class: "form-actions" },
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

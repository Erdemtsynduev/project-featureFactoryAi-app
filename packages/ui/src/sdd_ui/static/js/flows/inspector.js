/* Step inspector: every field applies as soon as it changes; renaming a step
 * rewires its incoming links and recovery references. */

import { h, replace } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { toastError } from "../ui/toast.js";
import { stepName } from "../features/vocabulary.js";
import { editor, KINDS, nodes, persist } from "./state.js";

function input(name, attrs = {}) {
  return h("input", { name, ...attrs });
}

export function buildInspector(onApplied) {
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
      onchange: () => applyStep(onApplied),
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

export function fillInspector() {
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

function applyStep(onApplied) {
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
    onApplied();
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

export function fillEdges() {
  const options = () =>
    editor.flow.steps.map((s) => h("option", { value: s.id }, stepName(s)));
  for (const select of [nodes.edgeSource, nodes.edgeTarget]) {
    const value = select.value;
    replace(select, options());
    if (editor.flow.steps.some((s) => s.id === value)) select.value = value;
  }
}

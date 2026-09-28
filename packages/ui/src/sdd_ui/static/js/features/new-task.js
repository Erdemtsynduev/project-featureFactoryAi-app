/* New task dialog. The operator picks what kind of work it is, not a workflow
 * digest: the server builds the project's version of that flow and publishes
 * it on first use. Everything typed is kept as a draft per project until the
 * task is created, so closing the dialog or reloading loses nothing. */

import * as api from "../core/api.js";
import { byId, h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { recall, remember } from "../core/storage.js";
import {
  currentProject,
  LOOSE,
  refresh,
  runs,
  selectProject,
  store,
  titleOf,
} from "../core/store.js";
import { confirmDialog, openDialog } from "../ui/dialog.js";
import { bindDraft } from "../ui/draft.js";
import { toast, toastError } from "../ui/toast.js";
import { go, openTask } from "./shell.js";

const INTENTS = ["feature", "main-flow", "ticket", "custom"];

function intentCard(intent, selected, missing) {
  const input = h("input", {
    type: "radio",
    name: "intent",
    value: intent,
    checked: intent === selected,
  });
  return h(
    "label",
    { class: "intent-card" + (missing?.length ? " is-missing" : "") },
    input,
    h(
      "span",
      { class: "intent-body" },
      h("strong", {}, t(`intent.${intent}.title`)),
      h("span", { class: "intent-text" }, t(`intent.${intent}.text`)),
      h("span", { class: "intent-path" }, t(`intent.${intent}.path`)),
      missing?.length
        ? h(
            "span",
            { class: "intent-warning" },
            t("intent.missing", { profiles: missing.join(", ") }),
          )
        : null,
    ),
  );
}

function field(label, input, hint, wide = false) {
  return h(
    "label",
    { class: wide ? "field wide" : "field" },
    h("span", {}, label),
    input,
    hint ? h("small", { class: "hint" }, hint) : null,
  );
}

export function openNewTask(preset = {}) {
  const project = currentProject();
  if (!project) return null;
  const readiness = store.state.intents || {};
  const remembered = recall("last-intent", "feature");
  // An intent remembered under an earlier name falls back to a feature.
  const chosen =
    preset.intent || (INTENTS.includes(remembered) ? remembered : "feature");
  const intents = h(
    "fieldset",
    { class: "intents wide" },
    h("legend", {}, t("task.kindQuestion")),
    INTENTS.map((intent) => intentCard(intent, chosen, readiness[intent])),
  );
  const definition = h(
    "select",
    { name: "definition" },
    store.state.definitions.map((d) =>
      h(
        "option",
        { value: d.digest },
        `${d.workflow.id} · ${d.digest.slice(0, 8)}`,
      ),
    ),
  );
  const customField = field(
    t("task.definition"),
    definition,
    t("task.definitionHint"),
    true,
  );
  const title = h("input", {
    name: "title",
    required: true,
    autocomplete: "off",
    placeholder: t("task.titlePlaceholder"),
  });
  const context = h("textarea", {
    name: "context",
    required: true,
    rows: "7",
    placeholder: t("task.contextPlaceholder"),
  });
  const open = runs().filter((r) => r.status !== "accepted");
  const dependencies = h(
    "select",
    {
      name: "dependencies",
      multiple: true,
      size: String(Math.min(5, Math.max(2, open.length))),
    },
    open.map((r) => h("option", { value: r.id }, titleOf(r))),
  );
  const language = h(
    "select",
    { name: "language" },
    h("option", { value: "ru" }, "Русский"),
    h("option", { value: "en" }, "English"),
  );
  language.value = project?.language || window.ffaiPreferences.language;
  const identifier = h("input", {
    name: "id",
    pattern: "[A-Za-z0-9_\-]{1,96}",
    placeholder: t("task.idPlaceholder"),
  });
  const start = h("input", {
    type: "checkbox",
    name: "start",
    checked: recall("start-after-create", false),
  });
  const next = h("section", {
    class: "wide next-steps",
    "aria-live": "polite",
  });
  const form = h(
    "form",
    { class: "form-grid", id: "task-form" },
    intents,
    next,
    customField,
    field(t("task.title"), title, "", true),
    field(t("task.context"), context, t("task.contextHint"), true),
    open.length
      ? field(
          t("task.dependencies"),
          dependencies,
          t("task.dependenciesHint"),
          true,
        )
      : null,
    field(t("task.language"), language),
    h(
      "details",
      { class: "wide advanced" },
      h("summary", {}, t("task.advanced")),
      field(t("task.id"), identifier, t("task.idHint")),
    ),
    h(
      "label",
      { class: "wide check-row" },
      start,
      h("span", {}, t("task.startNow")),
    ),
  );
  const syncIntent = () => {
    const intent = form.elements.intent.value;
    explain(next, intent, project);
    customField.hidden = intent !== "custom";
    definition.required = intent === "custom";
  };
  form.addEventListener("change", syncIntent);
  const draftState = h("small", { class: "draft-state" });
  const draftKey = "task:" + project.id;
  const draft = bindDraft(form, draftKey, {
    onState: (saved) => {
      draftState.textContent = saved ? t("draft.saved") : "";
      reset.hidden = !saved;
    },
  });
  const reset = h(
    "button",
    {
      type: "button",
      class: "ghost",
      hidden: true,
      onclick: async () => {
        if (
          !(await confirmDialog({
            title: t("draft.discardTitle"),
            message: t("draft.discardText"),
            confirm: t("draft.discard"),
            danger: true,
          }))
        )
          return;
        form.reset();
        language.value = project?.language || window.ffaiPreferences.language;
        draft.clear();
        syncIntent();
      },
    },
    t("draft.discard"),
  );
  draft.restore();
  if (preset.intent) form.elements.intent.value = preset.intent;
  syncIntent();
  const submit = h(
    "button",
    { type: "submit", class: "primary", form: "task-form" },
    t("task.create"),
  );
  const dialog = openDialog({
    title: t("task.newIn", { project: project.name }),
    subtitle: t("task.subtitle"),
    size: "wide",
    body: [form],
    footer: [
      draftState,
      reset,
      h("span", { class: "spacer" }),
      h(
        "button",
        { type: "button", onclick: () => dialog.close() },
        t("action.cancel"),
      ),
      submit,
    ],
  });
  form.onsubmit = async (event) => {
    event.preventDefault();
    submit.disabled = true;
    const intent = form.elements.intent.value;
    const body = {
      project: project.id,
      title: title.value.trim(),
      context: context.value.trim(),
      language: language.value,
      id: identifier.value.trim(),
      dependencies: [...dependencies.selectedOptions].map((o) => o.value),
    };
    if (intent === "custom") body.definition = definition.value;
    else body.intent = intent;
    try {
      const run = await api.post("create", body);
      remember("last-intent", intent);
      remember("start-after-create", start.checked);
      if (start.checked) await api.command("resume", run);
      draft.clear();
      dialog.close(true);
      await refresh();
      toast(start.checked ? t("task.createdQueued") : t("task.createdPaused"), {
        tone: "success",
      });
      openTask(run.id);
    } catch (error) {
      if (/agent profile/i.test(error.message))
        toast(t("task.needsAgents"), { tone: "error" });
      toastError(error);
    } finally {
      submit.disabled = false;
    }
  };
  title.focus();
  return dialog;
}

/** "What happens next" for the chosen kind, as numbered steps in this project. */
function explain(box, intent, project) {
  const steps = t(`intent.${intent}.steps`).split(" | ");
  box.replaceChildren(
    h("h3", {}, t("task.nextTitle")),
    h(
      "p",
      { class: "hint" },
      t("task.scope", { project: project.name, path: project.workspace }),
    ),
    h(
      "ol",
      { class: "stepper" },
      steps.map((text, index) =>
        h(
          "li",
          {},
          h("span", { class: "step-mark" }, index + 1),
          h("span", {}, text),
        ),
      ),
    ),
  );
}

export function startNewTask() {
  byId("new-task").onclick = () => openNewTask();
  document.addEventListener("ffai-demo", () => startDemo());
}

export async function startDemo() {
  try {
    const run = await api.post("interactive-demo", {
      language: window.ffaiPreferences.language,
    });
    await refresh();
    selectProject(LOOSE);
    go("board");
    openTask(run.id);
    toast(t("demo.noModel"));
  } catch (error) {
    toastError(error);
  }
}

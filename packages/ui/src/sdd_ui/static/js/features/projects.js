/* Add or edit a project in a dialog. A project is an existing folder with its
 * response language, check command and lane settings; registering it starts
 * nothing. The folder of an existing project cannot change. */

import * as api from "../core/api.js";
import { byId, h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { currentProject, refresh, selectProject } from "../core/store.js";
import { openDialog } from "../ui/dialog.js";
import { bindDraft } from "../ui/draft.js";
import { toast, toastError } from "../ui/toast.js";

function field(label, input, hint, wide = false) {
  return h(
    "label",
    { class: wide ? "field wide" : "field" },
    h("span", {}, label),
    input,
    hint ? h("small", { class: "hint" }, hint) : null,
  );
}

function idFrom(name) {
  return name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 48);
}

/** "python -m pytest" or a JSON array → argv; quotes keep spaces together. */
export function splitCommand(text) {
  const raw = text.trim();
  if (!raw) return [];
  if (raw.startsWith("[")) {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed) || !parsed.every((x) => typeof x === "string"))
      throw Error();
    return parsed;
  }
  return [...raw.matchAll(/"([^"]*)"|'([^']*)'|(\S+)/g)].map(
    (m) => m[1] ?? m[2] ?? m[3],
  );
}

export function joinCommand(argv) {
  return (argv || []).map((a) => (/\s/.test(a) ? `"${a}"` : a)).join(" ");
}

export function openProjectDialog(project = null) {
  const editing = !!project;
  const name = h("input", {
    name: "name",
    required: true,
    autocomplete: "off",
  });
  const id = h("input", {
    name: "id",
    required: true,
    pattern: "[A-Za-z0-9_\-]{1,64}",
    readOnly: editing,
  });
  const workspace = h("input", {
    name: "workspace",
    required: true,
    readOnly: editing,
    placeholder: t("project.workspacePlaceholder"),
    spellcheck: "false",
  });
  const language = h(
    "select",
    { name: "language" },
    h("option", { value: "ru" }, "Русский"),
    h("option", { value: "en" }, "English"),
  );
  const checks = h("input", {
    name: "checks",
    placeholder: t("project.checksPlaceholder"),
    spellcheck: "false",
  });
  const isolation = h("input", {
    type: "checkbox",
    name: "isolation",
    checked: true,
  });
  const autoResolve = h("input", {
    type: "checkbox",
    name: "auto_resolve",
    checked: true,
  });
  let idTouched = editing;
  id.addEventListener("input", () => (idTouched = true));
  name.addEventListener("input", () => {
    if (!idTouched) id.value = idFrom(name.value);
  });
  const form = h(
    "form",
    { class: "form-grid", id: "project-form" },
    field(t("project.name"), name),
    field(
      t("project.id"),
      id,
      editing ? t("project.idFixed") : t("project.idHint"),
    ),
    field(
      t("project.workspace"),
      workspace,
      editing ? t("project.workspaceFixed") : t("project.workspaceHint"),
      true,
    ),
    field(t("project.language"), language),
    field(t("project.checks"), checks, t("project.checksHint"), true),
    h(
      "fieldset",
      { class: "wide checks" },
      h("legend", {}, t("project.lanes")),
      h("label", {}, isolation, h("span", {}, t("project.isolation"))),
      h("label", {}, autoResolve, h("span", {}, t("project.autoResolve"))),
    ),
  );
  const draftState = h("small", { class: "draft-state" });
  const draft = bindDraft(
    form,
    editing ? "project:" + project.id : "project:new",
    {
      onState: (saved) =>
        (draftState.textContent = saved ? t("draft.saved") : ""),
    },
  );
  if (editing) {
    name.value = project.name;
    id.value = project.id;
    workspace.value = project.workspace;
    language.value = project.language || "ru";
    checks.value = joinCommand(project.checks);
    isolation.checked = project.isolation !== false;
    autoResolve.checked = project.auto_resolve !== false;
  } else {
    language.value = window.ffaiPreferences.language;
  }
  draft.restore();
  const save = h(
    "button",
    { type: "submit", class: "primary", form: "project-form" },
    t("action.save"),
  );
  const dialog = openDialog({
    title: editing
      ? t("project.editTitle", { name: project.name })
      : t("project.addTitle"),
    subtitle: t("project.subtitle"),
    body: [form],
    footer: [
      draftState,
      h("span", { class: "spacer" }),
      h(
        "button",
        { type: "button", onclick: () => dialog.close() },
        t("action.cancel"),
      ),
      save,
    ],
  });
  form.onsubmit = async (event) => {
    event.preventDefault();
    save.disabled = true;
    try {
      let parsed;
      try {
        parsed = splitCommand(checks.value);
      } catch {
        throw Error(t("project.checksInvalid"));
      }
      const result = await api.post("project", {
        id: id.value.trim(),
        name: name.value.trim(),
        workspace: workspace.value.trim(),
        language: language.value,
        checks: parsed,
        isolation: isolation.checked,
        auto_resolve: autoResolve.checked,
      });
      draft.clear();
      dialog.close(true);
      await refresh();
      selectProject(result.id);
      toast(t("project.saved", { name: result.name }), { tone: "success" });
    } catch (error) {
      toastError(error);
    } finally {
      save.disabled = false;
    }
  };
  name.focus();
  return dialog;
}

export function startProjects() {
  byId("project-add").onclick = () => openProjectDialog();
  byId("project-edit").onclick = () => {
    const project = currentProject();
    if (project) openProjectDialog(project);
  };
}

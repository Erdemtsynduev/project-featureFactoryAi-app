/* Many tasks at once, always through a dialog that says what will happen.
 *
 * "Resume" only allows a task to work; the queue takes it when a process slot
 * is free and its dependencies are accepted. Starting the queue while nothing
 * is resumed offers to resume the startable tasks first, because a running
 * queue with only paused tasks does nothing. */

import * as api from "../core/api.js";
import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import {
  boardFilter,
  currentProject,
  filteredRuns,
  refresh,
  runs,
  store,
} from "../core/store.js";
import { confirmDialog, openDialog } from "../ui/dialog.js";
import { toast, toastError } from "../ui/toast.js";

function counts() {
  const list = filteredRuns().filter(
    (r) => r.status !== "accepted" && !r.active,
  );
  const paused = list.filter((r) => r.paused && r.status !== "blocked");
  return {
    paused: paused.length,
    startable: paused.filter((r) => !r.pending_dependencies?.length).length,
    blocked: list.filter((r) => r.status === "blocked").length,
    moving: list.filter((r) => !r.paused).length,
  };
}

function budgetLeft() {
  const { totals, settings } = store.state;
  return Math.max(0, settings.max_calls - totals.calls);
}

function option(value, count, checked) {
  return h(
    "label",
    { class: "intent-card compact" },
    h("input", {
      type: "radio",
      name: "scope",
      value,
      checked,
      disabled: !count,
    }),
    h(
      "span",
      { class: "intent-body" },
      h("strong", {}, t(`bulk.scope.${value}.title`, { count })),
      h("span", { class: "intent-text" }, t(`bulk.scope.${value}.text`)),
    ),
  );
}

/** Resume tasks of the current project; `reason` explains why the dialog opened. */
export function openBulkResume(reason = "") {
  const project = currentProject();
  if (!project) return;
  const c = counts();
  const queuePaused = !store.state.settings.running;
  const startQueue = h("input", { type: "checkbox", checked: queuePaused });
  const form = h(
    "form",
    { id: "bulk-form", class: "form-grid" },
    reason ? h("p", { class: "attention tone-attention wide" }, reason) : null,
    h("p", { class: "wide lead" }, t("bulk.explain")),
    boardFilter.kind || boardFilter.plan
      ? h(
          "p",
          { class: "wide attention tone-idle" },
          t("bulk.filtered", {
            filter: [
              boardFilter.kind && t("kind.plural." + boardFilter.kind),
              boardFilter.plan,
            ]
              .filter(Boolean)
              .join(" · "),
          }),
        )
      : h("p", { class: "wide hint" }, t("bulk.filterHint")),
    h(
      "fieldset",
      { class: "intents wide single" },
      h("legend", {}, t("bulk.which")),
      option("startable", c.startable, c.startable > 0),
      option("all", c.paused, c.startable === 0 && c.paused > 0),
    ),
    h(
      "ul",
      { class: "wide facts-list" },
      h("li", {}, t("bulk.fact.slots")),
      h(
        "li",
        {},
        t("bulk.fact.budget", {
          left: budgetLeft(),
          max: store.state.settings.max_calls,
        }),
      ),
      c.blocked
        ? h("li", {}, t("bulk.fact.blocked", { count: c.blocked }))
        : null,
    ),
    queuePaused
      ? h(
          "label",
          { class: "wide check-row" },
          startQueue,
          h("span", {}, t("bulk.startQueue")),
        )
      : null,
  );
  const submit = h(
    "button",
    {
      type: "submit",
      class: "primary",
      form: "bulk-form",
      disabled: !c.paused,
    },
    t("bulk.resume"),
  );
  const dialog = openDialog({
    title: t("bulk.title", { project: project.name }),
    size: "wide",
    body: [form],
    footer: [
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
    try {
      const scope = form.elements.scope.value;
      const result = await api.post("resume-many", {
        project: project.id,
        scope,
      });
      if (startQueue.checked && queuePaused)
        await api.post("queue", { running: true });
      await refresh();
      dialog.close(true);
      toast(t("bulk.resumed", { count: result.changed.length }), {
        tone: "success",
      });
    } catch (error) {
      toastError(error);
      submit.disabled = false;
    }
  };
}

export async function pauseAll() {
  const project = currentProject();
  const c = counts();
  if (!project || !c.moving) return toast(t("bulk.nothingToPause"));
  const ok = await confirmDialog({
    title: t("bulk.pauseTitle"),
    message: t("bulk.pauseText", { count: c.moving, project: project.name }),
    confirm: t("bulk.pause"),
  });
  if (!ok) return;
  try {
    const result = await api.post("pause-many", {
      project: project.id,
      ...boardFilter,
    });
    await refresh();
    toast(t("bulk.paused", { count: result.changed.length }), {
      tone: "success",
    });
  } catch (error) {
    toastError(error);
  }
}

/** Start or pause the queue; starting with nothing resumed explains why first. */
export async function toggleQueue() {
  const running = store.state.settings.running;
  const resumed = runs().some((r) => !r.paused && r.status !== "accepted");
  if (!running && !resumed && currentProject() && counts().paused) {
    openBulkResume(t("bulk.nothingResumed"));
    return;
  }
  try {
    await api.post("queue", { running: !running });
    await refresh();
    if (!running && !resumed) toast(t("bulk.queueIdle"));
  } catch (error) {
    toastError(error);
  }
}

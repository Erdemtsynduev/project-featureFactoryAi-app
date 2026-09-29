/* Many tasks at once, always through a dialog that says what will happen.
 *
 * "Start" only allows a task to work; the queue takes it when a process slot
 * is free and its dependencies are accepted. A target narrows the tasks: the
 * board filter by default, one plan (its row on the board) or given task ids
 * (a task with its dependencies). The dialog lists prerequisites outside the
 * target and offers to start them too, because a plan whose tasks wait on
 * paused work elsewhere never moves. Starting the queue while nothing is
 * resumed offers to resume the startable tasks first. */

import * as api from "../core/api.js";
import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import {
  boardFilter,
  callsNeeded,
  currentProject,
  kindOf,
  meta,
  outsidePrerequisites,
  refresh,
  runs,
  store,
  titleOf,
} from "../core/store.js";
import { confirmDialog, openDialog } from "../ui/dialog.js";
import { toast, toastError } from "../ui/toast.js";
import { go } from "./shell.js";

const resumable = (r) =>
  r.paused &&
  r.status !== "blocked" &&
  r.status !== "accepted" &&
  !r.active;

/** What a bulk action applies to: explicit ids, one plan, or the board filter. */
function selection(target) {
  const filter = target
    ? { kind: "", plan: target.plan || "", ids: target.ids || [] }
    : { kind: boardFilter.kind, plan: boardFilter.plan, ids: [] };
  const list = runs().filter(
    (run) =>
      (!filter.kind || kindOf(run) === filter.kind) &&
      (!filter.plan || meta(run).plan === filter.plan) &&
      (!filter.ids.length || filter.ids.includes(run.id)),
  );
  return { filter, list };
}

/** `ids`: tasks the operator named; only those can start a ticket that needs a person. */
function counts(list, ids = []) {
  const open = list.filter((r) => r.status !== "accepted" && !r.active);
  const held = (r) => r.ticket?.hitl && !ids.includes(r.id);
  const waiting = open.filter((r) => r.paused && r.status !== "blocked");
  const paused = waiting.filter((r) => !held(r));
  return {
    paused,
    hitl: waiting.length - paused.length,
    startable: paused.filter((r) => !r.pending_dependencies?.length),
    blocked: open.filter((r) => r.status === "blocked").length,
    moving: open.filter((r) => !r.paused).length,
  };
}

/** Calls left under an optional queue cap; no cap (subscriptions) leaves no limit. */
function budgetLeft() {
  const { totals, settings } = store.state;
  return settings.max_calls == null
    ? Infinity
    : Math.max(0, settings.max_calls - totals.calls);
}

const shown = (value) => (value == null || value === Infinity ? "∞" : value);

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

function dialogTitle(target, project) {
  if (target?.ids?.length) {
    const task = target.title || target.ids[0];
    return t("bulk.taskTitle", {
      task: task.length > 60 ? task.slice(0, 59) + "…" : task,
    });
  }
  if (target?.plan)
    return t("bulk.planTitle", { plan: target.title || target.plan });
  return t("bulk.title", { project: project.name });
}

/** Resume tasks of the current project; `reason` explains why the dialog opened. */
export function openBulkResume(reason = "", target = null) {
  const project = currentProject();
  if (!project) return;
  const { filter, list } = selection(target);
  const c = counts(list, filter.ids);
  const queuePaused = !store.state.settings.running;
  const startQueue = h("input", { type: "checkbox", checked: queuePaused });
  const withDependencies = h("input", { type: "checkbox", checked: !!target });
  const dependencies = h("div", { class: "wide" });
  const budget = h("ul", { class: "wide facts-list" });
  const scopeNote =
    !target && (boardFilter.kind || boardFilter.plan)
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
      : target
        ? null
        : h("p", { class: "wide hint" }, t("bulk.filterHint"));
  const form = h(
    "form",
    { id: "bulk-form", class: "form-grid", onchange: () => update() },
    reason ? h("p", { class: "attention tone-attention wide" }, reason) : null,
    h("p", { class: "wide lead" }, t("bulk.explain")),
    scopeNote,
    h(
      "fieldset",
      { class: "intents wide single" },
      h("legend", {}, t("bulk.which")),
      option("startable", c.startable.length, c.startable.length > 0),
      option(
        "all",
        c.paused.length,
        c.startable.length === 0 && c.paused.length > 0,
      ),
    ),
    dependencies,
    budget,
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
    },
    t("bulk.resume"),
  );
  const scope = () => form.elements.scope?.value || "startable";
  // Prerequisites outside the target that its unfinished tasks wait for.
  const found = outsidePrerequisites(
    list.filter((r) => r.status !== "accepted"),
  );
  const external = found.filter(resumable);
  submit.disabled = !c.paused.length && !external.length;
  const outside = () => ({
    startable: external,
    blocked: found.filter((r) => r.status === "blocked").length,
    chosen: scope() === "all" ? c.paused : c.startable,
  });
  function update() {
    const o = outside();
    dependencies.replaceChildren(
      ...(o.startable.length
        ? [
            h(
              "label",
              { class: "check-row" },
              withDependencies,
              h(
                "span",
                {},
                h(
                  "strong",
                  {},
                  t("bulk.deps.title", { count: o.startable.length }),
                ),
                h("br"),
                h("small", { class: "hint" }, t("bulk.deps.text")),
              ),
            ),
            h(
              "ul",
              { class: "dep-list" },
              o.startable
                .slice(0, 5)
                .map((r) =>
                  h(
                    "li",
                    {},
                    meta(r).plan
                      ? h(
                          "span",
                          { class: "plan-chip" },
                          t("card.plan", { id: meta(r).plan }),
                        )
                      : null,
                    " ",
                    titleOf(r),
                  ),
                ),
              o.startable.length > 5
                ? h(
                    "li",
                    { class: "hint" },
                    t("attention.dependenciesMore", {
                      count: o.startable.length - 5,
                    }),
                  )
                : null,
            ),
          ]
        : []),
      ...(o.blocked
        ? [
            h(
              "p",
              { class: "attention tone-blocked" },
              t("bulk.deps.blocked", { count: o.blocked }),
            ),
          ]
        : []),
    );
    const starting = [
      ...o.chosen,
      ...(withDependencies.checked ? o.startable : []),
    ];
    const need = callsNeeded(starting);
    const { totals, settings } = store.state;
    const left = budgetLeft();
    const planningLeft =
      settings.max_planning_calls == null
        ? Infinity
        : Math.max(0, settings.max_planning_calls - totals.planning_calls);
    const short = need.calls > left || need.planning > planningLeft;
    const features = starting.filter(
      (r) => kindOf(r) === "feature",
    ).length;
    const facts = [
      h("li", {}, t("bulk.fact.slots")),
      h(
        "li",
        { class: short ? "warn" : "" },
        t("bulk.estimate", {
          calls: need.calls,
          planning: need.planning,
          left: shown(left),
          max: shown(settings.max_calls),
          pleft: shown(planningLeft),
          pmax: shown(settings.max_planning_calls),
        }),
        short ? " " : null,
        short
          ? h(
              "button",
              {
                type: "button",
                class: "link",
                onclick: (e) => raiseBudget(e.currentTarget, need),
              },
              t("bulk.raiseTo", {
                calls: totals.calls + need.calls,
                planning: totals.planning_calls + need.planning,
              }),
            )
          : null,
      ),
      features
        ? h("li", {}, t("bulk.approvals", { count: features }))
        : null,
      c.blocked
        ? h("li", {}, t("bulk.fact.blocked", { count: c.blocked }))
        : null,
      c.hitl ? h("li", { class: "warn" }, t("bulk.fact.hitl", { count: c.hitl })) : null,
    ];
    budget.replaceChildren(...facts.filter(Boolean));
  }
  /** Raise the queue budgets just enough for this selection; an explicit click. */
  async function raiseBudget(button, need) {
    button.disabled = true;
    const { totals, settings } = store.state;
    try {
      await api.post("budget", {
        max_calls:
          settings.max_calls == null
            ? null
            : Math.max(settings.max_calls, totals.calls + need.calls),
        max_planning_calls:
          settings.max_planning_calls == null
            ? null
            : Math.max(settings.max_planning_calls, totals.planning_calls + need.planning),
      });
      await refresh();
      toast(t("bulk.raised"), { tone: "success" });
      update();
    } catch (error) {
      toastError(error);
      button.disabled = false;
    }
  }
  const dialog = openDialog({
    title: dialogTitle(target, project),
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
  update();
  form.onsubmit = async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      const result = await api.post("resume-many", {
        project: project.id,
        scope: scope(),
        ...filter,
        with_dependencies: withDependencies.checked && external.length > 0,
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

/** Pause the board filter's tasks, or one plan's (`target.plan`). */
export async function pauseAll(target = null) {
  const project = currentProject();
  const { filter, list } = selection(target);
  const c = counts(list, filter.ids);
  if (!project || !c.moving) return toast(t("bulk.nothingToPause"));
  const ok = await confirmDialog({
    title: t("bulk.pauseTitle"),
    message: t("bulk.pauseScope", {
      count: c.moving,
      scope: target?.title || project.name,
    }),
    confirm: target ? t("plans.pause") : t("bulk.pause"),
  });
  if (!ok) return;
  try {
    const result = await api.post("pause-many", {
      project: project.id,
      ...filter,
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
  const { list } = selection(null);
  if (!running && !resumed && currentProject() && counts(list).paused.length) {
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

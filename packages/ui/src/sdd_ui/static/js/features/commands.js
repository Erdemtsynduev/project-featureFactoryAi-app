/* Task commands available in the current state. Cards show the primary one;
 * the drawer shows all. Commands carry the version the operator saw, so a
 * command on a task that moved meanwhile is rejected instead of misapplied. */

import * as api from "../core/api.js";
import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { refresh, run as findRun, stepOf, store } from "../core/store.js";
import { confirmDialog } from "../ui/dialog.js";
import { attempt } from "../ui/toast.js";
import { go } from "./shell.js";
import { attentionText } from "./vocabulary.js";
import { resumeChildrenButton } from "./work.js";

function recoverable(run) {
  try {
    return !!JSON.parse(stepOf(run)?.config || "{}").recovery_step;
  } catch {
    return false;
  }
}

/** [command, label key, tone] in display order, filtered by state. */
export function availableCommands(run) {
  if (run.status === "accepted") return [];
  const list = [];
  if (run.paused && run.status !== "blocked")
    list.push(["resume", "command.resume", "primary-soft"]);
  if (run.status === "blocked" && !run.active)
    list.push(["retry", "command.retry", "primary"]);
  if (
    !run.active &&
    ["blocked", "waiting"].includes(run.status) &&
    recoverable(run)
  )
    list.push(["recover", "command.recover", ""]);
  if (!run.paused) list.push(["pause", "command.pause", ""]);
  if (run.active && stepOf(run)?.kind !== "human")
    list.push(["stop", "command.stop", "danger"]);
  return list;
}

export async function runCommand(name, run) {
  return attempt(
    async () => {
      await api.command(name, run);
      await refresh();
    },
    t("command.done." + name),
  );
}

/** Start one task: resume it and, when the queue is paused, start the queue too.
 * Starting the queue also starts every other allowed task, so that case asks. */
export async function startTask(run) {
  const others = store.state.runs.filter(
    (r) =>
      r.id !== run.id &&
      !r.paused &&
      !r.active &&
      !["accepted", "blocked"].includes(r.status),
  ).length;
  let startQueue = !store.state.settings.running;
  if (startQueue && others)
    startQueue = await confirmDialog({
      title: t("start.queueTitle"),
      message: t("start.queueText", { count: others }),
      confirm: t("queue.start"),
      cancel: t("start.onlyTask"),
    });
  return attempt(
    async () => {
      await api.command("resume", run);
      if (startQueue) await api.post("queue", { running: true });
      await refresh();
      const moved = findRun(run.id);
      return moved ? attentionText(moved) : "";
    },
    (next) =>
      t(startQueue ? "command.done.start" : "command.done.resume") +
      (next ? " — " + next : ""),
  );
}

export function commandButton([name, label, tone], run, after) {
  return h(
    "button",
    {
      type: "button",
      class: tone,
      title: t(label + "Hint"),
      onclick: async (event) => {
        event.stopPropagation();
        event.currentTarget.disabled = true;
        await (name === "resume" ? startTask(run) : runCommand(name, run));
        after?.();
      },
    },
    t(label),
  );
}

/** What a card offers in one click, following the attention reason. */
export function primaryAction(run, open) {
  const action = run.attention?.action;
  if (action === "answer")
    return h(
      "button",
      {
        type: "button",
        class: "primary",
        onclick: (e) => (e.stopPropagation(), open()),
      },
      t("command.answer"),
    );
  if (action === "queue" && !store.state.settings.running)
    return h(
      "button",
      {
        type: "button",
        onclick: (e) => {
          e.stopPropagation();
          attempt(async () => {
            await api.post("queue", { running: true });
            await refresh();
          });
        },
      },
      t("queue.start"),
    );
  if (action === "resume_children") return resumeChildrenButton(run);
  if (action === "agents")
    return h(
      "button",
      { type: "button", onclick: (e) => (e.stopPropagation(), go("agents")) },
      t("command.agents"),
    );
  const command = availableCommands(run).find(([name]) => name === action);
  return command ? commandButton(command, run) : null;
}

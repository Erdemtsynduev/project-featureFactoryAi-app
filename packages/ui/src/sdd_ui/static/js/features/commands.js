/* Task commands available in the current state. Cards show the primary one;
 * the drawer shows all. Commands carry the version the operator saw, so a
 * command on a task that moved meanwhile is rejected instead of misapplied. */

import * as api from "../core/api.js";
import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { refresh, stepOf, store } from "../core/store.js";
import { attempt } from "../ui/toast.js";
import { go } from "./shell.js";

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
    list.push(["resume", "command.resume", "primary"]);
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
        await runCommand(name, run);
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
  if (action === "agents")
    return h(
      "button",
      { type: "button", onclick: (e) => (e.stopPropagation(), go("agents")) },
      t("command.agents"),
    );
  const command = availableCommands(run).find(([name]) => name === action);
  return command ? commandButton(command, run) : null;
}

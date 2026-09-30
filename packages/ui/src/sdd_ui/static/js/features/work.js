/* Work as a tree: a parent (a draft, a feature, or a ticket split further) is
 * delivered when all its children are. Its planning ends when its breakdown is
 * approved; after that the server derives its state from the children. A partly done parent is finished (resume the rest) or closed early:
 * its unfinished children become top-level work of their own. */

import * as api from "../core/api.js";
import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { childrenOf, kindOf, refresh, titleOf } from "../core/store.js";
import { confirmDialog } from "../ui/dialog.js";
import { attempt } from "../ui/toast.js";
import { openBulkResume } from "./bulk.js";

/** Every run below `run`, depth first; safe against cyclic records. */
export function descendantsOf(run, seen = new Set([run.id])) {
  const found = [];
  for (const child of childrenOf(run)) {
    if (seen.has(child.id)) continue;
    seen.add(child.id);
    found.push(child, ...descendantsOf(child, seen));
  }
  return found;
}

/** Whether a parent's planning is done and some of its children are not. */
export function closable(run) {
  const p = run.progress;
  return (
    !!p &&
    run.status === "accepted" &&
    p.done < p.total &&
    run.attention?.code !== "closed"
  );
}

/** Resume the unfinished work below a parent, with its prerequisites. */
export function finishWork(run) {
  const ids = descendantsOf(run)
    .filter((r) => r.status !== "accepted")
    .map((r) => r.id);
  return openBulkResume("", { ids, title: titleOf(run) });
}

export async function closeWork(run) {
  const { done, total } = run.progress;
  const ok = await confirmDialog({
    title: t("work.closeTitle", { title: titleOf(run) }),
    message: t("work.closeText", { done, total, rest: total - done }),
    confirm: t("work.close"),
  });
  if (!ok) return;
  await attempt(
    async () => {
      const result = await api.post("close", { id: run.id });
      await refresh();
      return result;
    },
    (r) => t("work.closed", { count: r.detached.length }),
  );
}

/** The one-click action of a parent waiting on paused children. */
export function resumeChildrenButton(run) {
  const label =
    run.attention?.code === "partial"
      ? t("work.finish")
      : kindOf(run) === "draft"
        ? t("work.startFeatures")
        : t("work.startTickets");
  return h(
    "button",
    {
      type: "button",
      class: "primary-soft",
      onclick: (e) => (e.stopPropagation(), finishWork(run)),
    },
    label,
  );
}

export function closeButton(run) {
  return h(
    "button",
    {
      type: "button",
      class: "ghost",
      title: t("work.closeHint"),
      onclick: (e) => (e.stopPropagation(), closeWork(run)),
    },
    t("work.close"),
  );
}

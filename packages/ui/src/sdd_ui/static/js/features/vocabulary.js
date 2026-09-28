/* Names for steps, roles and task states, plus the one-line "why" of a task.
 * Every view uses these helpers so a state reads the same everywhere. */

import { h } from "../core/dom.js";
import { formatTime, relativeTime, t } from "../core/i18n.js";

const KNOWN_STEPS = new Set([
  "spec",
  "tickets",
  "implement",
  "checks",
  "review",
  "diagnose",
  "repair",
  "reconcile",
  "interview",
  "approve",
  "accepted",
  "finish",
  "ask",
  "answer",
  "integrate",
  "rebase",
  "resolve",
  "merge_conflict",
]);

export function stepName(step) {
  const id = typeof step === "string" ? step : step?.id;
  if (!id) return "";
  if (KNOWN_STEPS.has(id)) return t("step." + id);
  if (/^check_\d+$/.test(id)) return t("step.check", { n: id.slice(6) });
  try {
    const title = JSON.parse(step?.config || "{}").title;
    if (title) return title;
  } catch {
    /* A malformed config only loses the friendly title. */
  }
  return id;
}

export function roleName(step) {
  if (step.kind === "human") return t("role.you");
  if (step.kind === "check") return t("role.tester");
  if (step.handler?.startsWith("lane-")) return t("role.integrator");
  const key = "role." + step.id;
  const role = t(key);
  return role === key ? stepName(step) : role;
}

export function kindLabel(kind) {
  return t("kind." + kind);
}

export const KIND_GLYPH = { requirement: "◇", ticket: "▣", task: "○" };

/** The server's attention reason as text, e.g. "Paused — press Resume". */
export function attentionText(run) {
  const a = run.attention || {};
  const params = {
    detail: a.detail || "",
    until: a.until ? formatTime(a.until) : "",
  };
  if (a.code === "waiting" && a.until) params.in = relativeTime(a.until);
  return t("attention." + (a.code || "queued"), params);
}

export function attentionTone(run) {
  return run.attention?.tone || "idle";
}

/** A status pill with the attention tone. */
export function statusPill(run) {
  return h(
    "span",
    { class: "pill tone-" + attentionTone(run) },
    t("tone." + attentionTone(run)),
  );
}

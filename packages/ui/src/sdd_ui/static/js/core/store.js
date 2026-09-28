/* Single source of UI state: the server snapshot plus the selected project.
 * Views subscribe and re-render; they never poll or mutate the snapshot. */

import { fetchState } from "./api.js";
import { recall, remember } from "./storage.js";

const listeners = new Set();
const POLL_MS = 3000;

export const store = {
  state: null,
  project: recall("project", null),
  connected: false,
  error: null,
};

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function emit(reason) {
  for (const listener of listeners) listener(store, reason);
}

export async function refresh() {
  try {
    const { state, changed } = await fetchState();
    const wasConnected = store.connected;
    store.state = state;
    store.connected = true;
    store.error = null;
    normalizeProject();
    if (changed || !wasConnected) emit("state");
  } catch (error) {
    store.connected = false;
    store.error = error.message;
    emit("connection");
  }
  return store.state;
}

let timer = null;
export function startPolling() {
  if (timer) return;
  timer = setInterval(refresh, POLL_MS);
}
export function stopPolling() {
  clearInterval(timer);
  timer = null;
}

/* Projects ------------------------------------------------------------------ */

/** Runs that belong to no registered project (demos, CLI-created tasks). */
export const LOOSE = "";

export function projects() {
  return store.state?.projects || [];
}

export function looseRuns() {
  return (store.state?.runs || []).filter((run) => projectOf(run) === LOOSE);
}

function projectOf(run) {
  const state = store.state;
  const declared = state.task_metadata?.[run.id]?.project;
  if (declared && state.projects.some((p) => p.id === declared))
    return declared;
  const location = state.locations?.[run.id];
  return state.projects.find((p) => p.workspace === location)?.id ?? LOOSE;
}

/** Keep the selection valid: a known project, loose tasks, or nothing at all. */
function normalizeProject() {
  const ids = projects().map((p) => p.id);
  const valid =
    ids.includes(store.project) ||
    (store.project === LOOSE && looseRuns().length);
  if (!valid) store.project = ids[0] ?? (looseRuns().length ? LOOSE : null);
}

export function selectProject(id) {
  store.project = id;
  remember("project", id);
  emit("project");
}

export function currentProject() {
  return projects().find((p) => p.id === store.project) || null;
}

export function hasScope() {
  return store.project !== null;
}

/* Runs ---------------------------------------------------------------------- */

export function runs() {
  if (!store.state || store.project === null) return [];
  return store.state.runs.filter((run) => projectOf(run) === store.project);
}

export function run(id) {
  return store.state?.runs.find((r) => r.id === id) || null;
}

export function meta(runOrId) {
  const id = typeof runOrId === "string" ? runOrId : runOrId.id;
  return store.state?.task_metadata?.[id] || {};
}

export function titleOf(run) {
  return meta(run).title || run.id;
}

export const KINDS = ["feature", "ticket", "task"];
export function kindOf(run) {
  const kind = meta(run).kind;
  return KINDS.includes(kind) ? kind : "task";
}

export function workflowOf(run) {
  return store.state?.definitions.find((d) => d.digest === run.workflow_digest)
    ?.workflow;
}

export function stepOf(run) {
  return workflowOf(run)?.steps.find((s) => s.id === run.step);
}

export function childrenOf(run) {
  return (store.state?.runs || []).filter((r) => meta(r).parent === run.id);
}

/** The board's kind and plan filter; bulk actions apply to the same tasks. */
export const boardFilter = { kind: "", plan: "" };

/** A plan's catalog entry: title, file path and row counts from the file. */
export function planInfo(id) {
  const plans = store.state?.plans || {};
  const list = plans[store.project] || Object.values(plans).flat();
  return list.find((p) => p.id === id) || null;
}

/** A plan's display name: "105 · Physics assemblies", or its id. */
export function planTitle(id) {
  return planInfo(id)?.title || id;
}

/** Tasks this one waits for (every prerequisite, accepted or not). */
export function prerequisitesOf(task) {
  return (task.dependencies || []).map((id) => ({ id, run: run(id) }));
}

/** Tasks that wait for this one. */
export function dependentsOf(task) {
  return (store.state?.runs || []).filter((r) =>
    r.dependencies?.includes(task.id),
  );
}

/** Unaccepted prerequisites of `list`, transitively, that are not in `list`. */
export function outsidePrerequisites(list) {
  const seen = new Set(list.map((r) => r.id));
  const found = [];
  const frontier = [...list];
  while (frontier.length)
    for (const id of frontier.pop().dependencies || []) {
      if (seen.has(id)) continue;
      seen.add(id);
      const dependency = run(id);
      if (!dependency || dependency.status === "accepted") continue;
      found.push(dependency);
      frontier.push(dependency);
    }
  return found;
}

/** Least model calls the tasks still need: an estimate for budgets. */
export function callsNeeded(list) {
  // Per-run estimates come from the server (sdd_ui.attention.calls_needed).
  return list.reduce(
    (sum, task) => ({
      calls: sum.calls + (task.needs?.calls || 0),
      planning: sum.planning + (task.needs?.planning || 0),
    }),
    { calls: 0, planning: 0 },
  );
}

/** Tasks that need the operator in other projects (or without a project). */
export function needsElsewhere() {
  if (!store.state) return [];
  const counts = new Map();
  for (const r of store.state.runs) {
    const owner = projectOf(r);
    if (owner === store.project || laneOf(r) !== "needs") continue;
    counts.set(owner, (counts.get(owner) || 0) + 1);
  }
  return [...counts].map(([id, count]) => ({
    id,
    count,
    name: projects().find((p) => p.id === id)?.name,
  }));
}

/** Board column of a task. */
export function laneOf(run) {
  // The server derives the lane with the attention reason (sdd_ui.attention.lane).
  return run.lane || "queue";
}

/* Profiles ------------------------------------------------------------------ */

export function profileNames() {
  return Object.keys(store.state?.profile_config?.profiles || {});
}

/** "claude · opus" for an agent step, the command for a check, "you" for a human. */
export function runnerOf(step, t) {
  if (!step) return "";
  if (step.kind === "human") return t("runner.you");
  if (step.kind === "check" || step.kind === "operation")
    return step.handler?.startsWith("lane-") ? "git" : t("runner.checks");
  if (step.kind !== "agent") return "";
  const name = step.profile !== "default" ? step.profile : step.handler;
  const profile = store.state?.profile_config?.profiles?.[name];
  return profile ? `${name} · ${profile.model}` : name;
}

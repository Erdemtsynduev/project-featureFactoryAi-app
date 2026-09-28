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

export const KINDS = ["requirement", "ticket", "task"];
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

export function filteredRuns() {
  return runs().filter(
    (run) =>
      (!boardFilter.kind || kindOf(run) === boardFilter.kind) &&
      (!boardFilter.plan || meta(run).plan === boardFilter.plan),
  );
}

/** Board column from the server's single attention reason. */
export function laneOf(run) {
  const tone = run.attention?.tone;
  if (tone === "done") return "done";
  if (tone === "attention" || tone === "blocked") return "needs";
  if (tone === "working") return "running";
  return "queue";
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

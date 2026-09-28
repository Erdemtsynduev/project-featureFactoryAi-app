/* Entry point: registers views in navigation order, then starts the frame,
 * the task drawer and polling. */

import { refresh, startPolling } from "./core/store.js";
import "./features/board.js";
import "./features/team.js";
import "./features/flows.js";
import "./features/agents.js";
import "./features/usage.js";
import "./features/journal.js";
import { startNewTask } from "./features/new-task.js";
import { startNotifications } from "./features/notifications.js";
import { startOnboarding } from "./features/onboarding.js";
import { startProjects } from "./features/projects.js";
import { startShell } from "./features/shell.js";
import { startTaskDrawer } from "./features/task-drawer.js";

async function start() {
  await refresh();
  startTaskDrawer();
  startShell();
  startProjects();
  startNewTask();
  startNotifications();
  startOnboarding();
  startPolling();
  document.documentElement.dataset.ready = "true";
}

start();

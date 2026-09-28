/* Entry point: registers views in navigation order, then starts the frame,
 * the task drawer and polling. */

import { refresh, startPolling } from "./core/store.js";
import "./features/board.js";
import "./features/dashboard.js";
import "./features/flows.js";
import "./features/agents.js";
import "./features/usage.js";
import "./features/journal.js";
import { startAbout } from "./features/about.js";
import { startNewTask } from "./features/new-task.js";
import { startNotifications } from "./features/notifications.js";
import { startOnboarding } from "./features/onboarding.js";
import { startProjects } from "./features/projects.js";
import { startShell } from "./features/shell.js";
import { startTaskDrawer } from "./features/task-drawer.js";
import { startTicker } from "./features/vocabulary.js";

async function start() {
  await refresh();
  startTaskDrawer();
  startShell();
  startProjects();
  startNewTask();
  startNotifications();
  startOnboarding();
  startAbout();
  startTicker();
  startPolling();
  document.documentElement.dataset.ready = "true";
}

start();

/* Optional browser notifications for new questions, blockers and finished tasks
 * while the page is open. Each event notifies once, also across reloads. */

import { byId } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { recall, remember } from "../core/storage.js";
import { store as storeState, subscribe, titleOf } from "../core/store.js";
import { toast } from "../ui/toast.js";
import { openTask } from "./shell.js";

const seen = new Set(recall("notifications-seen", []));
let primed = false;

function key(run) {
  const code = run.attention?.code;
  if (code === "answer") return run.id + ":answer:" + run.active?.id;
  if (run.attention?.tone === "blocked")
    return run.id + ":blocked:" + run.reason;
  if (code === "accepted") return run.id + ":accepted";
  return null;
}

function enabled() {
  return (
    recall("notifications", false) &&
    "Notification" in window &&
    Notification.permission === "granted"
  );
}

function check(store) {
  for (const run of store.state?.runs || []) {
    const id = key(run);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    if (!primed || !enabled()) continue;
    const title = t(
      "notify." +
        (run.attention.code === "answer"
          ? "answer"
          : run.attention.code === "accepted"
            ? "done"
            : "blocked"),
    );
    const note = new Notification(title, { body: titleOf(run), tag: id });
    note.onclick = () => {
      window.focus();
      openTask(run.id);
      note.close();
    };
  }
  primed = true;
  remember("notifications-seen", [...seen].slice(-300));
}

export function startNotifications() {
  subscribe((store, reason) => reason === "state" && check(store));
  check(storeState);
  byId("notifications").onclick = async () => {
    if (!("Notification" in window))
      return toast(t("notify.unsupported"), { tone: "error" });
    const permission = await Notification.requestPermission();
    remember("notifications", permission === "granted");
    toast(permission === "granted" ? t("notify.on") : t("notify.denied"), {
      tone: permission === "granted" ? "success" : "error",
    });
  };
}

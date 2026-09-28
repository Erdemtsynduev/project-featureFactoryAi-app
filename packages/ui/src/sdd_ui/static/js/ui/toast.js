/* Transient notices. Errors stay until dismissed; success fades on its own.
 * The region is aria-live so screen readers announce every notice. */

import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";

let region = null;

function host() {
  if (!region) {
    region = h("div", {
      class: "toasts",
      role: "status",
      "aria-live": "polite",
    });
    document.body.append(region);
  }
  return region;
}

export function toast(message, { tone = "info", timeout } = {}) {
  if (!message) return;
  const close = h(
    "button",
    { type: "button", class: "icon-button", "aria-label": t("action.dismiss") },
    "×",
  );
  const item = h(
    "div",
    { class: `toast toast-${tone}` },
    h("span", {}, message),
    close,
  );
  const dismiss = () => item.remove();
  close.onclick = dismiss;
  // Newest on top; keep the stack short.
  host().prepend(item);
  while (host().children.length > 4) host().lastChild.remove();
  const ms = timeout ?? (tone === "error" ? 0 : 4500);
  if (ms) setTimeout(dismiss, ms);
  return dismiss;
}

export const toastError = (error) =>
  toast(error?.message || String(error), { tone: "error" });

/** Run an async operator action; report failures instead of throwing. */
export async function attempt(action, success) {
  try {
    const result = await action();
    if (success)
      toast(typeof success === "function" ? success(result) : success, {
        tone: "success",
      });
    return result;
  } catch (error) {
    toastError(error);
    return undefined;
  }
}

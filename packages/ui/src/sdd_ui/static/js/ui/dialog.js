/* Modal dialogs and side drawers on the native <dialog> element.
 *
 * Every dialog closes the same three ways: the × button, Escape and a click on
 * the backdrop. A dialog may guard closing (`canClose`), e.g. to keep unsaved
 * input; drafts are saved separately so closing never loses typed text. */

import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";

const open = new Set();

/**
 * openDialog({ title, subtitle, body, footer, variant: "modal"|"drawer", onClose, canClose })
 * Returns { element, body, close, setTitle }.
 */
export function openDialog({
  title = "",
  subtitle = "",
  body = [],
  footer = [],
  variant = "modal",
  size = "",
  label = "",
  onClose = () => {},
  canClose = () => true,
} = {}) {
  const heading = h("h2", { class: "dialog-title" }, title);
  const sub = h("p", { class: "dialog-subtitle", hidden: !subtitle }, subtitle);
  const content = h("div", { class: "dialog-body" }, body);
  const actions = h(
    "footer",
    { class: "dialog-footer", hidden: !footer.length },
    footer,
  );
  const closer = h(
    "button",
    {
      type: "button",
      class: "icon-button dialog-close",
      "aria-label": t("action.close"),
      title: t("action.close"),
    },
    "×",
  );
  const element = h(
    "dialog",
    { class: `dialog dialog-${variant} ${size}`, "aria-label": label || title },
    h(
      "header",
      { class: "dialog-header" },
      h("div", { class: "dialog-heading" }, heading, sub),
      closer,
    ),
    content,
    actions,
  );
  let closed = false;
  const close = (force = false) => {
    if (closed || (!force && !canClose())) return false;
    closed = true;
    open.delete(api);
    element.close();
    element.remove();
    onClose();
    return true;
  };
  closer.onclick = () => close();
  element.addEventListener("cancel", (event) => {
    event.preventDefault();
    close();
  });
  // A press that starts and ends on the backdrop closes; text selection drags do not.
  let downOnBackdrop = false;
  element.addEventListener(
    "mousedown",
    (e) => (downOnBackdrop = e.target === element),
  );
  element.addEventListener("click", (e) => {
    if (e.target === element && downOnBackdrop) close();
  });
  document.body.append(element);
  element.showModal();
  const api = {
    element,
    body: content,
    footer: actions,
    close,
    setTitle(text, subtitleText) {
      heading.textContent = text;
      if (subtitleText !== undefined) {
        sub.textContent = subtitleText;
        sub.hidden = !subtitleText;
      }
    },
    setFooter(...nodes) {
      actions.replaceChildren(...nodes.flat());
      actions.hidden = !nodes.flat().length;
    },
  };
  open.add(api);
  return api;
}

export function closeAll() {
  for (const dialog of [...open]) dialog.close(true);
}

/** A yes/no question; resolves true only on explicit confirmation. */
export function confirmDialog({
  title,
  message,
  confirm,
  cancel,
  danger = false,
}) {
  return new Promise((resolve) => {
    let answer = false;
    const yes = h(
      "button",
      {
        type: "button",
        class: danger ? "danger" : "primary",
        onclick: () => ((answer = true), dialog.close()),
      },
      confirm || t("action.confirm"),
    );
    const no = h(
      "button",
      { type: "button", onclick: () => dialog.close() },
      cancel || t("action.cancel"),
    );
    const dialog = openDialog({
      title,
      body: h("p", {}, message),
      footer: [no, yes],
      size: "narrow",
      onClose: () => resolve(answer),
    });
    yes.focus();
  });
}

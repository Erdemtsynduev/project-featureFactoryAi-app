/* Discussion tab: the question picker or a message to the next step, then history. */

import * as api from "../core/api.js";
import { h } from "../core/dom.js";
import { formatTime, t } from "../core/i18n.js";
import { persistentMap } from "../core/storage.js";
import { refresh } from "../core/store.js";
import { toast, toastError } from "../ui/toast.js";
import { answerPanel } from "../features/answers.js";
import { safeJson, section } from "./parts.js";

const messages = persistentMap("message-drafts");

function resultBubble(result) {
  const data =
    typeof result.data === "string" ? safeJson(result.data) : result.data || {};
  const usage = result.usage || {};
  const tokens = (usage.input_tokens || 0) + (usage.output_tokens || 0);
  return h(
    "article",
    { class: "message outcome-" + result.outcome },
    h(
      "header",
      {},
      h(
        "span",
        { class: "pill" },
        t("outcome." + result.outcome) === "outcome." + result.outcome
          ? result.outcome
          : t("outcome." + result.outcome),
      ),
      data.answer !== undefined
        ? h(
            "small",
            {},
            data.auto ? t("answer.autoAnswered") : t("answer.human"),
          )
        : null,
      tokens
        ? h("small", { class: "mono" }, t("detail.tokens", { count: tokens }))
        : null,
    ),
    h("p", { class: "message-text" }, result.reason),
    data.notes?.length
      ? h(
          "ul",
          { class: "notes" },
          data.notes.map((n) => h("li", {}, n)),
        )
      : null,
  );
}

function operatorMessages(detail) {
  return detail.events
    .filter((event) => event.kind === "operator_message")
    .map((event) => {
      const message = safeJson(event.detail);
      return h(
        "article",
        { class: "message from-operator" },
        h(
          "header",
          {},
          h("span", { class: "pill" }, t("detail.you")),
          h("small", {}, formatTime(event.at)),
        ),
        h("p", { class: "message-text" }, message.text),
        h(
          "small",
          { class: "hint" },
          detail.run.generation > message.after_generation
            ? t("message.delivered")
            : t("message.pending"),
        ),
      );
    });
}

function messageForm(run) {
  const input = h("textarea", {
    rows: "3",
    required: true,
    "aria-label": t("message.label"),
    placeholder: t("message.placeholder"),
    value: messages.get(run.id) || "",
    oninput: (e) => messages.set(run.id, e.target.value),
  });
  const send = h(
    "button",
    { type: "submit", class: "primary" },
    t("message.send"),
  );
  return h(
    "form",
    {
      class: "message-form",
      onsubmit: async (event) => {
        event.preventDefault();
        send.disabled = true;
        try {
          await api.command("message", run, { message: input.value });
          messages.delete(run.id);
          await refresh();
          toast(t("message.saved"), { tone: "success" });
        } catch (error) {
          toastError(error);
        } finally {
          send.disabled = false;
        }
      },
    },
    input,
    h(
      "div",
      { class: "form-actions" },
      h("small", { class: "hint" }, t("message.hint")),
      send,
    ),
  );
}

export function discussion(detail, step) {
  const run = detail.run;
  const nodes = [];
  if (run.active && step?.kind === "human")
    nodes.push(answerPanel(detail, step));
  else if (run.status !== "accepted") nodes.push(messageForm(run));
  const history = [
    ...detail.results.map(resultBubble),
    ...operatorMessages(detail),
  ];
  if (history.length) nodes.push(section(t("detail.history"), history));
  else nodes.push(h("p", { class: "empty" }, t("detail.noResults")));
  return nodes;
}

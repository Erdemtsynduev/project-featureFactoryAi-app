/* Answering a waiting human step.
 *
 * Structured questions work like a CLI picker: arrows or digits choose, Enter
 * moves on, the recommended option is marked and preselected. A requirement's
 * approval shows the proposed tickets; approving creates them on the board.
 * Choices and typed text are drafts until submitted. */

import * as api from "../core/api.js";
import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { persistentMap } from "../core/storage.js";
import { refresh } from "../core/store.js";
import { toast, toastError } from "../ui/toast.js";

const notes = persistentMap("answer-drafts");
const picks = persistentMap("choice-drafts");

const SUCCESS = ["approved", "answered", "resolved", "done"];

function outcomeLabel(outcome) {
  const key = "outcome." + outcome;
  const text = t(key);
  return text === key ? outcome : text;
}

function picker(question, index, total, chosen, save, focusNext) {
  const buttons = [];
  const select = (value, focus) => {
    chosen[question.id] = value;
    save();
    for (const b of buttons) {
      const on = b.dataset.value === value;
      b.setAttribute("aria-checked", String(on));
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    }
  };
  if (!(question.id in chosen) && question.recommended)
    chosen[question.id] = question.recommended;
  question.options.forEach((value, i) => {
    const button = h(
      "button",
      { type: "button", class: "option", role: "radio", dataset: { value } },
      h("kbd", {}, String(i + 1)),
      h("span", {}, value),
      value === question.recommended
        ? h("em", {}, t("answer.recommended"))
        : null,
    );
    button.onclick = () => select(value, true);
    button.onkeydown = (e) => {
      const at = question.options.indexOf(value);
      const count = question.options.length;
      if (e.key === "ArrowDown" || e.key === "ArrowRight")
        select(question.options[(at + 1) % count], true);
      else if (e.key === "ArrowUp" || e.key === "ArrowLeft")
        select(question.options[(at - 1 + count) % count], true);
      else if (/^[1-9]$/.test(e.key) && question.options[Number(e.key) - 1])
        select(question.options[Number(e.key) - 1], true);
      else if (e.key === "Enter") focusNext(index);
      else return;
      e.preventDefault();
    };
    buttons.push(button);
  });
  const group = h(
    "fieldset",
    { class: "picker" },
    h(
      "legend",
      {},
      h("span", { class: "picker-index" }, `${index + 1}/${total}`),
      h("span", {}, question.text),
    ),
    h(
      "div",
      { class: "options", role: "radiogroup", "aria-label": question.text },
      buttons,
    ),
  );
  select(chosen[question.id], false);
  return group;
}

function ticketList(tickets) {
  return h(
    "ol",
    { class: "ticket-list" },
    tickets.map((ticket) =>
      h(
        "li",
        {},
        h(
          "div",
          { class: "ticket-head" },
          h("code", {}, ticket.id),
          h("strong", {}, ticket.title),
        ),
        ticket.goal ? h("p", {}, ticket.goal) : null,
        ticket.acceptance.length
          ? h(
              "ul",
              {},
              ticket.acceptance.map((a) => h("li", {}, a)),
            )
          : null,
        ticket.depends_on.length || ticket.paths.length
          ? h(
              "small",
              { class: "hint" },
              [
                ticket.depends_on.length
                  ? t("tickets.dependsOn", {
                      ids: ticket.depends_on.join(", "),
                    })
                  : "",
                ticket.paths.length
                  ? t("tickets.paths", { paths: ticket.paths.join(", ") })
                  : "",
              ]
                .filter(Boolean)
                .join(" · "),
            )
          : null,
      ),
    ),
  );
}

/** The panel for the waiting human step of `detail`; `after(result)` runs on success. */
export function answerPanel(detail, step, after) {
  const run = detail.run;
  const key = run.id + ":" + run.active.id;
  const asked = detail.questions || [];
  const chosen = picks.get(key) || {};
  const save = () => picks.set(key, chosen);
  const tickets = detail.tickets || [];
  const approving =
    tickets.length && step.transitions.some(([o]) => o === "approved");
  const pickers = [];
  const note = h("textarea", {
    rows: "3",
    "aria-label": t("answer.note"),
    placeholder:
      asked.length || approving
        ? t("answer.optionalNote")
        : t("answer.writeReply"),
    value: notes.get(key) || "",
    oninput: (e) => notes.set(key, e.target.value),
  });
  const focusNext = (index) => {
    const next = pickers[index + 1];
    (next
      ? next.querySelector('[aria-checked="true"], .option')
      : note
    ).focus();
  };
  asked.forEach((q, i) =>
    pickers.push(picker(q, i, asked.length, chosen, save, focusNext)),
  );

  const submit = async (outcome, choices, button) => {
    const typed = note.value.trim();
    if (!asked.length && !approving && !typed && !SUCCESS.includes(outcome))
      throw Error(t("answer.needText"));
    if (outcome === "rework" && !typed) throw Error(t("answer.needReworkNote"));
    button.disabled = true;
    try {
      const result = await api.post("answer", {
        id: run.id,
        version: run.version,
        outcome,
        answer: typed,
        choices,
      });
      notes.delete(key);
      picks.delete(key);
      document.dispatchEvent(new Event("ffai-answered"));
      if (result.admitted?.length)
        toast(t("tickets.created", { count: result.admitted.length }), {
          tone: "success",
        });
      await refresh();
      after?.(result);
    } finally {
      button.disabled = false;
    }
  };
  const act = (outcome, choices) => async (event) => {
    try {
      await submit(outcome, choices(), event.currentTarget);
    } catch (error) {
      toastError(error);
    }
  };
  const outcomes = step.transitions.map(([o]) => o);
  const primary = outcomes.find((o) => SUCCESS.includes(o)) || outcomes[0];
  const buttons = outcomes.map((outcome) =>
    h(
      "button",
      {
        type: "button",
        class: outcome === primary ? "primary" : "",
        onclick: act(outcome, () => (asked.length ? { ...chosen } : {})),
      },
      approving && outcome === "approved"
        ? t("tickets.approve", { count: tickets.length })
        : outcomeLabel(outcome),
    ),
  );
  if (asked.length > 1 && asked.every((q) => q.recommended))
    buttons.push(
      h(
        "button",
        {
          type: "button",
          class: "ghost",
          onclick: act(primary, () =>
            Object.fromEntries(asked.map((q) => [q.id, q.recommended])),
          ),
        },
        t("answer.acceptAll"),
      ),
    );
  return h(
    "section",
    { class: "answer-panel" },
    h("small", { class: "eyebrow" }, t("answer.eyebrow")),
    h("h3", {}, step.prompt || t("answer.needed")),
    approving
      ? h(
          "div",
          { class: "tickets-preview" },
          h("p", {}, t("tickets.preview", { count: tickets.length })),
          ticketList(tickets),
        )
      : null,
    pickers,
    note,
    h("div", { class: "form-actions" }, buttons),
  );
}

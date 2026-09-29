/* Interface copy by key. User content, agent output and workflow prompts are never
 * translated. Static markup declares keys with data-i18n attributes; code calls t(). */

import ru from "../i18n/ru.js";
import en from "../i18n/en.js";

const DICTIONARIES = { ru, en };

export function language() {
  return window.ffaiPreferences?.language === "en" ? "en" : "ru";
}

/** t("board.lane.queue"), t("task.newIn", { project: "App" }) */
export function t(key, params) {
  const dictionary = DICTIONARIES[language()];
  let text = dictionary[key] ?? ru[key] ?? key;
  if (typeof text === "object") text = plural(text, params?.count ?? 0);
  if (params)
    text = text.replace(/\{(\w+)\}/g, (match, name) =>
      name in params ? String(params[name]) : match,
    );
  return text;
}

function plural(forms, count) {
  const rule = new Intl.PluralRules(language()).select(count);
  return forms[rule] ?? forms.other;
}

const ATTRIBUTES = ["title", "placeholder", "aria-label"];

/** Translate static markup: data-i18n (text) and data-i18n-<attribute>. */
export function applyStatic(root = document) {
  document.documentElement.lang = language();
  for (const node of root.querySelectorAll("[data-i18n]"))
    node.textContent = t(node.dataset.i18n);
  for (const attribute of ATTRIBUTES)
    for (const node of root.querySelectorAll(`[data-i18n-${attribute}]`))
      node.setAttribute(
        attribute,
        t(node.getAttribute(`data-i18n-${attribute}`)),
      );
}

/** Calls used, against an optional cap: "12" with no cap, "12 / 40" with one. */
export function callsOf(calls, cap) {
  return cap == null ? `${calls}` : `${calls} / ${cap}`;
}

export function formatNumber(value) {
  return new Intl.NumberFormat(language()).format(value);
}

export function formatTime(seconds, withDate = false) {
  if (!seconds) return "—";
  const date = new Date(seconds * 1000);
  const sameDay = new Date().toDateString() === date.toDateString();
  return withDate || !sameDay
    ? date.toLocaleString(language(), {
        dateStyle: "short",
        timeStyle: "short",
      })
    : date.toLocaleTimeString(language(), {
        hour: "2-digit",
        minute: "2-digit",
      });
}

export function relativeTime(seconds) {
  if (!seconds) return "—";
  const delta = Math.round(seconds - Date.now() / 1000);
  const format = new Intl.RelativeTimeFormat(language(), { numeric: "auto" });
  const abs = Math.abs(delta);
  if (abs < 60) return format.format(delta, "second");
  if (abs < 3600) return format.format(Math.round(delta / 60), "minute");
  if (abs < 86400) return format.format(Math.round(delta / 3600), "hour");
  return format.format(Math.round(delta / 86400), "day");
}

export function money(value) {
  if (value === undefined || value === null) return "—";
  const digits = value >= 100 ? 0 : value >= 1 ? 2 : 3;
  return "$" + value.toFixed(digits);
}

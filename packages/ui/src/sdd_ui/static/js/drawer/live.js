/* What the running attempt is doing now: time, timeout, output freshness, output. */

import * as api from "../core/api.js";
import { h, replace } from "../core/dom.js";
import { formatTime, t } from "../core/i18n.js";
import { runnerOf } from "../core/store.js";
import { elapsed, stepName } from "../features/vocabulary.js";

const STALE_OUTPUT = 300; // seconds without output before the panel says so
let liveTimer = null;

export function stopLive() {
  clearTimeout(liveTimer);
  liveTimer = null;
}

/** What the running attempt is doing now: time, timeout, freshness and output. */
export function livePanel(run, step, isCurrent) {
  const box = h("section", { class: "live" });
  const draw = (data) => {
    if (!data.active) return replace(box);
    const total = data.deadline - data.started;
    const share = Math.min(
      100,
      Math.round((100 * (data.now - data.started)) / total),
    );
    const lines = Object.values(data.streams).flatMap((stream) => stream.lines);
    const quiet =
      data.last_output && data.now - data.last_output > STALE_OUTPUT;
    const tail = h(
      "pre",
      { class: "live-tail", "aria-live": "polite" },
      lines.slice(-30).join("\n") || t("live.noOutput"),
    );
    replace(
      box,
      h(
        "div",
        { class: "live-head" },
        h("span", { class: "spinner", "aria-hidden": "true" }),
        h("strong", {}, t("live.now", { step: stepName(step || run.step) })),
        h("span", { class: "runner" }, runnerOf(step, t)),
        h("span", { class: "spacer" }),
        h(
          "span",
          { class: "elapsed mono", dataset: { since: String(data.started) } },
          elapsed(data.started, data.now),
        ),
      ),
      h(
        "div",
        {
          class: "live-meter",
          title: t("live.timeoutAt", { time: formatTime(data.deadline) }),
        },
        h(
          "span",
          { class: "progress-track wide" },
          h("span", { style: { width: share + "%" } }),
        ),
        h(
          "small",
          { class: "hint" },
          t("live.timeout", { time: formatTime(data.deadline) }),
        ),
      ),
      h(
        "small",
        { class: quiet ? "live-quiet" : "hint" },
        data.last_output
          ? h(
              "span",
              { dataset: { ago: String(data.last_output) } },
              t("live.ago", { time: elapsed(data.last_output, data.now) }),
            )
          : t("live.waitingOutput"),
        quiet ? " · " + t("live.quiet") : "",
        data.hosted ? "" : " · " + t("live.notHosted"),
      ),
      tail,
    );
    tail.scrollTop = tail.scrollHeight;
  };
  const poll = async () => {
    if (!isCurrent() || !box.isConnected) return stopLive();
    try {
      draw(await api.get("live", { id: run.id }));
    } catch {
      /* The next poll retries; the panel keeps its last picture. */
    }
    liveTimer = setTimeout(poll, 2000);
  };
  stopLive();
  liveTimer = setTimeout(poll, 0);
  return box;
}

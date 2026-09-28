/* Editable pipeline view of a workflow graph.
 *
 * Layout is derived from the graph itself and never stored in the workflow:
 * the main lane follows each step's success outcome from the entry; every other
 * step sits in a recovery lane below the stage it serves. Edges that are not a
 * straight hop to the next stage run as orthogonal wires on their own tracks.
 */
"use strict";
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const NODE_W = 168,
    NODE_H = 76,
    PITCH_X = 228,
    PITCH_Y = 188,
    LEFT = 28,
    HEADER = 34,
    TOP_TRACKS = 56;
  const SUCCESS = [
    "passed",
    "done",
    "approved",
    "answered",
    "ok",
    "true",
    "yes",
  ];
  const GLYPH = {
    agent: "●",
    check: "◆",
    human: "◉",
    condition: "◇",
    operation: "■",
    finish: "✓",
  };

  function svg(tag, attrs = {}, text) {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs))
      if (value !== undefined && value !== null) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function config(step) {
    try {
      return JSON.parse(step.config || "{}");
    } catch {
      return {};
    }
  }

  function mainLane(flow, byId) {
    const lane = [];
    let current = byId.get(flow.entry);
    while (current && !lane.includes(current.id)) {
      lane.push(current.id);
      if (current.kind === "finish") break;
      const edges = current.transitions.filter(([, t]) => byId.has(t));
      const pick =
        SUCCESS.map((o) =>
          edges.find(([name, t]) => name === o && !lane.includes(t)),
        ).find(Boolean) || edges.find(([, t]) => !lane.includes(t));
      current = pick ? byId.get(pick[1]) : null;
    }
    return lane;
  }

  function layout(flow) {
    const byId = new Map(flow.steps.map((s) => [s.id, s]));
    const place = new Map();
    const lane = mainLane(flow, byId);
    lane.forEach((id, col) => place.set(id, { col, row: 0 }));
    // Breadth-first discovery keeps recovery steps near the stage that routes to them.
    const order = [];
    const seen = new Set([flow.entry]);
    const queue = [flow.entry];
    while (queue.length) {
      const id = queue.shift();
      order.push(id);
      for (const [, target] of byId.get(id)?.transitions || [])
        if (byId.has(target) && !seen.has(target)) {
          seen.add(target);
          queue.push(target);
        }
    }
    for (const step of flow.steps) if (!seen.has(step.id)) order.push(step.id);
    const taken = new Set([...place.values()].map((p) => p.row + ":" + p.col));
    const success = (id) => {
      const edges = byId.get(id)?.transitions || [];
      const hit = SUCCESS.map((o) => edges.find(([name]) => name === o)).find(
        Boolean,
      );
      return hit && byId.has(hit[1]) && !place.has(hit[1]) ? hit[1] : null;
    };
    const put = (id, row, col) => {
      taken.add(row + ":" + col);
      place.set(id, { col, row });
      // Keep a recovery chain (diagnose → repair) on one row, side by side.
      const next = success(id);
      if (next && !taken.has(row + ":" + (col + 1))) put(next, row, col + 1);
    };
    for (const id of order) {
      if (place.has(id)) continue;
      const preds = flow.steps
        .filter(
          (s) => place.has(s.id) && s.transitions.some(([, t]) => t === id),
        )
        .map((s) => place.get(s.id));
      const desired = preds.length
        ? Math.min(...preds.map((p) => p.col + (p.row === 0 ? 0 : 1)))
        : 0;
      let row = 1,
        col = desired;
      for (;;) {
        while (taken.has(row + ":" + col)) col++;
        if (col - desired <= 3) break;
        row++;
        col = desired;
      }
      put(id, row, col);
    }
    const rows = Math.max(0, ...[...place.values()].map((p) => p.row)) + 1;
    const cols = Math.max(0, ...[...place.values()].map((p) => p.col)) + 1;
    const top = HEADER + TOP_TRACKS;
    const box = new Map();
    for (const [id, p] of place)
      box.set(id, {
        x: LEFT + p.col * PITCH_X,
        y: top + p.row * PITCH_Y,
        w: NODE_W,
        h: NODE_H,
        ...p,
      });
    return { byId, lane, box, rows, cols, top };
  }

  function rounded(points) {
    const r = 7;
    let d = `M${points[0][0]},${points[0][1]}`;
    for (let i = 1; i < points.length - 1; i++) {
      const [px, py] = points[i - 1],
        [x, y] = points[i],
        [nx, ny] = points[i + 1];
      const inLen = Math.hypot(x - px, y - py),
        outLen = Math.hypot(nx - x, ny - y);
      const k = Math.min(r, inLen / 2, outLen / 2);
      const ax = x - ((x - px) / (inLen || 1)) * k,
        ay = y - ((y - py) / (inLen || 1)) * k;
      const bx = x + ((nx - x) / (outLen || 1)) * k,
        by = y + ((ny - y) / (outLen || 1)) * k;
      d += ` L${ax},${ay} Q${x},${y} ${bx},${by}`;
    }
    const [lx, ly] = points[points.length - 1];
    return d + ` L${lx},${ly}`;
  }

  function route(flow, geo) {
    const edges = [];
    for (const step of flow.steps)
      for (const [outcome, target] of step.transitions) {
        const a = geo.box.get(step.id),
          b = geo.box.get(target);
        if (!a || !b) continue;
        let kind;
        if (a.row === b.row && b.col === a.col + 1) kind = "straight";
        else if (a.row === b.row) kind = a.row === 0 ? "over" : "under";
        else kind = b.row > a.row ? "down" : "up";
        edges.push({ source: step.id, outcome, target, a, b, kind });
      }
    // Wires with the same outcome, target and channel share one bus and one label.
    const buses = new Map();
    for (const e of edges) {
      if (e.kind === "straight") continue;
      e.outSide = e.kind === "over" || e.kind === "up" ? "top" : "bottom";
      e.inSide = e.kind === "over" || e.kind === "down" ? "top" : "bottom";
      e.channel =
        e.kind === "over"
          ? "top"
          : e.kind === "up"
            ? "below:" + (e.a.row - 1)
            : "below:" + e.a.row;
      const key = [e.target, e.outcome, e.inSide, e.channel].join("|");
      if (!buses.has(key))
        buses.set(key, { key, members: [], b: e.b, channel: e.channel });
      buses.get(key).members.push(e);
      e.bus = buses.get(key);
    }
    // Ports: spread exits per node side and entries per bus.
    const sides = new Map();
    const add = (key, item) => {
      if (!sides.has(key)) sides.set(key, []);
      sides.get(key).push(item);
    };
    for (const e of edges)
      if (e.bus)
        add(e.source + ":" + e.outSide, {
          x: e.b.x,
          set: (x) => (e.outX = x),
          box: e.a,
        });
    for (const bus of buses.values()) {
      const e = bus.members[0];
      const from =
        bus.members.reduce((sum, m) => sum + m.a.x, 0) / bus.members.length;
      add(e.target + ":" + e.inSide, {
        x: from,
        set: (x) => (bus.inX = x),
        box: e.b,
      });
    }
    for (const list of sides.values()) {
      list.sort((l, r) => l.x - r.x);
      list.forEach((item, i) => {
        item.set(
          item.box.x + 24 + ((item.box.w - 48) * (i + 1)) / (list.length + 1),
        );
      });
    }
    // Tracks: one horizontal lane per bus inside its channel.
    const channels = new Map();
    for (const bus of buses.values()) {
      if (!channels.has(bus.channel)) channels.set(bus.channel, []);
      channels.get(bus.channel).push(bus);
    }
    for (const [channel, list] of channels) {
      const span = (bus) =>
        Math.max(...bus.members.map((m) => Math.abs(m.outX - bus.inX)));
      list.sort((l, r) => span(l) - span(r) || l.inX - r.inX);
      list.forEach((bus, i) => {
        if (channel === "top") {
          const gap = Math.min(
            18,
            (TOP_TRACKS - 10) / Math.max(1, list.length),
          );
          bus.trackY = geo.top - 12 - i * gap;
        } else {
          const row = Number(channel.split(":")[1]);
          const start = geo.top + row * PITCH_Y + NODE_H + 14;
          const room = PITCH_Y - NODE_H - 28;
          bus.trackY = start + (room * (i + 0.5)) / list.length;
        }
      });
    }
    for (const e of edges) {
      const { a, b } = e;
      if (e.kind === "straight") {
        const y = a.y + a.h / 2;
        e.points = [
          [a.x + a.w, y],
          [b.x - 3, y],
        ];
        e.label = [(a.x + a.w + b.x) / 2, y - 10];
        continue;
      }
      const bus = e.bus;
      const sy = e.outSide === "top" ? a.y : a.y + a.h;
      const ty = e.inSide === "top" ? b.y - 3 : b.y + b.h + 3;
      e.points = [
        [e.outX, sy],
        [e.outX, bus.trackY],
        [bus.inX, bus.trackY],
        [bus.inX, ty],
      ];
      if (bus.members[0] === e) {
        const reach = bus.members.map((m) => m.outX - bus.inX);
        const side = reach.every((d) => d <= 0) ? -1 : 1;
        const width = e.outcome.length * 6.4 + 12;
        e.label = [bus.inX + side * (width / 2 + 8), bus.trackY];
      }
    }
    return edges;
  }

  function positive(outcome) {
    return SUCCESS.includes(outcome);
  }

  function truncate(value, size) {
    return value.length > size ? value.slice(0, size - 1) + "…" : value;
  }

  /**
   * options: labels, selected (index), selectedEdge ([source, outcome]),
   * zoom, connecting (source id), run ({step, completed, visits, status}),
   * onSelect(index), onEdge(source, outcome, target), onInsert(source, outcome),
   * onPort(source), onBackground().
   */
  function render(root, flow, options = {}) {
    const geo = layout(flow);
    const edges = route(flow, geo);
    const width =
      LEFT * 2 + Math.max(1, geo.cols) * PITCH_X - (PITCH_X - NODE_W);
    const height = geo.top + geo.rows * PITCH_Y + 6;
    const zoom = options.zoom || 1;
    const canvas = svg("svg", {
      viewBox: `0 0 ${width} ${height}`,
      width: Math.round(width * zoom),
      height: Math.round(height * zoom),
      class: "pipeline" + (options.connecting ? " is-connecting" : ""),
      role: "group",
      "aria-label": options.ariaLabel || "Pipeline",
    });
    canvas.addEventListener("click", (e) => {
      if (e.target === canvas) options.onBackground?.();
    });
    const defs = svg("defs");
    for (const [id, cls] of [
      ["pl-arrow", "wire-tip"],
      ["pl-arrow-alt", "wire-tip alt"],
      ["pl-arrow-on", "wire-tip on"],
    ]) {
      const marker = svg("marker", {
        id,
        viewBox: "0 0 8 8",
        refX: 7,
        refY: 4,
        markerWidth: 7,
        markerHeight: 7,
        orient: "auto-start-reverse",
      });
      marker.append(svg("path", { d: "M0,0 L8,4 L0,8 z", class: cls }));
      defs.append(marker);
    }
    canvas.append(defs);

    // Stage rail: the numbering is the real order of the main lane.
    const rail = svg("g", { class: "stage-rail" });
    geo.lane.forEach((id, col) => {
      const x = LEFT + col * PITCH_X;
      rail.append(
        svg(
          "text",
          { x, y: 18, class: "stage-index" },
          String(col + 1).padStart(2, "0"),
        ),
        svg("line", {
          x1: x + 24,
          y1: 14,
          x2: x + NODE_W,
          y2: 14,
          class: "stage-line",
        }),
      );
    });
    if (geo.rows > 1) {
      const y = geo.top + PITCH_Y - 40;
      rail.append(
        svg("rect", { x: 0, y, width, height: height - y, class: "lane-band" }),
        svg(
          "text",
          {
            x: 0,
            y: 0,
            class: "lane-caption",
            transform: `translate(17,${height - 12}) rotate(-90)`,
          },
          options.recoveryLabel || "recovery",
        ),
      );
    }
    canvas.append(rail);

    const selectedStep = flow.steps[options.selected]?.id;
    const [selSource, selOutcome] = options.selectedEdge || [];
    const wires = svg("g", { class: "wires" });
    const chips = svg("g", { class: "chips" });
    const placed = [];
    for (const e of edges) {
      const chosen = e.source === selSource && e.outcome === selOutcome;
      const tone = positive(e.outcome) ? "" : " alt";
      const path = svg("path", {
        d: rounded(e.points),
        class: "wire" + tone + (chosen ? " on" : "") + " " + e.kind,
        "marker-end": `url(#${chosen ? "pl-arrow-on" : tone ? "pl-arrow-alt" : "pl-arrow"})`,
      });
      const hit = svg("path", {
        d: rounded(e.points),
        class: "wire-hit",
        tabindex: 0,
        role: "button",
        "aria-label": `${e.source}: ${e.outcome} → ${e.target}`,
      });
      const pick = (ev) => {
        ev.stopPropagation();
        options.onEdge?.(e.source, e.outcome, e.target);
      };
      hit.addEventListener("click", pick);
      hit.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") pick(ev);
      });
      wires.append(path, hit);
      if (!e.label) continue;
      let [lx, ly] = e.label;
      const label = truncate(e.outcome, 14);
      const w = label.length * 6.4 + 12;
      // Nudge a label along its wire until it clears the labels already drawn.
      if (e.kind !== "straight") {
        const base = lx;
        const clash = (x) =>
          x - w / 2 < 2 ||
          x + w / 2 > width - 2 ||
          placed.some(
            ([px, py, pw]) =>
              Math.abs(px - x) < (pw + w) / 2 + 4 && Math.abs(py - ly) < 18,
          );
        for (const k of [0, 1, -1, 2, -2, 3, -3]) {
          const x = base + k * (w + 8);
          if (!clash(x)) {
            lx = x;
            break;
          }
        }
      }
      placed.push([lx, ly, w]);
      const chip = svg("g", {
        class: "chip" + tone + (chosen ? " on" : ""),
        transform: `translate(${lx - w / 2},${ly - 8})`,
      });
      chip.append(
        svg("rect", { width: w, height: 16, rx: 8 }),
        svg("text", { x: w / 2, y: 11.5, "text-anchor": "middle" }, label),
      );
      chip.addEventListener("click", pick);
      chips.append(chip);
      if (e.kind === "straight" && options.onInsert) {
        const plus = svg("g", {
          class: "insert",
          transform: `translate(${lx},${e.points[0][1] + 13})`,
          tabindex: 0,
          role: "button",
          "aria-label": `Insert after ${e.source} (${e.outcome})`,
        });
        plus.append(
          svg("circle", { r: 8 }),
          svg("path", { d: "M-4,0 H4 M0,-4 V4" }),
        );
        const insert = (ev) => {
          ev.stopPropagation();
          options.onInsert(e.source, e.outcome);
        };
        plus.addEventListener("click", insert);
        plus.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") insert(ev);
        });
        chips.append(plus);
      }
    }
    canvas.append(wires);

    const run = options.run;
    const completed = new Set(run?.completed || []);
    const visits = new Map(run?.visits || []);
    const nodes = svg("g", { class: "nodes" });
    flow.steps.forEach((step, index) => {
      const b = geo.box.get(step.id);
      if (!b) return;
      const settings = config(step);
      const current = run && run.step === step.id;
      const classes = [
        "stage",
        "kind-" + step.kind,
        index === options.selected ? "selected" : "",
        current ? "current status-" + run.status : "",
        completed.has(step.id) ? "done" : "",
        options.connecting === step.id ? "source" : "",
      ];
      const group = svg("g", {
        class: classes.filter(Boolean).join(" "),
        transform: `translate(${b.x},${b.y})`,
        tabindex: 0,
        role: "button",
        "aria-label": `${options.labels?.[step.id] || step.id} (${step.kind})`,
        "aria-pressed": String(index === options.selected),
      });
      group.append(
        svg("rect", { width: b.w, height: b.h, rx: 8, class: "stage-body" }),
      );
      const runner = options.runner
        ? options.runner(step)
        : step.kind === "human"
          ? ""
          : step.handler || (step.profile !== "default" ? step.profile : "");
      group.append(
        svg(
          "text",
          { x: 12, y: 19, class: "stage-kind" },
          // The glyph names the kind; the text names who runs it (e.g. "claude · opus").
          `${GLYPH[step.kind] || "·"} ${runner ? truncate(runner, 21) : step.kind}`,
        ),
        svg(
          "text",
          { x: 12, y: 42, class: "stage-title" },
          truncate(options.labels?.[step.id] || settings.title || step.id, 22),
        ),
        svg("text", { x: 12, y: 63, class: "stage-id" }, truncate(step.id, 18)),
      );
      const flags = [];
      if (step.gate) flags.push("gate");
      else if (step.required) flags.push("req");
      if (step.mutates) flags.push("writes");
      let fx = b.w - 10;
      for (const flag of flags.reverse()) {
        const w = flag.length * 5.6 + 10;
        fx -= w;
        const tag = svg("g", {
          class: "flag flag-" + flag,
          transform: `translate(${fx},53)`,
        });
        tag.append(
          svg("rect", { width: w, height: 14, rx: 3 }),
          svg("text", { x: w / 2, y: 10.2, "text-anchor": "middle" }, flag),
        );
        group.append(tag);
        fx -= 4;
      }
      if (step.id === flow.entry)
        group.append(
          svg(
            "text",
            { x: b.w - 10, y: 19, "text-anchor": "end", class: "stage-entry" },
            "start",
          ),
        );
      if (visits.get(step.id) > 1)
        group.append(
          svg(
            "text",
            { x: b.w - 10, y: 19, "text-anchor": "end", class: "stage-visits" },
            "×" + visits.get(step.id),
          ),
        );
      if (step.kind !== "finish" && options.onPort) {
        const port = svg("g", {
          class: "port-out",
          transform: `translate(${b.w},${b.h / 2})`,
          tabindex: 0,
          role: "button",
          "aria-label": `Connect from ${step.id}`,
        });
        port.append(
          svg("circle", { r: 7 }),
          svg("path", { d: "M-3,0 H3 M0,-3 V3" }),
        );
        const start = (ev) => {
          ev.stopPropagation();
          options.onPort(step.id);
        };
        port.addEventListener("click", start);
        port.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") start(ev);
        });
        group.append(port);
      }
      const choose = (ev) => {
        ev.stopPropagation();
        options.onSelect?.(index);
      };
      group.addEventListener("click", choose);
      group.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          choose(ev);
        }
      });
      nodes.append(group);
    });
    canvas.append(nodes, chips);
    root.replaceChildren(canvas);
    return { width, height, selectedStep };
  }

  window.ffaiPipeline = { render, layout };
})();

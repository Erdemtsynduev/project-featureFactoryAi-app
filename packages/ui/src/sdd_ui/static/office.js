/* Live pixel office driven by the queue.
 *
 * People are the workflow's roles: each sits behind a desk, facing you, under a
 * name plate with the model that runs it. Tasks are documents on the desk of
 * their current step. The person types (mutating agent, code scrolls on the
 * screen), reads (review), watches a check spinner, raises "?" at your desk or
 * "!" when blocked. When a task moves on, the finishing person carries the
 * folder to the next desk and walks back. Paused and queued tasks wait in the
 * inbox tray, accepted ones on the shelf; idle people fetch coffee. The room
 * follows the theme: daylight windows in light mode, a night sky and glowing
 * screens in dark mode. Decorative: it reads state and opens cards.
 */
"use strict";
(() => {
  const T = 16; // art pixels per tile
  const Z = 2; // screen pixels per art pixel
  const TILE = T * Z;
  const ROWS = 11;
  const WALL = 2; // rows of back wall
  const MAIN = 4; // desk row of the main lane (seat above it)
  const RECOVERY = 8; // desk row of the recovery lane
  const SPEED = 3.2; // tiles per second
  const SKIN = ["#f1c9a5", "#e0b089", "#c68d64", "#8d5a3b", "#b07850"];
  const HAIR = [
    "#2b2320",
    "#6b4b32",
    "#1f1f24",
    "#a0522d",
    "#d9b56f",
    "#7a7f8a",
  ];
  const SHIRT = [
    "#4f7cac",
    "#6f9e5b",
    "#c0694c",
    "#8a6fb0",
    "#c9a13f",
    "#3f9a95",
    "#b25a7e",
  ];
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");

  let canvas = null,
    ctx = null,
    root = null,
    width = 0,
    cols = 0,
    handlers = {},
    hover = null,
    running = false,
    last = 0;
  let desks = [],
    blocked = new Set(),
    inbox = null,
    shelf = null,
    coffee = null,
    signature = "",
    stars = [];
  let docs = { inbox: [], shelf: [] };
  const agents = new Map(); // desk key -> person
  const previous = new Map(); // run id -> last desk key

  function hash(value) {
    let h = 2166136261;
    for (const c of value) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
    return h >>> 0;
  }
  function token(name, fallback) {
    return (
      getComputedStyle(document.documentElement)
        .getPropertyValue(name)
        .trim() || fallback
    );
  }
  const dark = () => document.documentElement.dataset.theme === "dark";

  function mount(container, callbacks) {
    handlers = callbacks;
    root = container;
    canvas = document.createElement("canvas");
    canvas.className = "office-canvas";
    canvas.setAttribute("role", "img");
    ctx = canvas.getContext("2d");
    const tip = document.createElement("div");
    tip.className = "office-tip";
    tip.hidden = true;
    root.replaceChildren(canvas, tip);
    canvas.addEventListener("mousemove", (e) => {
      hover = pick(e);
      canvas.style.cursor = hover ? "pointer" : "default";
      tip.hidden = !hover;
      if (!hover) return;
      tip.textContent = hover.label;
      const box = root.getBoundingClientRect();
      tip.style.left =
        Math.min(e.clientX - box.left + 12, box.width - 240) + "px";
      tip.style.top = e.clientY - box.top + 14 + "px";
    });
    canvas.addEventListener("mouseleave", () => {
      hover = null;
      tip.hidden = true;
    });
    canvas.addEventListener("click", (e) => {
      const target = pick(e);
      if (target?.run) handlers.openRun(target.run);
      else if (target?.step) handlers.openStep(target.step);
    });
    new ResizeObserver(resize).observe(root);
    document.addEventListener("visibilitychange", loop);
    resize();
  }

  function resize() {
    const ratio = window.devicePixelRatio || 1;
    width = Math.max(320, root.clientWidth);
    const next = Math.floor(width / TILE);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(ROWS * TILE * ratio);
    canvas.style.width = width + "px";
    canvas.style.height = ROWS * TILE + "px";
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.imageSmoothingEnabled = false;
    if (next !== cols) {
      cols = next;
      signature = "";
      stars = Array.from({ length: 60 }, (_, i) => [
        hash("s" + i) % (cols * T),
        4 + (hash("y" + i) % (WALL * T - 12)),
      ]);
      if (handlers.flow) arrange(handlers.flow());
    }
    loop();
  }

  // One desk per role: every human step is you, every check the tester desk.
  function deskKey(step) {
    if (!step) return "";
    if (step.kind === "human") return "you";
    if (step.kind === "check") return "checks";
    if (step.handler && step.handler.startsWith("lane-")) return "merge";
    return step.id;
  }

  function arrange(flows) {
    const list = (flows || []).filter(Boolean);
    const key = JSON.stringify([
      cols,
      list.map((f) => f.steps.map((s) => [s.id, s.kind, handlers.runner(s)])),
    ]);
    if (key === signature) return;
    signature = key;
    desks = [];
    blocked = new Set();
    for (let x = 0; x < cols; x++)
      for (let y = 0; y < WALL; y++) blocked.add(x + ":" + y);
    inbox = { x: 0, y: 6 };
    shelf = { x: cols - 2, y: 6 };
    coffee = { x: 1, y: ROWS - 1 };
    for (const tile of [inbox, shelf, coffee])
      blocked.add(tile.x + ":" + tile.y);
    blocked.add(shelf.x + 1 + ":" + shelf.y);
    const main = [],
      recovery = [],
      seen = new Set();
    for (const flow of list) {
      const geo = window.ffaiPipeline.layout(flow);
      const ordered = flow.steps
        .filter((s) => s.kind !== "finish" && geo.box.has(s.id))
        .sort((a, b) => geo.box.get(a.id).col - geo.box.get(b.id).col);
      for (const row of [0, 1])
        for (const step of ordered) {
          if ((row === 0) !== (geo.box.get(step.id).row === 0)) continue;
          const id = deskKey(step);
          if (seen.has(id)) continue;
          seen.add(id);
          (row === 0 ? main : recovery).push(step);
        }
    }
    const left = 3,
      right = cols - 5;
    const place = (steps, y) => {
      const gap = steps.length > 1 ? (right - left) / (steps.length - 1) : 0;
      steps.forEach((step, i) => {
        const x = Math.round(left + i * Math.max(3, gap));
        desks.push({
          key: deskKey(step),
          step,
          x,
          y,
          label: handlers.label(step),
          runner: step.kind === "human" ? "" : handlers.runner(step),
          docs: [],
        });
        blocked.add(x + ":" + y).add(x + 1 + ":" + y);
      });
    };
    place(main, MAIN);
    place(recovery, RECOVERY);
    for (const id of agents.keys())
      if (!desks.some((d) => d.key === id)) agents.delete(id);
    for (const desk of desks) {
      const seat = [desk.x, desk.y - 1]; // behind the desk, facing the viewer
      let person = agents.get(desk.key);
      if (!person) {
        const h = hash(desk.key + desk.runner);
        person = {
          x: seat[0],
          y: seat[1],
          route: [],
          jobs: [],
          skin: SKIN[h % SKIN.length],
          hair: HAIR[(h >> 3) % HAIR.length],
          style: (h >> 9) % 3,
          shirt:
            desk.key === "you" ? "#c98a2b" : SHIRT[(h >> 6) % SHIRT.length],
          phase: (h % 100) / 100,
          facing: 0,
          carrying: false,
          act: "idle",
        };
        agents.set(desk.key, person);
      }
      person.desk = desk;
      person.seat = seat;
    }
  }

  function walkable(x, y) {
    return (
      x >= 0 && y >= WALL && x < cols && y < ROWS && !blocked.has(x + ":" + y)
    );
  }

  function path(from, to) {
    const key = (p) => p[0] + ":" + p[1];
    const start = [Math.round(from[0]), Math.round(from[1])];
    const seen = new Map([[key(start), null]]);
    const queue = [start];
    while (queue.length) {
      const at = queue.shift();
      if (at[0] === to[0] && at[1] === to[1]) {
        const route = [];
        for (let p = at; p; p = seen.get(key(p))) route.unshift(p);
        return route.slice(1);
      }
      for (const [dx, dy] of [
        [1, 0],
        [-1, 0],
        [0, 1],
        [0, -1],
      ]) {
        const next = [at[0] + dx, at[1] + dy];
        if (
          !seen.has(key(next)) &&
          (walkable(...next) || key(next) === key(to))
        ) {
          seen.set(key(next), at);
          queue.push(next);
        }
      }
    }
    return [to];
  }

  function update(runs, stepOf, flows) {
    if (!canvas) return;
    arrange(flows);
    for (const desk of desks) desk.docs = [];
    docs = { inbox: [], shelf: [] };
    for (const run of runs) {
      const key = deskKey(stepOf(run));
      const desk = desks.find((d) => d.key === key);
      const onDesk =
        desk &&
        (run.active || run.status === "blocked" || run.status === "waiting");
      if (run.status === "accepted") docs.shelf.push(run);
      else if (onDesk) desk.docs.push(run);
      else docs.inbox.push(run);
      const before = previous.get(run.id);
      if (before && before !== key && !reduced.matches) {
        const giver = agents.get(before);
        // Drop the folder in front of the next desk, the shelf or the inbox.
        const to = onDesk
          ? [desk.x + 1, desk.y + 1]
          : run.status === "accepted"
            ? [shelf.x, shelf.y + 1]
            : [inbox.x + 1, inbox.y];
        if (giver) giver.jobs.push(to);
      }
      previous.set(run.id, key);
    }
    for (const desk of desks) {
      const person = agents.get(desk.key);
      const current = desk.docs.find((r) => r.active) || desk.docs[0];
      person.act = !current
        ? "idle"
        : current.status === "blocked"
          ? "alert"
          : current.status === "waiting"
            ? "wait"
            : desk.step.kind === "human"
              ? "ask"
              : desk.step.kind === "check"
                ? "check"
                : desk.step.mutates
                  ? "type"
                  : "read";
    }
    canvas.setAttribute(
      "aria-label",
      handlers.summary(runs.length, desks.length),
    );
    loop();
  }

  function loop() {
    if (!root || root.offsetParent === null || document.hidden) return;
    draw(performance.now());
    if (running) return;
    running = true;
    last = performance.now();
    const frame = (now) => {
      if (!root.offsetParent || document.hidden) {
        running = false;
        return;
      }
      advance((now - last) / 1000);
      last = now;
      draw(now);
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  function advance(dt) {
    dt = Math.min(dt, 0.1);
    for (const p of agents.values()) {
      if (!p.route.length) {
        const home = p.x === p.seat[0] && p.y === p.seat[1];
        p.carrying = false;
        p.coffee = false;
        if (home) p.facing = 0;
        if (p.jobs.length && home) {
          const to = p.jobs.shift();
          const there = path(p.seat, to);
          p.route = [...there, ...path(to, p.seat)];
          p.dropAt = there.length;
          p.carrying = true;
        } else if (!home) {
          p.route = path([p.x, p.y], p.seat);
        } else if (
          p.act === "idle" &&
          !reduced.matches &&
          Math.random() < dt * 0.02
        ) {
          const cup = [coffee.x + 1, coffee.y];
          p.route = [...path(p.seat, cup), ...path(cup, p.seat)];
          p.coffee = true;
        }
        continue;
      }
      const [tx, ty] = p.route[0];
      const dx = tx - p.x,
        dy = ty - p.y;
      const dist = Math.hypot(dx, dy);
      const move = SPEED * dt;
      p.facing =
        Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 1 : -1) : dy < 0 ? 2 : 0;
      if (dist <= move) {
        p.x = tx;
        p.y = ty;
        p.route.shift();
        if (p.carrying && --p.dropAt <= 0) p.carrying = false;
      } else {
        p.x += (dx / dist) * move;
        p.y += (dy / dist) * move;
      }
    }
  }

  /* ---------- drawing (art pixels scaled by Z) ---------- */
  function px(x, y, w, h, color) {
    ctx.fillStyle = color;
    ctx.fillRect(
      Math.round(x * Z),
      Math.round(y * Z),
      Math.max(1, Math.round(w * Z)),
      Math.max(1, Math.round(h * Z)),
    );
  }
  function write(value, x, y, align, font, color) {
    ctx.fillStyle = color;
    ctx.font = font;
    ctx.textAlign = align;
    ctx.fillText(value, x * Z, y * Z);
  }
  function short(value, size) {
    return value.length > size ? value.slice(0, size - 1) + "…" : value;
  }

  function drawRoom(now) {
    const night = dark();
    // Back wall with a baseboard.
    px(0, 0, cols * T, WALL * T, night ? "#232a36" : "#e3e7ec");
    px(0, WALL * T - 4, cols * T, 4, night ? "#171c25" : "#bcc3cc");
    // Windows: sky by day, stars and a moon at night.
    for (let x = 6; x < cols - 7; x += 9) {
      const wx = x * T,
        wy = 5,
        ww = 3 * T,
        wh = WALL * T - 14;
      px(wx - 2, wy - 2, ww + 4, wh + 4, night ? "#3a4252" : "#9aa7b5");
      px(wx, wy, ww, wh, night ? "#0d1426" : "#bfe0f5");
      if (night) {
        for (const [sx, sy] of stars)
          if (
            sx >= wx &&
            sx < wx + ww &&
            sy >= wy &&
            sy < wy + wh &&
            (reduced.matches || Math.floor(now / 900 + sx) % 9 !== 0)
          )
            px(sx, sy, 1, 1, "#e8ecff");
        if (x === 6) px(wx + ww - 11, wy + 4, 5, 5, "#f4f0d0");
      } else {
        px(wx + 6, wy + wh - 8, 12, 3, "#ffffff");
        px(wx + 20, wy + wh - 12, 9, 3, "#ffffff");
        px(wx, wy, ww, 3, "rgba(255,255,255,0.55)");
      }
      px(wx + ww / 2 - 1, wy, 2, wh, night ? "#3a4252" : "#9aa7b5");
    }
    // Clock with the real time.
    const cx = 3 * T,
      cy = 13;
    px(cx - 7, cy - 7, 14, 14, "#5e6670");
    px(cx - 6, cy - 6, 12, 12, night ? "#e6e9ed" : "#ffffff");
    const time = new Date();
    const hour =
      ((time.getHours() % 12) + time.getMinutes() / 60) * (Math.PI / 6);
    const minute = time.getMinutes() * (Math.PI / 30);
    ctx.strokeStyle = "#1b1f24";
    ctx.lineWidth = Z;
    for (const [angle, length] of [
      [hour, 3],
      [minute, 5],
    ]) {
      ctx.beginPath();
      ctx.moveTo(cx * Z, cy * Z);
      ctx.lineTo(
        (cx + Math.sin(angle) * length) * Z,
        (cy - Math.cos(angle) * length) * Z,
      );
      ctx.stroke();
    }
    // Bookshelf on the right of the wall.
    const bx = (cols - 5) * T;
    px(bx, 5, 3 * T, WALL * T - 9, "#6e5236");
    px(bx + 2, 17, 3 * T - 4, 2, "#5a4330");
    const books = ["#c0694c", "#4f7cac", "#6f9e5b", "#c9a13f", "#8a6fb0"];
    for (let i = 0; i < 12; i++)
      px(bx + 3 + i * 3.6, 8 + (i % 2), 3, 9 - (i % 2), books[i % 5]);
    for (let i = 0; i < 11; i++)
      px(
        bx + 3 + i * 3.9,
        20 + (i % 3 === 0 ? 1 : 0),
        3,
        7,
        books[(i + 2) % 5],
      );
    // Floor planks.
    const a = night ? "#2a2520" : "#e8d9c4",
      b = night ? "#302a24" : "#dfcfb7",
      seam = night ? "#221e1a" : "#d3c1a6";
    for (let y = WALL; y < ROWS; y++)
      for (let x = 0; x < cols; x++) {
        px(
          x * T,
          y * T,
          T,
          T,
          (y + Math.floor((x + (y % 2) * 2) / 4)) % 2 ? a : b,
        );
        px(x * T, y * T + T - 1, T, 1, seam);
      }
    // Lounge rug.
    const top = (ROWS - 3) * T + 4;
    px(0, top, 5 * T, 3 * T - 8, night ? "#2f3a55" : "#c6d0e8");
    px(4, top + 4, 5 * T - 8, 3 * T - 16, night ? "#35425f" : "#d6ddef");
  }

  function drawCoffee() {
    const x = coffee.x * T,
      y = coffee.y * T;
    px(x + 1, y - 13, 14, 26, "#1b1f24");
    px(x + 2, y - 12, 12, 24, "#4b525c");
    px(x + 4, y - 9, 8, 5, "#c84b31");
    px(x + 5, y + 4, 6, 5, "#f4efe1");
    px(x - 13, y - 8, 10, 16, "#5f8f4d"); // plant
    px(x - 12, y - 11, 6, 6, "#6f9e5b");
    px(x - 12, y + 8, 8, 5, "#8c6a45");
    px(x + 20, y - 15, 10, 8, "#9fd1ff"); // water cooler
    px(x + 19, y - 7, 12, 19, "#e6e9ed");
    px(x + 22, y - 2, 6, 2, "#9aa7b5");
  }

  function paperStack(x, y, count, mark) {
    for (let i = 0; i < Math.min(count, 4); i++)
      px(x + i, y - i * 2, 10, 7, i % 2 ? "#f4efe1" : "#fbf7ec");
    if (count > 1)
      write(
        String(count),
        x + 5,
        y + 18,
        "center",
        "600 10px " + token("--mono", "monospace"),
        token("--ink-2", "#333"),
      );
    if (mark) px(x + 3, y - 12, 4, 4, mark === "!" ? "#b83a3a" : "#c98a2b");
  }

  function drawPlate(d) {
    const x = d.x * T + T,
      y = d.y * T + 29; // name plate under the desk
    const title = short(d.label, 18);
    const runner = d.runner ? short(d.runner, 24) : "";
    ctx.font = "650 10px " + token("--ui", "sans-serif");
    const titleWidth = ctx.measureText(title).width;
    ctx.font = "10px " + token("--mono", "monospace");
    const runnerWidth = runner ? ctx.measureText(runner).width : 0;
    const w = Math.max(titleWidth, runnerWidth) / Z + 10;
    const h = runner ? 21 : 12;
    px(
      x - w / 2,
      y - 10,
      w,
      h,
      dark() ? "rgba(23,27,33,0.88)" : "rgba(255,255,255,0.9)",
    );
    write(
      title,
      x,
      y - 1,
      "center",
      "650 10px " + token("--ui", "sans-serif"),
      token("--ink", "#222"),
    );
    if (runner)
      write(
        runner,
        x,
        y + 8,
        "center",
        "10px " + token("--mono", "monospace"),
        token("--muted", "#666"),
      );
  }

  function drawDeskBack(d, now) {
    // Monitor sits behind the hands but in front of the seated body.
    const x = d.x * T,
      y = d.y * T;
    const active = d.docs.some((r) => r.active);
    if (d.step.kind === "human") {
      px(x + 18, y - 7, 12, 8, "#1b1f24");
      px(x + 19, y - 6, 10, 6, "#f4efe1");
      px(x + 19, y - 6, 10, 2, "#c98a2b");
      return;
    }
    if (active && dark()) {
      const cx = (x + 22) * Z,
        cy = (y - 8) * Z;
      const glow = ctx.createRadialGradient(cx, cy, 2, cx, cy, 28 * Z);
      glow.addColorStop(0, "rgba(127,182,234,0.32)");
      glow.addColorStop(1, "rgba(127,182,234,0)");
      ctx.fillStyle = glow;
      ctx.fillRect((x - 8) * Z, (y - 34) * Z, 60 * Z, 54 * Z);
    }
    px(x + 13, y - 16, 18, 13, "#1f242b");
    const screen = !active
      ? "#3c4654"
      : d.step.kind === "check"
        ? "#16301f"
        : "#122338";
    px(x + 14, y - 15, 16, 10, screen);
    if (active) {
      const tick = reduced.matches ? 0 : Math.floor(now / 180);
      const tone =
        d.step.kind === "check"
          ? "#8fd4a4"
          : d.step.mutates
            ? "#9fd1ff"
            : "#e2d59f";
      for (let i = 0; i < 4; i++) {
        const length = 3 + ((hash(d.key + (tick + i)) >> 3) % 10);
        px(
          x + 15 + (i % 2) * 2,
          y - 14 + i * 2.4,
          Math.min(length, 13),
          1,
          tone,
        );
      }
    }
    px(x + 21, y - 3, 2, 3, "#1f242b");
  }

  function drawDeskFront(d) {
    const x = d.x * T,
      y = d.y * T;
    const hot = hover && hover.desk === d;
    px(x - 1, y, 2 * T + 2, 6, "#5e4630");
    px(x, y + 1, 2 * T, 4, hot ? "#b08a5c" : "#9c7a52"); // top
    px(x, y + 6, 2 * T, 8, hot ? "#8a6a45" : "#7a5c3c"); // front panel
    px(x + 13, y + 9, 6, 1.5, "#5e4630"); // drawer handle
    px(x + 1, y + 14, 2, 3, "#5e4630");
    px(x + 2 * T - 3, y + 14, 2, 3, "#5e4630");
    if (d.step.kind !== "human") px(x + 17, y + 1.5, 10, 2, "#2a2f36"); // keyboard
    px(x + 27, y + 1, 3, 3, "#f4efe1"); // mug
    if (d.docs.length) {
      const mark = d.docs.some((r) => r.status === "blocked")
        ? "!"
        : d.step.kind === "human"
          ? "?"
          : "";
      paperStack(x + 3, y - 3, d.docs.length, mark);
    }
  }

  function drawTray(spot, runs, caption, colors) {
    const x = spot.x * T,
      y = spot.y * T;
    px(x, y + 4, T + 10, 10, "#7a5c3c");
    px(x + 1, y + 5, T + 8, 2, "#9c7a52");
    for (let i = 0; i < Math.min(runs.length, 6); i++)
      px(
        x + 3 + (i % 3) * 7,
        y - 2 - Math.floor(i / 3) * 6,
        6,
        6,
        colors[i % colors.length],
      );
    write(
      caption + " · " + runs.length,
      x + 2,
      y - 12,
      "left",
      "650 10px " + token("--ui", "sans-serif"),
      token("--ink-2", "#444"),
    );
  }

  function bubble(x, y, value, fill, ink, now) {
    const bob = reduced.matches ? 0 : Math.sin(now / 260) * 1.2;
    px(x - 6, y - 14 + bob, 12, 11, "#1b1f24");
    px(x - 5, y - 13 + bob, 10, 9, fill);
    px(x - 1, y - 3 + bob, 2, 2, "#1b1f24");
    write(
      value,
      x,
      y - 5.5 + bob,
      "center",
      "700 13px " + token("--mono", "monospace"),
      ink,
    );
  }

  function drawPerson(p, now) {
    const x = p.x * T + 10,
      y = p.y * T - 2;
    const moving = p.route.length > 0;
    const t = now / 1000 + p.phase * 10;
    const seated = !moving && p.x === p.seat[0] && p.y === p.seat[1];
    const frame = moving && !reduced.matches ? Math.floor(t * 8) % 4 : 0;
    const bounce = frame === 1 || frame === 3 ? 1 : 0;
    const outline = "#1b1f24";
    if (hover && hover.desk === p.desk)
      px(x - 2, y + 20, 16, 2, token("--accent", "#2446a8"));
    px(x + 1, y + 19, 10, 2, "rgba(0,0,0,0.18)");
    if (!seated) {
      const swing = [0, 2, 0, -2][frame];
      px(x + 2, y + 13 - bounce, 3, 6 + swing / 2, outline);
      px(x + 7, y + 13 - bounce, 3, 6 - swing / 2, outline);
      px(x + 3, y + 13 - bounce, 1, 5 + swing / 2, "#3a3f4a");
      px(x + 8, y + 13 - bounce, 1, 5 - swing / 2, "#3a3f4a");
    }
    px(x + 1, y + 6 - bounce, 10, 9, outline);
    px(x + 2, y + 7 - bounce, 8, 7, p.shirt);
    px(x + 5, y + 7 - bounce, 2, 2, "#f4efe1"); // collar
    px(x + 2, y - 2 - bounce, 8, 9, outline);
    px(x + 3, y - 1 - bounce, 6, 7, p.skin);
    const back = p.facing === 2;
    if (p.style === 2) {
      px(x + 2, y - 2 - bounce, 8, 3, "#2f5f8a"); // cap
      px(x + (p.facing === -1 ? -1 : 8), y - bounce, 4, 1, "#2f5f8a");
    } else {
      px(x + 3, y - 1 - bounce, 6, back ? 7 : 2, p.hair);
      if (p.style === 1) {
        px(x + 2, y - bounce, 1, 6, p.hair);
        px(x + 9, y - bounce, 1, 6, p.hair);
      }
    }
    if (!back) {
      if (p.facing === 1) px(x + 7, y + 2 - bounce, 1, 1, outline);
      else if (p.facing === -1) px(x + 4, y + 2 - bounce, 1, 1, outline);
      else {
        px(x + 4, y + 2 - bounce, 1, 1, outline);
        px(x + 7, y + 2 - bounce, 1, 1, outline);
      }
    }
    let arm = 0;
    if (!reduced.matches && seated) {
      if (p.act === "type") arm = Math.floor(t * 9) % 2;
      if (p.act === "read") arm = Math.floor(t * 1.2) % 2;
    }
    px(x, y + 8 + arm - bounce, 2, 5, p.shirt);
    px(x + 10, y + 8 + (1 - arm) - bounce, 2, 5, p.shirt);
    if (p.act === "ask" && seated) px(x + 10, y - 2, 2, 8, p.skin);
    if (p.carrying) {
      px(x + 8, y + 7 - bounce, 7, 6, outline);
      px(x + 9, y + 8 - bounce, 5, 4, "#e2b93b");
    }
    if (p.coffee && moving) px(x + 11, y + 8 - bounce, 3, 4, "#f4efe1");
  }

  function drawStatus(p, now) {
    const seated = !p.route.length && p.x === p.seat[0] && p.y === p.seat[1];
    if (!seated) return;
    const x = p.x * T + 16,
      y = p.y * T - 4;
    if (p.act === "ask") bubble(x, y, "?", "#f8eedb", "#9a6412", now);
    else if (p.act === "alert") bubble(x, y, "!", "#f8e4e2", "#b83a3a", now);
    else if (p.act === "wait") bubble(x, y, "…", "#e4edfb", "#2f6fd1", now);
    else if (p.act === "check") {
      const k = reduced.matches ? 0 : Math.floor(now / 150) % 4;
      for (let i = 0; i < 4; i++)
        px(x - 5 + i * 3, y - 6, 2, 2, i === k ? "#2e7d4f" : "#b9d8c3");
    }
  }

  function draw(now) {
    if (!ctx) return;
    ctx.clearRect(0, 0, width, ROWS * TILE);
    drawRoom(now);
    if (coffee) drawCoffee();
    if (inbox)
      drawTray(inbox, docs.inbox, handlers.inboxLabel(), [
        "#fbf7ec",
        "#f4efe1",
      ]);
    if (shelf)
      drawTray(shelf, docs.shelf, handlers.shelfLabel(), [
        "#6cc58f",
        "#e2b93b",
        "#8aa4ff",
      ]);
    // Painter's order by row: monitor, person, desk front; labels on top.
    const people = [...agents.values()];
    for (let row = WALL; row < ROWS; row++) {
      for (const d of desks) if (d.y === row + 1) drawDeskBack(d, now);
      for (const p of people) if (Math.round(p.y) === row) drawPerson(p, now);
      for (const d of desks) if (d.y === row) drawDeskFront(d);
    }
    for (const d of desks) drawPlate(d);
    for (const p of people) drawStatus(p, now);
    if (!desks.length)
      write(
        handlers.emptyLabel(),
        width / Z / 2,
        ((ROWS + WALL) * T) / 2,
        "center",
        "12px " + token("--ui", "sans-serif"),
        token("--muted", "#666"),
      );
  }

  function pick(e) {
    const box = canvas.getBoundingClientRect();
    const ax = (e.clientX - box.left) / Z,
      ay = (e.clientY - box.top) / Z;
    for (const d of desks) {
      const x = d.x * T,
        y = d.y * T;
      if (ax < x - 2 || ax > x + 2 * T + 2 || ay < y - 22 || ay > y + 40)
        continue;
      const current = d.docs.find((r) => r.active) || d.docs[0];
      if (current && ax <= x + 14 && ay >= y - 14 && ay <= y + 8)
        return { run: current, desk: d, label: handlers.docsLabel(d.docs) };
      return {
        step: d.step,
        desk: d,
        label:
          d.label +
          (d.runner ? " · " + d.runner : "") +
          " · " +
          handlers.deskHint(d.docs.length),
      };
    }
    for (const [spot, runs] of [
      [inbox, docs.inbox],
      [shelf, docs.shelf],
    ]) {
      if (!spot || !runs.length) continue;
      const x = spot.x * T,
        y = spot.y * T;
      if (ax >= x && ax <= x + T + 10 && ay >= y - 14 && ay <= y + 16)
        return { run: runs[0], label: handlers.docsLabel(runs) };
    }
    return null;
  }

  window.ffaiOffice = { mount, update };
})();

/* Per-browser memory for choices and drafts. Storage may be unavailable (private
 * windows, cleared data); every access is guarded and the page works without it. */
"use strict";
function recall(key, fallback) {
  try {
    const raw = localStorage.getItem("ffai-ui:" + key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}
function remember(key, value) {
  try {
    localStorage.setItem("ffai-ui:" + key, JSON.stringify(value));
  } catch {}
}
/* A Map whose entries survive reloads; values must be JSON. */
function persistentMap(key, limit = 200) {
  const map = new Map(recall(key, []));
  const save = () => remember(key, [...map.entries()].slice(-limit));
  return {
    get: (k) => map.get(k),
    has: (k) => map.has(k),
    set(k, v) {
      map.set(k, v);
      save();
      return this;
    },
    delete(k) {
      const had = map.delete(k);
      save();
      return had;
    },
  };
}

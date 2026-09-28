/* Per-browser memory for choices and drafts. Storage may be unavailable (private
 * windows, cleared data); every access is guarded and the page works without it. */

const PREFIX = "ffai-ui:";

export function recall(key, fallback) {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function remember(key, value) {
  try {
    if (value === undefined) localStorage.removeItem(PREFIX + key);
    else localStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch {
    /* Storage is a convenience; nothing depends on it succeeding. */
  }
}

/** A Map whose entries survive reloads; values must be JSON. */
export function persistentMap(key, limit = 200) {
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

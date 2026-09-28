/* HTTP client for the loopback server. Reads are GETs; every mutation is a POST
 * carrying the session token the state endpoint hands out. */

let token = "";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
  get conflict() {
    return this.status === 409;
  }
}

export function setToken(value) {
  token = value || token;
}

async function parse(response) {
  const body = response.status === 304 ? null : await response.json();
  if (!response.ok && response.status !== 304)
    throw new ApiError(body?.error || response.statusText, response.status);
  return body;
}

export async function get(path, params) {
  const query = params
    ? "?" +
      new URLSearchParams(
        Object.entries(params).filter(([, v]) => v !== undefined && v !== ""),
      )
    : "";
  return parse(await fetch("/api/" + path + query));
}

export async function post(action, body = {}) {
  return parse(
    await fetch("/api/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-FFAI-Token": token },
      body: JSON.stringify(body),
    }),
  );
}

/** A task command bound to the version the operator saw. */
export function command(name, run, extra = {}) {
  return post(name, {
    id: run.id,
    version: run.version,
    request_id: crypto.randomUUID(),
    ...extra,
  });
}

let stateTag = null;
let stateBody = null;
/** Polling revalidates with the last ETag; an unchanged board is not re-sent. */
export async function fetchState() {
  const response = await fetch("/api/state", {
    headers: stateTag ? { "If-None-Match": stateTag } : {},
  });
  if (response.status === 304 && stateBody)
    return { state: stateBody, changed: false };
  const body = await parse(response);
  stateTag = response.headers.get("ETag");
  stateBody = body;
  setToken(body.token);
  return { state: body, changed: true };
}

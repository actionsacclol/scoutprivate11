// Resolves the HTTP base URL + auth token of the Python backend exactly once.
//
// Inside Electron the supervisor probes a free port from 8000 upward and mints
// a random per-launch API token; both come back from
// window.scout.backend.connection() and all fetches then go direct to
// http://127.0.0.1:<port> carrying X-Scout-Token. Under plain `vite` (no
// Electron) the base is '' so requests stay relative and hit the dev-server
// /api proxy, and there is no token — a backend started without one accepts
// unauthenticated requests (see backend/kryptscout/api/main._api_token_gate).

interface Conn {
  base: string;
  token: string | null;
}

let cached: Conn | null = null;
let inflight: Promise<Conn> | null = null;

async function resolve(): Promise<Conn> {
  try {
    if (window.scout) {
      const conn = await window.scout.backend.connection();
      if (conn) return { base: `http://127.0.0.1:${conn.port}`, token: conn.token };
    }
  } catch {
    /* fall through to the relative-path fallback */
  }
  return { base: '', token: null };
}

function connection(): Promise<Conn> {
  if (cached !== null) return Promise.resolve(cached);
  if (!inflight) {
    inflight = resolve().then((conn) => {
      cached = conn;
      inflight = null;
      return conn;
    });
  }
  return inflight;
}

export async function apiBase(): Promise<string> {
  return (await connection()).base;
}

/** Drop the cached base + token so the next call re-asks the supervisor — used
 *  when a health check fails (the backend may have restarted on a different
 *  port, and a restart also mints a new token). */
export function invalidateApiBase(): void {
  cached = null;
}

/**
 * THE way to call the backend. Resolves the base URL, attaches the per-launch
 * API token, and otherwise behaves exactly like fetch(). Every renderer call
 * must go through here: a request without the token is rejected 401 by the
 * backend's token gate.
 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const { base, token } = await connection();
  const headers = new Headers(init.headers);
  if (token) headers.set('X-Scout-Token', token);
  return fetch(base + path, { ...init, headers });
}

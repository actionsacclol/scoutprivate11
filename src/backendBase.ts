// Resolves the HTTP base URL of the Python backend exactly once.
//
// Inside Electron the supervisor probes a free port from 8000 upward and
// exposes it via window.scout.backend.port(); all fetches then go direct to
// http://127.0.0.1:<port>. Under plain `vite` (no Electron) the resolved base
// is '' so requests stay relative and hit the dev-server /api proxy.

let cached: string | null = null;
let inflight: Promise<string> | null = null;

async function resolve(): Promise<string> {
  try {
    if (window.scout) {
      const port = await window.scout.backend.port();
      if (port) return `http://127.0.0.1:${port}`;
    }
  } catch {
    /* fall through to the relative-path fallback */
  }
  return '';
}

export function apiBase(): Promise<string> {
  if (cached !== null) return Promise.resolve(cached);
  if (!inflight) {
    inflight = resolve().then((base) => {
      cached = base;
      inflight = null;
      return base;
    });
  }
  return inflight;
}

/** Drop the cached base so the next call re-asks the supervisor — used when a
 *  health check fails (the backend may have restarted on a different port). */
export function invalidateApiBase(): void {
  cached = null;
}

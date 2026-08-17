import { useEffect, useState } from 'react'
import { AlertTriangle, Download, RotateCw } from 'lucide-react'
import type { BackendInfo } from '@shared/types'

/**
 * The pre-license splash.
 *
 * It exists because the license verdict comes FROM the backend: until
 * /api/license answers, the app can't tell "licensed" from "unlicensed" and
 * refuses to render either. That is correct — but it used to be the whole
 * story, so any machine where the backend never came up (missing/quarantined
 * bundle, AV holding the exe, a crash loop) sat on a spinner forever with no
 * error, no logs and no way in. Premium users reported it as "it doesn't load".
 *
 * So the spinner now has an opinion. It reports what the supervisor knows,
 * and once the backend is visibly broken — or is simply taking far longer than
 * a cold start should — it hands over the two things support actually needs:
 * restart the engine, export the logs.
 */

/** Cold start imports pandas/sklearn/xgboost out of a frozen bundle; on a slow
 *  disk behind an AV scan that genuinely can take a while. The supervisor's own
 *  startup budget is 60s, so we only cry wolf past it. */
const STALL_AFTER_MS = 65_000
/** Before that, say a little more than "Starting…" so a long first run doesn't
 *  look like a hang. */
const DETAIL_AFTER_MS = 8_000

export default function StartupSplash({ backend }: { backend: BackendInfo | null }) {
  const [elapsed, setElapsed] = useState(0)
  const [restarting, setRestarting] = useState(false)
  const [exported, setExported] = useState<string | null>(null)

  useEffect(() => {
    const started = Date.now()
    const t = setInterval(() => setElapsed(Date.now() - started), 1_000)
    return () => clearInterval(t)
  }, [])

  // 'crashed' is the supervisor's verdict, not a guess — surface it at once.
  const crashed = backend?.status === 'crashed'
  const stalled = crashed || elapsed >= STALL_AFTER_MS
  const showDetail = elapsed >= DETAIL_AFTER_MS

  const restart = async () => {
    if (!window.scout) return
    setRestarting(true)
    try {
      await window.scout.backend.restart()
    } finally {
      setTimeout(() => setRestarting(false), 3_000)
    }
  }

  const exportLogs = async () => {
    if (!window.scout) return
    const res = await window.scout.backend.exportLogs()
    if (res.ok && res.path) setExported(res.path)
  }

  return (
    <div className="flex min-h-0 flex-1 items-center justify-center bg-krypt-radial p-6">
      <div className="w-full max-w-md text-center">
        {stalled ? (
          <div className="mx-auto mb-3 grid h-10 w-10 place-items-center rounded-xl bg-amber-400/10 text-amber-300">
            <AlertTriangle size={20} />
          </div>
        ) : (
          <div className="mx-auto mb-3 h-6 w-6 animate-spin rounded-full border-2 border-scout-red border-t-transparent" />
        )}

        <p className="text-sm text-krypt-muted">
          {stalled ? 'The data engine has not started' : 'Starting Krypt Scout…'}
        </p>

        {showDetail && !stalled && (
          <p className="mt-1 text-xs text-krypt-muted/60">
            {backend?.status === 'running'
              ? 'Waiting for the license check…'
              : 'Loading the model stack — the first run takes longest.'}
          </p>
        )}

        {stalled && (
          <div className="mt-5 rounded-2xl border border-white/10 bg-krypt-void/60 p-5 text-left shadow-2xl backdrop-blur">
            <p className="text-xs leading-relaxed text-krypt-muted">
              Krypt Scout reads your license through its local data engine, so the app
              can&apos;t open until that engine answers. Restarting it fixes most cases; if it
              doesn&apos;t, export the logs and send them over.
            </p>

            <dl className="mt-4 space-y-1 font-mono text-[11px] text-krypt-muted/70">
              <div className="flex gap-2">
                <dt className="w-16 shrink-0 text-krypt-muted/50">engine</dt>
                <dd className="text-white/80">{backend?.status ?? 'unknown'}</dd>
              </div>
              {backend?.port != null && (
                <div className="flex gap-2">
                  <dt className="w-16 shrink-0 text-krypt-muted/50">port</dt>
                  <dd className="text-white/80">{backend.port}</dd>
                </div>
              )}
              {backend?.lastError && (
                <div className="flex gap-2">
                  <dt className="w-16 shrink-0 text-krypt-muted/50">error</dt>
                  <dd className="min-w-0 break-words text-rose-300/90">{backend.lastError}</dd>
                </div>
              )}
            </dl>

            <div className="mt-4 flex gap-2">
              <button
                onClick={() => void restart()}
                disabled={restarting || !window.scout}
                className="flex flex-1 items-center justify-center gap-1.5 rounded-xl bg-gradient-to-r from-scout-red to-scout-red-bright px-3 py-2 text-xs font-semibold text-white shadow-lg transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <RotateCw size={13} className={restarting ? 'animate-spin' : undefined} />
                {restarting ? 'Restarting…' : 'Restart engine'}
              </button>
              <button
                onClick={() => void exportLogs()}
                disabled={!window.scout}
                className="flex flex-1 items-center justify-center gap-1.5 rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-xs font-semibold text-white/90 transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Download size={13} />
                Export logs
              </button>
            </div>

            {exported && (
              <p className="mt-2 break-all text-[11px] text-emerald-300/80">Saved to {exported}</p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

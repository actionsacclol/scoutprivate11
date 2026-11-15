/**
 * First-run setup strip.
 *
 * A new install has no results history, so until the first refresh job
 * finishes every engine rates every team the same and the boards show the
 * home-advantage constant for each game. Prices, the exchange fair and the
 * boards themselves work at once; only the app's own model numbers are
 * waiting. This says so, in one line, for the few minutes it lasts — and
 * gives a way out when the download did not finish or automation is off.
 *
 * The scheduler (electron/system/scheduler.ts) owns the job; this only
 * watches `refreshedAt`. Desktop app only: the browser preview has no
 * scheduler and renders nothing.
 */
import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, Download } from 'lucide-react'
import type { SchedulerInfo } from '@shared/types'

const POLL_MS = 4_000
/** Set by HIDE: one sport's source can stay unreachable on some networks, and
 *  a strip that returns on every launch for it is noise. Cleared the moment a
 *  refresh does complete. */
const HIDE_KEY = 'scout-setup-hidden'

function isHidden(): boolean {
  try {
    return localStorage.getItem(HIDE_KEY) === '1'
  } catch {
    return false
  }
}

function setHiddenFlag(on: boolean): void {
  try {
    if (on) localStorage.setItem(HIDE_KEY, '1')
    else localStorage.removeItem(HIDE_KEY)
  } catch {
    /* storage unavailable — hidden for this session only */
  }
}

export default function SetupBanner({ onReady }: { onReady: () => void }) {
  const api = window.scout?.scheduler
  const [info, setInfo] = useState<SchedulerInfo | null>(null)
  const [asking, setAsking] = useState(false)
  const [hidden, setHidden] = useState(isHidden)
  const wasPending = useRef(false)

  const pending = info !== null && info.refreshedAt == null

  // Keep watching only while the first refresh is outstanding.
  const watching = info === null || pending
  useEffect(() => {
    if (!api || !watching) return
    let alive = true
    const pull = () =>
      void api
        .info()
        .then((next) => {
          if (alive) setInfo(next)
        })
        .catch(() => {})
    pull()
    const t = setInterval(pull, POLL_MS)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [api, watching])

  // The moment it lands, re-pull the boards so the trained numbers show.
  useEffect(() => {
    if (wasPending.current && !pending && info !== null) onReady()
    if (!pending && info !== null) setHiddenFlag(false)
    wasPending.current = pending
  }, [pending, info, onReady])

  if (!api || !pending || !info || hidden) return null

  const running = info.inFlight === 'refresh' || asking
  const lastTry = info.lastRuns.find((r) => r.job === 'refresh')
  // Queued behind the startup settle, or about to be: still "on its way".
  const queued = !running && !lastTry && info.config.enabled
  const stuck = !running && !queued

  const hide = () => {
    setHiddenFlag(true)
    setHidden(true)
  }

  const runNow = () => {
    setAsking(true)
    void api
      .runNow('refresh')
      .then(setInfo)
      .catch(() => {})
      .finally(() => setAsking(false))
  }

  return (
    <div
      className={
        'flex shrink-0 items-center gap-2.5 border-b px-4 py-1.5 text-[11px] ' +
        (stuck
          ? 'border-amber-400/20 bg-amber-500/[0.07] text-amber-200'
          : 'border-sky-400/15 bg-sky-500/[0.06] text-sky-100/90')
      }
    >
      {stuck ? (
        <AlertTriangle size={13} className="shrink-0" />
      ) : (
        <Download size={13} className="shrink-0 animate-pulse" />
      )}
      <span className="min-w-0 flex-1 leading-relaxed">
        {stuck ? (
          lastTry ? (
            <>
              <span className="font-semibold">Setup did not finish.</span> The results download stopped
              {lastTry.error ? ` (${lastTry.error})` : lastTry.summary ? ` (${lastTry.summary})` : ''}. Until it
              completes, the model numbers for some sports rate every team the same.
            </>
          ) : (
            <>
              <span className="font-semibold">The models have no results history yet.</span> Automation is off, so
              nothing has downloaded it. Until then they rate every team the same.
            </>
          )
        ) : (
          <>
            <span className="font-semibold">Setting up:</span> downloading past results so the models can rate every
            team. About five minutes on a new install, once. Prices and boards already work — the model numbers
            fill in when it is done.
          </>
        )}
      </span>
      {stuck && (
        <button
          onClick={runNow}
          className="shrink-0 rounded-lg border border-amber-300/30 bg-amber-400/10 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-amber-100 transition hover:bg-amber-400/20"
        >
          {lastTry ? 'Try again' : 'Download now'}
        </button>
      )}
      {stuck && lastTry && (
        <button
          onClick={hide}
          title="Stop showing this. Scout still retries on every launch; RUN NOW for it is under Settings."
          className="shrink-0 rounded-lg px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-amber-200/70 transition hover:text-amber-100"
        >
          Hide
        </button>
      )}
    </div>
  )
}

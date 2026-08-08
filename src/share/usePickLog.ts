/**
 * usePickLog — the bot's full value-pick record (the BOT PICKS view of the
 * TRACKER page). Demo mode returns the bundled DEMO_PICKLOG instantly; live
 * mode fetches /api/picklog on mount and refreshes every 5 minutes. Same
 * null-tolerance as useRoi: fetchPickLog resolves null on any failure and
 * consumers keep their fallback rendering, so a 404 can never break a page.
 */
import { useEffect, useState } from 'react'
import { fetchPickLog, type PickLogReport } from '../api'
import { DEMO_PICKLOG } from '../demo'

const REFRESH_MS = 5 * 60_000

export function usePickLog(demo: boolean): PickLogReport | null {
  const [report, setReport] = useState<PickLogReport | null>(null)

  useEffect(() => {
    if (demo) {
      setReport(DEMO_PICKLOG)
      return
    }
    setReport(null)
    let alive = true
    const load = () =>
      void fetchPickLog().then((r) => {
        // Keep the last good payload on transient failures mid-session.
        if (alive && r) setReport(r)
      })
    load()
    const t = window.setInterval(load, REFRESH_MS)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [demo])

  return report
}

/**
 * useRoi — the one hook every ROI surface shares. Demo mode returns the
 * bundled DEMO_ROI payload instantly; live mode fetches /api/roi on mount and
 * refreshes every 5 minutes. The endpoint may not exist yet (parallel backend
 * work) — fetchRoi resolves null on any failure and consumers keep their
 * fallback rendering, so a 404 can never break a page.
 */
import { useEffect, useState } from 'react'
import { fetchRoi, type RoiSummary } from '../api'
import { DEMO_ROI } from '../demo'

const REFRESH_MS = 5 * 60_000

export function useRoi(demo: boolean): RoiSummary | null {
  const [roi, setRoi] = useState<RoiSummary | null>(null)

  useEffect(() => {
    if (demo) {
      setRoi(DEMO_ROI)
      return
    }
    setRoi(null)
    let alive = true
    const load = () =>
      void fetchRoi().then((r) => {
        // Keep the last good payload on transient failures mid-session.
        if (alive && r) setRoi(r)
      })
    load()
    const t = window.setInterval(load, REFRESH_MS)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [demo])

  return roi
}

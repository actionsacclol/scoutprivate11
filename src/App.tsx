import { useCallback, useEffect, useRef, useState } from 'react'
import type { BackendInfo } from '@shared/types'
import type { BoardMap, Sport } from './types'
import { checkHealth, describeBoardError, fetchAllBoards, fetchBoard, fetchLicense, heartbeatLicense, type LicenseStatus } from './api'
import LicenseGate from './components/LicenseGate'
import StartupSplash from './components/StartupSplash'
import SetupBanner from './components/SetupBanner'
import { DEMO_BOARDS } from './demo'
import TitleBar from './components/TitleBar'
import Sidebar, { type PageId } from './components/Sidebar'
import DashboardPage from './components/DashboardPage'
import TodayPage from './components/TodayPage'
import BoardsPage from './components/BoardsPage'
import TerminalPage from './components/TerminalPage'
import PicksPage from './components/PicksPage'
import BonusesPage from './components/BonusesPage'
import DiscordPage from './components/DiscordPage'
import ParlayPage from './components/ParlayPage'
import TrackerPage from './components/TrackerPage'
import Onboarding from './components/Onboarding'
import TourGuide from './components/TourGuide'
import ValuePage from './components/ValuePage'
import PerfPage from './components/PerfPage'
import BacktestPage from './components/BacktestPage'
import ModelsPage from './components/ModelsPage'
import IntelPage from './components/IntelPage'
import AutobetPage from './components/AutobetPage'
import AgentsPage from './components/AgentsPage'
import SettingsPage from './components/SettingsPage'
import AboutPage from './components/AboutPage'
import { UiModeContext, loadUiMode, saveUiMode, type UiMode } from './uiMode'

const POLL_MS = 60_000

export default function App() {
  // SIMPLE (default) hides the quant pages and lands on PICKS; PRO is the
  // full terminal and lands on BOARDS.
  const [uiMode, setUiModeState] = useState<UiMode>(loadUiMode)
  // Suite convention: every Krypt tool lands on its dashboard.
  const [page, setPage] = useState<PageId>('dashboard')
  const [active, setActive] = useState<Sport>('nba')
  const [boards, setBoards] = useState<BoardMap>({})
  // Whole-app license gate. 'checking' until the backend answers /api/license.
  const [licenseState, setLicenseState] = useState<'checking' | 'licensed' | 'unlicensed'>('checking')
  const [licenseStatus, setLicenseStatus] = useState<LicenseStatus | null>(null)
  const [apiUp, setApiUp] = useState<boolean | null>(null)
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null)
  const [sportFetchedAt, setSportFetchedAt] = useState<Partial<Record<Sport, Date>>>({})
  // Sports whose last board request FAILED, with why. A failed sport keeps the
  // slate it last loaded; this is what lets the UI say "couldn't load" instead
  // of "no games scheduled".
  const [boardErrors, setBoardErrors] = useState<Partial<Record<Sport, string>>>({})
  const [loading, setLoading] = useState(false)
  // null = auto (demo follows API health); true/false = user override
  const [demoOverride, setDemoOverride] = useState<boolean | null>(null)
  const [backend, setBackend] = useState<BackendInfo | null>(null)
  // Jump-from-VALUE target: the board card to flash-highlight.
  const [highlightId, setHighlightId] = useState<string | null>(null)
  const inFlight = useRef(false)

  const demo = demoOverride ?? apiUp === false

  const boardLoaded = useCallback((sport: Sport, cards: BoardMap[Sport]) => {
    setBoards((prev) => ({ ...prev, [sport]: cards }))
    setSportFetchedAt((prev) => ({ ...prev, [sport]: new Date() }))
    setBoardErrors((prev) => {
      if (!(sport in prev)) return prev
      const next = { ...prev }
      delete next[sport]
      return next
    })
  }, [])
  const boardFailed = useCallback((sport: Sport, reason: string) => {
    setBoardErrors((prev) => ({ ...prev, [sport]: reason }))
  }, [])

  const setUiMode = useCallback((m: UiMode) => {
    setUiModeState(m)
    saveUiMode(m)
    // Every tab is visible in both modes now, so switching never strands the
    // user on a hidden page — no page bounce needed.
  }, [])

  const refresh = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    setLoading(true)
    try {
      const healthy = await checkHealth()
      setApiUp(healthy)
      if (healthy) {
        // Poll every sport's board so the cross-sport value board stays
        // complete. Each board paints as it lands; a sport that fails keeps
        // the slate it had and is flagged in boardErrors.
        await fetchAllBoards(boardLoaded, boardFailed)
      }
      setFetchedAt(new Date())
    } finally {
      inFlight.current = false
      setLoading(false)
    }
  }, [boardLoaded, boardFailed])

  /** Force-refetch a single sport's board (SETTINGS → DATA). */
  const refreshSport = useCallback(async (sport: Sport) => {
    try {
      boardLoaded(sport, await fetchBoard(sport))
    } catch (err) {
      // Keep the stale slate, and say why the refetch failed.
      boardFailed(sport, describeBoardError(err))
    }
  }, [boardLoaded, boardFailed])

  useEffect(() => {
    void refresh()
    const t = setInterval(() => void refresh(), POLL_MS)
    return () => clearInterval(t)
  }, [refresh])

  // Track the supervisor's backend state (Electron only) and re-poll as soon
  // as it reports running — no waiting out the 60s cycle after a cold start.
  useEffect(() => {
    if (!window.scout) return
    void window.scout.backend.info().then(setBackend)
    return window.scout.backend.onStatus(setBackend)
  }, [])
  const backendRunning = backend?.status === 'running'
  useEffect(() => {
    if (backendRunning) void refresh()
  }, [backendRunning, refresh])

  // ── License gate ────────────────────────────────────────────────────────
  // Poll the local license status (backend-side is_licensed); a null response
  // (backend not up yet) leaves us on the 'checking' splash rather than
  // flashing the app. Revocation is enforced by the heartbeat below.
  //
  // An EXPIRED stored token is NOT a verdict. The license server reissues on
  // heartbeat regardless of expiry (hornbot verifyToken ignores exp by design),
  // so a user coming back after a few days offline is still licensed — they
  // just haven't asked yet. Walling them on the local check alone would shove
  // LicenseGate in their face for the length of one round-trip, which reads as
  // "my license broke". So a stale token buys one renewal attempt before we
  // decide anything. This matters now that the token TTL is 48h rather than a
  // week (hornbot src/lib/scout-license.ts) — "away for a weekend" hits it.
  const renewAttempted = useRef(false)
  const checkLicense = useCallback(async () => {
    const s = await fetchLicense()
    if (!s) return // backend not up yet — hold the splash
    setLicenseStatus(s)
    if (s.licensed) {
      renewAttempted.current = false // re-arm, so a later expiry also gets one
      setLicenseState('licensed')
      return
    }
    if (s.reason === 'expired_or_invalid' && !renewAttempted.current) {
      const renewed = await heartbeatLicense()
      if (renewed) {
        // The server answered — its word is final, and we don't re-ask on
        // every 20s poll from here.
        renewAttempted.current = true
        setLicenseStatus(renewed)
        setLicenseState(renewed.licensed ? 'licensed' : 'unlicensed')
        return
      }
      // Unreachable: leave the latch off so the next poll retries, and fall
      // through to the local verdict, which is the truthful one when we can't
      // reach anyone. 'no_license' skips all of this — nothing to renew, so
      // first-run activation still shows the gate immediately.
    }
    setLicenseState('unlicensed')
  }, [])

  useEffect(() => {
    void checkLicense()
    const t = setInterval(() => void checkLicense(), 20_000)
    return () => clearInterval(t)
  }, [checkLicense])

  useEffect(() => {
    if (backendRunning) void checkLicense()
  }, [backendRunning, checkLicense])

  // Renew the signed token + enforce revocation (a revoked key clears the token
  // server-side within one heartbeat, which then walls the app on next poll).
  useEffect(() => {
    const beat = async () => {
      const s = await heartbeatLicense()
      if (s) {
        setLicenseStatus(s)
        setLicenseState(s.licensed ? 'licensed' : 'unlicensed')
      }
    }
    void beat()
    const t = setInterval(() => void beat(), 30 * 60_000)
    return () => clearInterval(t)
  }, [])

  const data: BoardMap = demo ? DEMO_BOARDS : boards

  const jumpToGame = useCallback((sport: Sport, gameId: string) => {
    setActive(sport)
    setHighlightId(gameId)
    setPage('boards')
  }, [])

  return (
    <UiModeContext.Provider value={uiMode}>
    <div className="flex h-screen flex-col overflow-hidden bg-krypt-void">
      <TitleBar backend={backend} mode={uiMode} onMode={setUiMode} />
      {licenseState === 'unlicensed' ? (
        <LicenseGate
          status={licenseStatus}
          onActivated={(s) => {
            setLicenseStatus(s)
            setLicenseState('licensed')
          }}
        />
      ) : licenseState === 'checking' ? (
        // Holds until the backend answers /api/license. It reports the
        // supervisor's state and offers restart/export once that stops looking
        // like a slow cold start — see StartupSplash.
        <StartupSplash backend={backend} />
      ) : (
        <>
      <Onboarding />
      <TourGuide onNav={setPage} />
      <SetupBanner onReady={refresh} />
      <div className="flex min-h-0 flex-1">
        <Sidebar page={page} onNav={setPage} />
        <div className="flex min-w-0 flex-1 flex-col bg-krypt-radial">
          {page === 'dashboard' && (
            <DashboardPage boards={data} demo={demo} apiUp={apiUp} onNav={(p) => setPage(p as PageId)} />
          )}
          {page === 'today' && (
            <TodayPage
              boards={data}
              demo={demo}
              apiUp={apiUp}
              fetchedAt={fetchedAt}
              loading={loading}
              onRefresh={() => void refresh()}
              onToggleDemo={() => setDemoOverride(!demo)}
            />
          )}
          {page === 'boards' && (
            <BoardsPage
              boards={data}
              active={active}
              demo={demo}
              apiUp={apiUp}
              fetchedAt={fetchedAt}
              loading={loading}
              boardErrors={demo ? {} : boardErrors}
              highlightId={highlightId}
              onSport={(s) => {
                setActive(s)
                setHighlightId(null)
              }}
              onRefresh={() => void refresh()}
              onToggleDemo={() => setDemoOverride(!demo)}
              onClearHighlight={() => setHighlightId(null)}
            />
          )}
          {page === 'terminal' && <TerminalPage boards={data} demo={demo} apiUp={apiUp} />}
          {page === 'picks' && (
            <PicksPage
              boards={data}
              demo={demo}
              apiUp={apiUp}
              fetchedAt={fetchedAt}
              loading={loading}
              onRefresh={() => void refresh()}
              onToggleDemo={() => setDemoOverride(!demo)}
            />
          )}
          {page === 'parlay' && <ParlayPage boards={data} demo={demo} />}
          {page === 'tracker' && <TrackerPage boards={data} demo={demo} apiUp={apiUp} />}
          {page === 'autobet' && <AutobetPage demo={demo} apiUp={apiUp} />}
          {page === 'bonuses' && <BonusesPage />}
          {page === 'discord' && <DiscordPage boards={data} demo={demo} />}
          {page === 'value' && (
            <ValuePage boards={data} demo={demo} apiUp={apiUp} onJump={jumpToGame} />
          )}
          {page === 'perf' && <PerfPage demo={demo} apiUp={apiUp} />}
          {page === 'backtest' && <BacktestPage demo={demo} apiUp={apiUp} />}
          {page === 'models' && <ModelsPage boards={data} demo={demo} apiUp={apiUp} />}
          {page === 'intel' && <IntelPage boards={data} demo={demo} apiUp={apiUp} />}
          {page === 'agents' && <AgentsPage demo={demo} apiUp={apiUp} />}
          {page === 'settings' && (
            <SettingsPage
              boards={data}
              demo={demo}
              apiUp={apiUp}
              backend={backend}
              sportFetchedAt={sportFetchedAt}
              boardErrors={demo ? {} : boardErrors}
              onToggleDemo={() => setDemoOverride(!demo)}
              onRefreshSport={refreshSport}
              mode={uiMode}
              onMode={setUiMode}
            />
          )}
          {page === 'about' && <AboutPage />}
        </div>
      </div>
        </>
      )}
    </div>
    </UiModeContext.Provider>
  )
}

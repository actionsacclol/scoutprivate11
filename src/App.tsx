import { useCallback, useEffect, useRef, useState } from 'react'
import type { BackendInfo } from '@shared/types'
import type { BoardMap, Sport } from './types'
import { SPORTS } from './types'
import { checkHealth, fetchAllBoards, fetchBoard, fetchLicense, heartbeatLicense, type LicenseStatus } from './api'
import LicenseGate from './components/LicenseGate'
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
  const [loading, setLoading] = useState(false)
  // null = auto (demo follows API health); true/false = user override
  const [demoOverride, setDemoOverride] = useState<boolean | null>(null)
  const [backend, setBackend] = useState<BackendInfo | null>(null)
  // Jump-from-VALUE target: the board card to flash-highlight.
  const [highlightId, setHighlightId] = useState<string | null>(null)
  const inFlight = useRef(false)

  const demo = demoOverride ?? apiUp === false

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
        // Poll every sport's board so the cross-sport value board stays complete.
        setBoards(await fetchAllBoards())
        const now = new Date()
        setSportFetchedAt(Object.fromEntries(SPORTS.map((s) => [s, now])))
      }
      setFetchedAt(new Date())
    } finally {
      inFlight.current = false
      setLoading(false)
    }
  }, [])

  /** Force-refetch a single sport's board (SETTINGS → DATA). */
  const refreshSport = useCallback(async (sport: Sport) => {
    try {
      const cards = await fetchBoard(sport)
      setBoards((prev) => ({ ...prev, [sport]: cards }))
      setSportFetchedAt((prev) => ({ ...prev, [sport]: new Date() }))
    } catch {
      /* board endpoint down — keep the stale slate */
    }
  }, [])

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
  const checkLicense = useCallback(async () => {
    const s = await fetchLicense()
    if (s) {
      setLicenseStatus(s)
      setLicenseState(s.licensed ? 'licensed' : 'unlicensed')
    }
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
        <div className="flex min-h-0 flex-1 items-center justify-center bg-krypt-radial">
          <div className="text-center">
            <div className="mx-auto mb-3 h-6 w-6 animate-spin rounded-full border-2 border-scout-red border-t-transparent" />
            <p className="text-sm text-krypt-muted">Starting Krypt Scout…</p>
          </div>
        </div>
      ) : (
        <>
      <Onboarding />
      <TourGuide onNav={setPage} />
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
          {page === 'settings' && (
            <SettingsPage
              boards={data}
              demo={demo}
              apiUp={apiUp}
              backend={backend}
              sportFetchedAt={sportFetchedAt}
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

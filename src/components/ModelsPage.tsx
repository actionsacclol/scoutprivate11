/**
 * MODELS — the engine room. One console card per prediction engine (9):
 * artifact state (ratings json / model pickle), live-vs-backtest record and a
 * CRT job console that streams the backend's refresh log. FETCH LATEST pulls
 * new results without retraining; RETRAIN nukes the pickle and rebuilds from
 * scratch. Backend runs refreshes serially, so all buttons lock while any
 * engine is in flight. Demo / API-down renders a bundled fabricated payload
 * with the controls disabled.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ChevronRight, Download, Flame, Loader2, RefreshCw } from 'lucide-react'
import type { GameCard, Sport } from '../types'
import { SPORTS, SPORT_TAG } from '../types'
import type { ModelInfo, ModelLiveRecord, ModelsReport } from '../api'
import { fetchModels, refreshModel } from '../api'
import { FALLBACK_MODEL_META, backtestAcc } from '../modelMeta'
import { Badge, Page } from './common'
import { cls } from '../utils/format'

// Engine names + backtest-accuracy fallbacks live in src/modelMeta.ts (single
// source of truth); the live /api/models payload overrides them per engine.

// Fabricated /api/models payload for demo / backend-down: realistic artifact
// sizes + ages, live records that rhyme with the demo ROI story. Tennis sits
// past the 48h freshness line on purpose so the status logic reads on screen.
const DEMO_AGE_H: Record<Sport, number> = {
  nba: 3, nhl: 7, nfl: 11, ncaaf: 21, ncaab: 5, mlb: 2, wnba: 9, soccer: 14, tennis: 63,
}
const DEMO_STATE_KB: Record<Sport, number> = {
  nba: 84, nhl: 71, nfl: 66, ncaaf: 402, ncaab: 517, mlb: 92, wnba: 38, soccer: 143, tennis: 261,
}
const DEMO_LIVE: Record<Sport, ModelLiveRecord> = {
  nba: { n: 184, accuracy: 0.6685, log_loss: 0.604, brier: 0.211 },
  nhl: { n: 156, accuracy: 0.551, log_loss: 0.678, brier: 0.246 },
  nfl: { n: 64, accuracy: 0.656, log_loss: 0.618, brier: 0.219 },
  ncaaf: { n: 88, accuracy: 0.705, log_loss: 0.566, brier: 0.196 },
  ncaab: { n: 214, accuracy: 0.696, log_loss: 0.574, brier: 0.199 },
  mlb: { n: 262, accuracy: 0.557, log_loss: 0.672, brier: 0.243 },
  wnba: { n: 47, accuracy: 0.638, log_loss: 0.631, brier: 0.226 },
  soccer: { n: 129, accuracy: 0.488, log_loss: 1.012, brier: 0.201 },
  tennis: { n: 173, accuracy: 0.647, log_loss: 0.628, brier: 0.222 },
}

/** Engine label off the live board when a prediction is present. */
function boardModelName(boards: Record<string, GameCard[]>, s: Sport): string {
  return boards[s]?.find((c) => c.prediction)?.prediction?.model ?? FALLBACK_MODEL_META[s].model
}

function demoModelList(boards: Record<string, GameCard[]>): ModelInfo[] {
  const now = Date.now()
  return SPORTS.map((s) => {
    const model = boardModelName(boards, s)
    const updated = new Date(now - DEMO_AGE_H[s] * 3_600_000).toISOString()
    const hasPickle = model.includes('xgb')
    return {
      sport: s,
      model,
      state: { exists: true, path: `models/${s}_state.json`, size_kb: DEMO_STATE_KB[s], updated_at: updated },
      pickle: hasPickle
        ? { exists: true, path: `models/${s}_model.pkl`, size_kb: 157, updated_at: updated }
        : null,
      live: DEMO_LIVE[s],
      backtest: null,
    }
  })
}

/** Placeholder entry while live but /api/models hasn't answered (or is absent). */
function unknownModel(boards: Record<string, GameCard[]>, s: Sport): ModelInfo {
  return { sport: s, model: boardModelName(boards, s), state: null, pickle: null, live: null, backtest: null }
}

// -------------------------------------------------------------------- helpers

type EngineStatus = 'fresh' | 'stale' | 'missing'
const FRESH_MS = 48 * 3_600_000

function engineStatus(m: ModelInfo): EngineStatus {
  if (!m.state?.exists) return 'missing'
  const age = Date.now() - Date.parse(m.state.updated_at)
  return Number.isFinite(age) && age < FRESH_MS ? 'fresh' : 'stale'
}

function ago(iso: string): string {
  const s = (Date.now() - Date.parse(iso)) / 1000
  if (!Number.isFinite(s)) return '—'
  if (s < 90) return 'just now'
  const m = s / 60
  if (m < 90) return `${Math.round(m)}m ago`
  const h = m / 60
  if (h < 48) return `${Math.round(h)}h ago`
  return `${Math.round(h / 24)}d ago`
}

function sizeLabel(kb: number): string {
  return kb >= 1024 ? `${(kb / 1024).toFixed(1)}MB` : `${Math.round(kb)}KB`
}

/** Engine family from the model name — pickle line + retrain copy. */
function engineKind(model: string): string {
  const m = model.toLowerCase()
  if (m.includes('xgb')) return 'XGB'
  if (m.includes('poisson')) return 'POISSON'
  if (m.includes('elo')) return 'ELO'
  return model.split('-')[0]?.toUpperCase() || 'MODEL'
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))
const utc = () => new Date().toISOString().slice(11, 19)

/** Presentation drip: the refresh response arrives complete; lines are typed
 *  out one-by-one for the terminal feel. */
const DRIP_MS = 60

// ------------------------------------------------------------------- console

type Tone = 'dim' | 'text' | 'ok' | 'err'

interface ConsoleLine {
  key: number
  t: string
  msg: string
  tone: Tone
}

interface ConsoleState {
  open: boolean
  lines: ConsoleLine[]
}

const TONE_CLS: Record<Tone, string> = {
  dim: 'text-krypt-muted/60',
  text: 'text-white/80',
  ok: 'text-emerald-300 [text-shadow:0_0_8px_rgba(52,211,153,0.55)]',
  err: 'text-rose-400 [text-shadow:0_0_8px_rgba(244,63,94,0.55)]',
}

/** CRT job well — THE WIRE's black-phosphor treatment: true-black pane,
 *  scanlines + vignette overlay, dim timestamps, glowing ok/error lines,
 *  blinking prompt. Pinned to the newest line (no scrollback). */
function ConsoleWell({
  sport,
  lines,
  running,
  cmd,
}: {
  sport: Sport
  lines: ConsoleLine[]
  running: boolean
  cmd: string | null
}) {
  return (
    <div
      className="relative mt-1.5 h-[150px] overflow-hidden rounded-lg border border-scout-red/15 px-2.5 py-2"
      style={{
        background: 'linear-gradient(180deg, rgba(6,2,5,0.94), rgba(2,1,2,0.97))',
        boxShadow: 'inset 0 0 60px rgba(225,29,72,0.055), inset 0 1px 0 rgba(255,255,255,0.03)',
      }}
    >
      <div
        className="pointer-events-none absolute inset-0 rounded-lg"
        style={{
          background:
            'repeating-linear-gradient(0deg, rgba(255,255,255,0.024) 0 1px, transparent 1px 3px), radial-gradient(130% 90% at 50% 0%, transparent 62%, rgba(0,0,0,0.35) 100%)',
        }}
      />
      <div
        className="flex h-full flex-col justify-end overflow-hidden font-mono text-[10.5px] leading-[1.55]"
        style={{ WebkitMaskImage: 'linear-gradient(180deg, transparent, #000 22px)' }}
      >
        {lines.length === 0 && (
          <div className="flex-none text-krypt-muted/45">
            engine console idle — FETCH LATEST or RETRAIN streams the job log here
          </div>
        )}
        {lines.slice(-24).map((l) => (
          <div key={l.key} className="flex-none overflow-hidden text-ellipsis whitespace-pre">
            <span className="text-[#5c4a54]">{l.t}</span>{' '}
            <span className={TONE_CLS[l.tone]}>{l.msg}</span>
          </div>
        ))}
        <div className="flex-none whitespace-pre">
          <span className="text-emerald-300/90 [text-shadow:0_0_8px_rgba(52,211,153,0.4)]">
            scout@{sport}
          </span>
          <span className="text-[#5c4a54]">:~$</span>
          <span className="text-white/70"> {running ? cmd ?? '' : ''}</span>
          <span className="animate-pulse text-scout-red-bright [text-shadow:0_0_10px_rgba(251,113,133,0.6)]">
            ▮
          </span>
        </div>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ the page

const DOT_CLS: Record<EngineStatus, string> = {
  fresh: 'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]',
  stale: 'bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.8)]',
  missing: 'bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.8)]',
}
const DOT_TITLE: Record<EngineStatus, string> = {
  fresh: 'State fresh — updated within 48h',
  stale: 'State stale — last update over 48h ago',
  missing: 'No state on disk — run FETCH LATEST',
}

function StatCell({ label, title, children }: { label: string; title?: string; children: ReactNode }) {
  return (
    <div className="min-w-0 rounded-lg border border-white/[0.06] bg-black/25 px-2.5 py-1.5" title={title}>
      <div className="text-[8.5px] font-semibold uppercase tracking-[0.16em] text-krypt-muted/70">
        {label}
      </div>
      <div className="mt-0.5 overflow-hidden text-ellipsis whitespace-nowrap font-mono text-[11px] text-white/85">
        {children}
      </div>
    </div>
  )
}

interface Props {
  boards: Record<string, GameCard[]>
  demo: boolean
  apiUp: boolean | null
}

export default function ModelsPage({ boards, demo, apiUp }: Props) {
  const live = !demo && apiUp === true

  // ---- feed ----------------------------------------------------------------
  const [report, setReport] = useState<ModelsReport | null>(null)
  useEffect(() => {
    if (!live) return
    let alive = true
    const load = () => void fetchModels().then((r) => alive && r && setReport(r))
    load()
    const t = window.setInterval(load, 60_000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [live])

  const models: ModelInfo[] = useMemo(() => {
    if (!live) return demoModelList(boards)
    const by = new Map((report?.models ?? []).map((m) => [m.sport, m]))
    return SPORTS.map((s) => by.get(s) ?? unknownModel(boards, s))
  }, [live, report, boards])

  // ---- consoles + flight ----------------------------------------------------
  const [consoles, setConsoles] = useState<Partial<Record<Sport, ConsoleState>>>({})
  const [flight, setFlight] = useState<{ sport: Sport; force: boolean } | null>(null)
  const [allRun, setAllRun] = useState<number | null>(null) // index into SPORTS while REFRESH ALL sequences
  const [armed, setArmed] = useState<Sport | null>(null) // retrain confirm-on-second-click
  const flightRef = useRef(false)
  const aliveRef = useRef(true)
  const lineSeq = useRef(0)
  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  }, [])

  const push = useCallback((sport: Sport, msg: string, tone: Tone) => {
    if (!aliveRef.current) return
    const line: ConsoleLine = { key: lineSeq.current++, t: utc(), msg, tone }
    setConsoles((prev) => {
      const cur = prev[sport] ?? { open: false, lines: [] }
      return { ...prev, [sport]: { ...cur, lines: [...cur.lines, line].slice(-120) } }
    })
  }, [])

  const toggleConsole = (sport: Sport) =>
    setConsoles((prev) => {
      const cur = prev[sport] ?? { open: false, lines: [] }
      return { ...prev, [sport]: { ...cur, open: !cur.open } }
    })

  /** Single-engine refresh: open console, POST, then drip the returned log.
   *  Shared verbatim by the per-card buttons and REFRESH ALL. */
  const runOne = useCallback(
    async (sport: Sport, force: boolean) => {
      setConsoles((prev) => {
        const cur = prev[sport] ?? { open: false, lines: [] }
        return { ...prev, [sport]: { ...cur, open: true } }
      })
      push(
        sport,
        force
          ? '>> retrain requested · force_retrain=true · pickle deleted, full rebuild'
          : '>> refresh requested · force_retrain=false',
        'dim',
      )
      push(sport, 'dispatched — engines run serially, this can take tens of seconds…', 'dim')
      const t0 = performance.now()
      const res = await refreshModel(sport, force)
      if (!aliveRef.current) return
      if (!res) {
        push(
          sport,
          `✗ no response after ${((performance.now() - t0) / 1000).toFixed(1)}s — backend down or refresh timed out`,
          'err',
        )
        return
      }
      for (const raw of res.log) {
        if (!aliveRef.current) return
        push(sport, raw, 'text')
        await sleep(DRIP_MS)
      }
      push(
        sport,
        res.ok
          ? `✓ ok · ${res.seconds.toFixed(1)}s · ${sport} engine ${force ? 'retrained' : 'refreshed'}`
          : `✗ failed after ${res.seconds.toFixed(1)}s — see log above`,
        res.ok ? 'ok' : 'err',
      )
      if (res.state) {
        setReport((prev) =>
          prev
            ? { models: prev.models.map((m) => (m.sport === sport ? { ...m, state: res.state ?? m.state } : m)) }
            : prev,
        )
      }
    },
    [push],
  )

  const refetch = useCallback(async () => {
    const r = await fetchModels()
    if (aliveRef.current && r) setReport(r)
  }, [])

  const handleSingle = useCallback(
    async (sport: Sport, force: boolean) => {
      if (flightRef.current || !live) return
      flightRef.current = true
      setFlight({ sport, force })
      try {
        await runOne(sport, force)
      } finally {
        flightRef.current = false
        if (aliveRef.current) setFlight(null)
      }
      await refetch()
    },
    [live, runOne, refetch],
  )

  /** REFRESH ALL — sequential POSTs, one engine at a time, reusing runOne. */
  const handleAll = useCallback(async () => {
    if (flightRef.current || !live) return
    flightRef.current = true
    try {
      for (let i = 0; i < SPORTS.length; i++) {
        if (!aliveRef.current) return
        setAllRun(i)
        setFlight({ sport: SPORTS[i], force: false })
        await runOne(SPORTS[i], false)
      }
    } finally {
      flightRef.current = false
      if (aliveRef.current) {
        setFlight(null)
        setAllRun(null)
      }
    }
    await refetch()
  }, [live, runOne, refetch])

  // Retrain arms on first click, fires on the second; auto-disarms after 4s.
  const onRetrain = (sport: Sport) => {
    if (armed !== sport) {
      setArmed(sport)
      return
    }
    setArmed(null)
    void handleSingle(sport, true)
  }
  useEffect(() => {
    if (!armed) return
    const t = window.setTimeout(() => setArmed(null), 4000)
    return () => clearTimeout(t)
  }, [armed])

  // ---- derived --------------------------------------------------------------
  const anyBusy = flight !== null
  const statuses = useMemo(() => models.map(engineStatus), [models])
  const freshCount = statuses.filter((s) => s === 'fresh').length
  const disabledTitle = !live ? 'Desktop + live backend required' : undefined

  const btnBase =
    'inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[10px] font-bold tracking-widest transition disabled:cursor-not-allowed disabled:opacity-40'

  return (
    <Page
      title="Models"
      subtitle="The engine room — nine prediction engines, artifact state and manual override."
      actions={
        <>
          {demo && <Badge tone="warn">DEMO DATA</Badge>}
          {!demo && apiUp === false && <Badge tone="danger">API DOWN</Badge>}
          {live && <Badge tone="success">LIVE</Badge>}
        </>
      }
    >
      {/* ------------------------------------------------------- top strip */}
      <div className="mb-5 rounded-2xl border border-white/10 bg-krypt-panel/80 px-4 py-3 shadow-krypt-card">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <span className="flex items-center gap-2">
            <span className="inline-block h-2.5 w-0.5 rounded-full bg-scout-red shadow-scout-glow" />
            <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-white/80">
              ENGINE ROOM
            </span>
          </span>
          <span
            className={cls(
              'font-mono text-sm font-bold',
              freshCount === SPORTS.length ? 'text-emerald-300' : 'text-amber-300',
            )}
            title="Engines with state updated within the last 48h"
          >
            {freshCount}/{SPORTS.length} FRESH
          </span>
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={() => void handleAll()}
              disabled={!live || anyBusy}
              title={disabledTitle ?? 'Refresh every engine in sequence (no retrain)'}
              className={cls(
                btnBase,
                'border-scout-red/30 bg-scout-red/[0.06] text-scout-red-bright/90 hover:border-scout-red/50 hover:bg-scout-red/10 hover:text-scout-red-bright',
              )}
            >
              {allRun !== null ? (
                <>
                  <Loader2 size={11} className="animate-spin" />
                  REFRESHING {allRun + 1}/{SPORTS.length} · {SPORT_TAG[SPORTS[allRun]]}
                </>
              ) : (
                <>
                  <RefreshCw size={11} />
                  REFRESH ALL
                </>
              )}
            </button>
          </div>
        </div>
        <p className="mt-1.5 text-[10px] uppercase tracking-wider text-krypt-muted/70">
          Data refreshes automatically every 24h via AUTOMATION — this panel is manual override.
          {!live && (
            <span className="text-amber-300/80"> · Desktop + live backend required — controls disabled.</span>
          )}
        </p>
      </div>

      {/* --------------------------------------------------------- engines */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {models.map((m, i) => {
          const sport = SPORTS[i]
          const st = statuses[i]
          const acc = backtestAcc(m, sport)
          const kind = engineKind(m.model)
          const con = consoles[sport] ?? { open: false, lines: [] }
          const isFlight = flight?.sport === sport
          const armedThis = armed === sport
          const accTone =
            acc >= 55
              ? 'border-emerald-400/30 bg-emerald-500/10 text-emerald-300'
              : acc >= 50
                ? 'border-white/10 bg-white/5 text-white/75'
                : 'border-rose-400/30 bg-rose-500/10 text-rose-300'
          return (
            <div
              key={sport}
              className={cls(
                'rounded-2xl border bg-krypt-panel/80 p-4 shadow-krypt-card backdrop-blur-sm transition',
                isFlight ? 'border-scout-red/40 shadow-scout-glow' : 'border-white/10',
              )}
            >
              {/* header row */}
              <div className="flex items-center gap-2.5">
                <span className="rounded-md border border-scout-red/30 bg-scout-red-dim px-1.5 py-0.5 font-mono text-[10px] font-bold tracking-wider text-scout-red-bright">
                  {SPORT_TAG[sport]}
                </span>
                <span className="truncate font-mono text-sm font-bold text-white">{m.model}</span>
                <span className={cls('h-2 w-2 shrink-0 rounded-full', DOT_CLS[st])} title={DOT_TITLE[st]} />
                {isFlight && (
                  <span className="font-mono text-[9px] uppercase tracking-widest text-scout-red-bright/80">
                    {flight?.force ? 'retraining' : 'refreshing'}
                  </span>
                )}
                <span
                  className={cls(
                    'ml-auto shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[10px] font-bold',
                    accTone,
                  )}
                  title="Backtested moneyline accuracy — historical replay"
                >
                  BT {acc.toFixed(1)}%
                </span>
              </div>

              {/* stat strip */}
              <div className="mt-3 grid grid-cols-2 gap-1.5">
                <StatCell label="Live record" title="Graded live picks — accuracy and log loss">
                  {m.live ? (
                    <>
                      {m.live.n} graded ·{' '}
                      <span className={m.live.accuracy >= 0.5 ? 'text-emerald-300' : 'text-rose-400'}>
                        {(m.live.accuracy * 100).toFixed(1)}%
                      </span>{' '}
                      · LL {m.live.log_loss.toFixed(3)}
                    </>
                  ) : (
                    <span className="text-krypt-muted/60">— no graded picks yet</span>
                  )}
                </StatCell>
                <StatCell label="Backtest" title="Historical replay over past seasons">
                  <span className={acc >= 50 ? 'text-emerald-300' : 'text-rose-400'}>
                    {acc.toFixed(1)}%
                  </span>{' '}
                  · replay ACC
                </StatCell>
                <StatCell label="State" title={m.state?.exists ? m.state.path : 'No ratings state on disk'}>
                  {m.state?.exists ? (
                    <>
                      {sizeLabel(m.state.size_kb)} ·{' '}
                      <span className={st === 'fresh' ? 'text-white/85' : 'text-amber-300'}>
                        updated {ago(m.state.updated_at)}
                      </span>
                    </>
                  ) : (
                    <span className="text-rose-400">missing — run FETCH LATEST</span>
                  )}
                </StatCell>
                <StatCell label="Pickle" title={m.pickle?.exists ? m.pickle.path : 'Rating-based engine — no trained binary'}>
                  {m.pickle?.exists ? (
                    `${kind} PICKLE · ${sizeLabel(m.pickle.size_kb)}`
                  ) : (
                    <span className="text-krypt-muted/70">{kind} — no pickle</span>
                  )}
                </StatCell>
              </div>

              {/* buttons */}
              <div className="mt-3 flex items-center gap-2">
                <button
                  onClick={() => void handleSingle(sport, false)}
                  disabled={!live || anyBusy}
                  title={disabledTitle ?? 'Pull the latest results and update ratings — no retrain'}
                  className={cls(
                    btnBase,
                    'border-white/10 bg-white/[0.03] text-krypt-muted hover:border-white/20 hover:bg-white/5 hover:text-white',
                  )}
                >
                  {isFlight && !flight?.force ? (
                    <Loader2 size={10} className="animate-spin" />
                  ) : (
                    <Download size={10} />
                  )}
                  FETCH LATEST
                </button>
                <button
                  onClick={() => onRetrain(sport)}
                  disabled={!live || anyBusy}
                  title={disabledTitle ?? 'Deletes the model pickle and retrains from scratch'}
                  className={cls(
                    btnBase,
                    armedThis
                      ? 'border-scout-red/70 bg-scout-red/20 text-scout-red-bright shadow-scout-glow'
                      : 'border-scout-red/30 bg-scout-red/[0.06] text-scout-red-bright/80 hover:border-scout-red/50 hover:bg-scout-red/10 hover:text-scout-red-bright',
                  )}
                >
                  {isFlight && flight?.force ? (
                    <Loader2 size={10} className="animate-spin" />
                  ) : (
                    <Flame size={10} />
                  )}
                  {armedThis ? 'CONFIRM RETRAIN?' : 'RETRAIN'}
                </button>
              </div>

              {/* console */}
              <button
                onClick={() => toggleConsole(sport)}
                className="mt-3 flex w-full items-center gap-1.5 text-left text-[9px] font-bold uppercase tracking-[0.18em] text-krypt-muted/70 transition hover:text-white"
              >
                <ChevronRight
                  size={10}
                  className={cls('transition-transform', con.open && 'rotate-90')}
                />
                ENGINE CONSOLE
                <span className="font-mono normal-case tracking-normal text-krypt-muted/50">
                  {con.lines.length > 0 ? `${con.lines.length} lines` : 'idle'}
                </span>
                {isFlight && <Loader2 size={9} className="animate-spin text-scout-red-bright" />}
              </button>
              {con.open && (
                <ConsoleWell
                  sport={sport}
                  lines={con.lines}
                  running={isFlight}
                  cmd={isFlight ? (flight?.force ? 'retrain --force' : 'refresh --latest') : null}
                />
              )}
            </div>
          )
        })}
      </div>
    </Page>
  )
}

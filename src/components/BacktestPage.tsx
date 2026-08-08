import { useEffect, useMemo, useState } from 'react'
import { FlaskConical, Loader2 } from 'lucide-react'
import type {
  AutobetSettings,
  BacktestCalBin,
  BacktestReport,
  RoiBacktestReport,
  RoiSummary,
} from '../api'
import {
  fetchAutobet,
  fetchBacktestAll,
  fetchRoiBacktest,
  fetchRoiOptimize,
  startBackfill,
  type RoiOptimizeReport,
} from '../api'
import { fmtPct } from '../oddsmath'
import { cls } from '../utils/format'
import { Badge, Card, Page, Section, StatTile } from './common'
import { EquityCurve } from './PerfPage'
import { useRoi } from '../share/useRoi'

const num = (v: number | null | undefined, digits: number): string =>
  v != null && Number.isFinite(v) ? v.toFixed(digits) : '—'
const pct = (v: number | null | undefined, digits = 1): string =>
  v != null && Number.isFinite(v) ? `${fmtPct(v, digits)}%` : '—'
const signed = (v: number, digits = 1) => `${v >= 0 ? '+' : ''}${v.toFixed(digits)}`

const TH = 'px-3 py-2 text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70'
const TD = 'px-3 py-1.5 font-mono text-[11px]'

const SPORT_LABEL: Record<string, string> = {
  nba: 'NBA', wnba: 'WNBA', nfl: 'NFL', mlb: 'MLB', nhl: 'NHL',
  ncaaf: 'NCAAF', ncaab: 'NCAAB', soccer: 'SOCCER', tennis: 'TENNIS',
}
const label = (s: string) => SPORT_LABEL[s] ?? s.toUpperCase()

// ---------------------------------------------------------- calibration plot

/** Reliability diagram: one dot per non-empty prob bin at (predicted, actual),
 *  radius ~ sample size, on a 0..1 square with the y=x perfect-calibration
 *  diagonal. Dots hugging the diagonal = the model's stated confidence matches
 *  reality. Emerald within 8pts, amber within 15, rose beyond. */
function Calibration({ bins }: { bins: BacktestCalBin[] }) {
  const pts = bins
    .filter((b) => b.n > 0 && b.predicted != null && b.actual != null)
    .map((b) => ({ lo: b.lo, n: b.n, predicted: b.predicted as number, actual: b.actual as number }))
  const S = 240
  const pad = 26
  const X = (v: number) => pad + v * (S - 2 * pad)
  const Y = (v: number) => S - pad - v * (S - 2 * pad)
  const maxN = Math.max(...pts.map((p) => p.n), 1)
  const ticks = [0, 0.25, 0.5, 0.75, 1]
  const tone = (d: number) =>
    d <= 0.08 ? 'rgb(52,211,153)' : d <= 0.15 ? 'rgb(251,191,36)' : 'rgb(244,63,94)'
  return (
    <div className="mx-auto w-full max-w-[300px]">
      <svg width="100%" viewBox={`0 0 ${S} ${S}`} className="overflow-visible">
        {/* grid */}
        {ticks.map((t) => (
          <g key={t}>
            <line x1={X(t)} y1={Y(0)} x2={X(t)} y2={Y(1)} stroke="rgba(255,255,255,0.06)" strokeWidth={1} />
            <line x1={X(0)} y1={Y(t)} x2={X(1)} y2={Y(t)} stroke="rgba(255,255,255,0.06)" strokeWidth={1} />
            <text x={X(t)} y={S - pad + 12} textAnchor="middle" className="fill-krypt-muted/60 text-[7px]">
              {Math.round(t * 100)}
            </text>
            <text x={pad - 6} y={Y(t) + 3} textAnchor="end" className="fill-krypt-muted/60 text-[7px]">
              {Math.round(t * 100)}
            </text>
          </g>
        ))}
        {/* perfect-calibration diagonal */}
        <line x1={X(0)} y1={Y(0)} x2={X(1)} y2={Y(1)} stroke="rgba(255,255,255,0.25)" strokeWidth={1} strokeDasharray="4 4" />
        {/* bin dots */}
        {pts.map((p) => {
          const d = Math.abs(p.actual - p.predicted)
          return (
            <circle
              key={p.lo}
              cx={X(p.predicted)}
              cy={Y(p.actual)}
              r={3 + 6 * Math.sqrt(p.n / maxN)}
              fill={tone(d)}
              fillOpacity={0.85}
              stroke="rgba(0,0,0,0.35)"
              strokeWidth={0.75}
            >
              <title>{`pred ${pct(p.predicted)} · actual ${pct(p.actual)} · n=${p.n}`}</title>
            </circle>
          )
        })}
        <text x={S / 2} y={S - 2} textAnchor="middle" className="fill-krypt-muted/70 text-[8px] uppercase tracking-widest">
          Predicted %
        </text>
        <text x={9} y={S / 2} textAnchor="middle" transform={`rotate(-90 9 ${S / 2})`} className="fill-krypt-muted/70 text-[8px] uppercase tracking-widest">
          Actual %
        </text>
      </svg>
    </div>
  )
}

// ------------------------------------------------------------ summary metrics

/** Pull {n, accuracy, log_loss} out of a persisted metrics sub-dict, tolerating
 *  the elo_* naming some engines use. */
function subMetrics(v: unknown): { n: number | null; accuracy: number | null; log_loss: number | null } | null {
  if (typeof v !== 'object' || v === null) return null
  const o = v as Record<string, unknown>
  const pick = (...keys: string[]): number | null => {
    for (const k of keys) {
      const x = o[k]
      if (typeof x === 'number' && Number.isFinite(x)) return x
    }
    return null
  }
  const acc = pick('accuracy', 'elo_accuracy')
  const n = pick('n', 'n_games', 'games')
  if (acc == null && n == null) return null
  return { n, accuracy: acc, log_loss: pick('log_loss', 'elo_log_loss') }
}

/** Table of the raw per-year / per-tour breakdown for "summary" sports. */
function SummaryBreakdown({ summary }: { summary: Record<string, unknown> }) {
  const rows = Object.entries(summary)
    .map(([k, v]) => [k, subMetrics(v)] as const)
    .filter(([, m]) => m !== null) as Array<[string, NonNullable<ReturnType<typeof subMetrics>>]>
  if (rows.length === 0) return null
  return (
    <Card padded={false} className="overflow-hidden">
      <table className="w-full border-collapse text-left">
        <thead>
          <tr className="border-b border-white/10 bg-white/[0.02]">
            <th className={TH}>Split</th>
            <th className={cls(TH, 'text-right')}>Games</th>
            <th className={cls(TH, 'text-right')}>Accuracy</th>
            <th className={cls(TH, 'text-right')}>Log loss</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([k, m]) => (
            <tr key={k} className="border-b border-white/5 last:border-0">
              <td className={cls(TD, 'font-sans text-[10px] font-bold uppercase tracking-widest text-white/80')}>
                {k === 'overall' ? 'Overall' : k}
              </td>
              <td className={cls(TD, 'text-right text-krypt-muted')}>{m.n?.toLocaleString() ?? '—'}</td>
              <td className={cls(TD, 'text-right text-white')}>{pct(m.accuracy)}</td>
              <td className={cls(TD, 'text-right text-krypt-muted')}>{num(m.log_loss, 3)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}

// ---------------------------------------------------------------- sweep table

function Sweep({
  rows,
  floorProb,
}: {
  rows: NonNullable<BacktestReport['sweep']>
  /** Active auto-bet min-win-prob floor; nearest band is marked "your floor". */
  floorProb?: number | null
}) {
  const peak = rows.reduce(
    (best, r) => (r.accuracy != null && (best == null || r.accuracy > best.accuracy) ? { threshold: r.threshold, accuracy: r.accuracy } : best),
    null as { threshold: number; accuracy: number } | null,
  )
  // The sweep band closest to the strategy's win-prob floor.
  const floorRow =
    floorProb != null && floorProb > 0
      ? rows.reduce(
          (best, r) =>
            best == null || Math.abs(r.threshold - floorProb) < Math.abs(best - floorProb)
              ? r.threshold
              : best,
          null as number | null,
        )
      : null
  return (
    <Card padded={false} className="overflow-hidden">
      <table className="w-full border-collapse text-left">
        <thead>
          <tr className="border-b border-white/10 bg-white/[0.02]">
            <th className={TH}>Confidence ≥</th>
            <th className={cls(TH, 'text-right')}>Games</th>
            <th className={cls(TH, 'text-right')}>Share</th>
            <th className={cls(TH, 'text-right')}>Accuracy</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const isPeak = peak != null && r.threshold === peak.threshold
            const isFloor = floorRow != null && r.threshold === floorRow
            return (
              <tr
                key={r.threshold}
                className={cls(
                  'border-b border-white/5 last:border-0',
                  isFloor ? 'bg-sky-500/10' : isPeak && 'bg-scout-red/10',
                )}
              >
                <td className={cls(TD, 'text-white')}>{pct(r.threshold, 0)}</td>
                <td className={cls(TD, 'text-right text-krypt-muted')}>{r.n.toLocaleString()}</td>
                <td className={cls(TD, 'text-right text-krypt-muted')}>{pct(r.share, 0)}</td>
                <td className={cls(TD, 'text-right font-semibold', (r.accuracy ?? 0) >= 0.5 ? 'text-emerald-300' : 'text-rose-400')}>
                  {pct(r.accuracy)}
                  {isFloor && <span className="ml-1.5 text-[8px] font-bold uppercase tracking-widest text-sky-300">your floor</span>}
                  {!isFloor && isPeak && <span className="ml-1.5 text-[8px] font-bold uppercase tracking-widest text-scout-red-bright">peak</span>}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </Card>
  )
}

// ------------------------------------------------------------ $-ROI backtest

const ROI_OFFSETS = [
  { minutes: 15, label: 'T-15m' },
  { minutes: 60, label: 'T-1h' },
  { minutes: 360, label: 'T-6h' },
  { minutes: 1440, label: 'T-24h' },
] as const
const ROI_VOLUMES = [1000, 5000, 20000] as const

const FILL_TONE: Record<string, string> = {
  book: 'text-sky-300',
  pessimistic: 'text-rose-300',
  realistic: 'text-white',
  optimistic: 'text-emerald-300',
}

/** Replay the ACTIVE auto-bet strategy against backfilled Polymarket prices.
 *  Three fill assumptions per run — trust the pessimistic row. */
function RoiBacktestSection({
  sport,
  autobet,
  onSettings,
}: {
  sport: string
  autobet: AutobetSettings | null
  /** Push freshly fetched settings back up so the sweep overlay stays live. */
  onSettings: (s: AutobetSettings | null) => void
}) {
  const [res, setRes] = useState<RoiBacktestReport | null>(null)
  const [running, setRunning] = useState(false)
  const [offset, setOffset] = useState<number>(60)
  const [minVolume, setMinVolume] = useState<number>(5000)
  const [harvestMsg, setHarvestMsg] = useState<string | null>(null)
  const [opt, setOpt] = useState<RoiOptimizeReport | null>(null)
  const [optRunning, setOptRunning] = useState(false)

  // New sport selected → stale results don't linger.
  useEffect(() => {
    setRes(null)
    setOpt(null)
  }, [sport])

  const runOptimize = async () => {
    if (optRunning) return
    setOptRunning(true)
    setOpt(await fetchRoiOptimize(sport, { offset_minutes: offset, min_volume: minVolume }))
    setOptRunning(false)
  }

  const run = async () => {
    if (running) return
    setRunning(true)
    // Re-fetch at click time so a just-saved AUTO-BET change is what replays —
    // not the settings from whenever this page mounted.
    const s = (await fetchAutobet())?.settings ?? autobet
    onSettings(s)
    const r = await fetchRoiBacktest(sport, {
      offset_minutes: offset,
      min_volume: minVolume,
      ...(s
        ? {
            min_ev: s.min_ev,
            min_win_prob: s.min_win_prob,
            min_edge: s.min_edge,
            min_prob: s.min_prob ?? 0,
            max_prob: s.max_prob,
            stake_mode: s.stake_mode,
            stake_usd: s.stake_usd,
            stake_pct: s.stake_pct,
            stake_pct_min: s.stake_pct_min,
            stake_pct_max: s.stake_pct_max,
            daily_stop_loss_usd: s.daily_stop_loss_usd,
            daily_stop_profit_usd: s.daily_stop_profit_usd,
            stop_loss_usd: s.stop_loss_usd,
            stop_profit_usd: s.stop_profit_usd,
          }
        : {}),
    })
    setRes(r)
    setRunning(false)
  }

  const strategyLine = autobet
    ? `min EV ${fmtPct(autobet.min_ev, 0)}% · min win ${fmtPct(autobet.min_win_prob, 0)}% · min edge ${fmtPct(autobet.min_edge, 0)}pts · ${
        autobet.stake_mode === 'fixed'
          ? `$${autobet.stake_usd} flat`
          : autobet.stake_mode === 'percent'
            ? `${autobet.stake_pct}% of bank`
            : `${autobet.stake_pct_min}–${autobet.stake_pct_max}% by confidence`
      }`
    : 'default strategy (auto-bet settings unavailable)'
  const realistic = res?.sims.realistic

  return (
    <Section
      title="Dollar-ROI backtest · backfilled Polymarket history"
      description="Replays your ACTIVE auto-bet strategy against real recorded pre-kickoff prices, with era-correct taker fees. Three fill assumptions — trust the pessimistic column."
    >
      <Card>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
          <span className="flex items-center gap-1.5" role="group" aria-label="Price snapshot offset">
            {ROI_OFFSETS.map((o) => (
              <button
                key={o.minutes}
                onClick={() => setOffset(o.minutes)}
                aria-pressed={offset === o.minutes}
                title="How long before kickoff the entry price is taken"
                className={cls(
                  'rounded-lg border px-2 py-1 text-[10px] font-bold tracking-widest transition',
                  offset === o.minutes
                    ? 'border-scout-red/40 bg-scout-red/10 text-scout-red-bright'
                    : 'border-white/10 bg-white/[0.03] text-krypt-muted hover:border-white/20',
                )}
              >
                {o.label}
              </button>
            ))}
          </span>
          <span className="flex items-center gap-1.5" role="group" aria-label="Minimum market volume">
            {ROI_VOLUMES.map((v) => (
              <button
                key={v}
                onClick={() => setMinVolume(v)}
                aria-pressed={minVolume === v}
                title="Drop markets thinner than this — thin last-trade prices fake edge"
                className={cls(
                  'rounded-lg border px-2 py-1 text-[10px] font-bold tracking-widest transition',
                  minVolume === v
                    ? 'border-scout-red/40 bg-scout-red/10 text-scout-red-bright'
                    : 'border-white/10 bg-white/[0.03] text-krypt-muted hover:border-white/20',
                )}
              >
                VOL≥{v >= 1000 ? `${v / 1000}K` : v}
              </button>
            ))}
          </span>
          <button
            onClick={() => void run()}
            disabled={running}
            className="inline-flex items-center gap-1.5 rounded-lg bg-krypt-gradient px-3 py-1.5 text-[11px] font-bold uppercase tracking-widest text-white shadow-krypt-glow transition hover:brightness-110 disabled:opacity-50"
          >
            {running ? <Loader2 size={12} className="animate-spin" /> : <FlaskConical size={12} />}
            Run backtest
          </button>
          <button
            onClick={() => void runOptimize()}
            disabled={optRunning}
            title="Grid-search the gate thresholds on the first 70% of history, then show each top combo against the untouched last 30% — trust the validation column"
            className="inline-flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-3 py-1.5 text-[11px] font-bold uppercase tracking-widest text-white/90 transition hover:border-white/20 hover:bg-white/10 disabled:opacity-50"
          >
            {optRunning ? <Loader2 size={12} className="animate-spin" /> : null}
            Optimize
          </button>
          <span className="text-[10px] text-krypt-muted/70">strategy: {strategyLine}</span>
        </div>

        {opt && (
          <div className="mt-4">
            <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
              Threshold optimizer · tuned before {opt.split_date ?? '—'} · validated after
            </div>
            {opt.results.length === 0 ? (
              <p className="text-xs text-krypt-muted">{opt.note}</p>
            ) : (
              <Card padded={false} className="overflow-hidden">
                <table className="w-full border-collapse text-left">
                  <thead>
                    <tr className="border-b border-white/10 bg-white/[0.02]">
                      <th className={TH}>Gates</th>
                      <th className={cls(TH, 'text-right')}>Tune ROI</th>
                      <th className={cls(TH, 'text-right')}>Validation ROI</th>
                      <th className={cls(TH, 'text-right')}>Verdict</th>
                    </tr>
                  </thead>
                  <tbody>
                    {opt.results.map((r, i) => {
                      const t = r.tune.roi_pct ?? 0
                      const v = r.validation.roi_pct
                      const holds = v != null && v > 0
                      return (
                        <tr key={i} className="border-b border-white/5 last:border-0">
                          <td className={cls(TD, 'text-white')}>
                            ev≥{fmtPct(r.min_ev, 0)}% · win≥{fmtPct(r.min_win_prob, 0)}% · edge≥{fmtPct(r.min_edge, 0)}pts
                          </td>
                          <td className={cls(TD, 'text-right text-krypt-muted')}>
                            {t >= 0 ? '+' : ''}{t}% ({r.tune.n_bets})
                          </td>
                          <td className={cls(TD, 'text-right font-semibold', holds ? 'text-emerald-300' : 'text-rose-400')}>
                            {v != null ? `${v >= 0 ? '+' : ''}${v}%` : '—'} ({r.validation.n_bets})
                          </td>
                          <td className={cls(TD, 'text-right text-[9px] font-bold uppercase tracking-widest', holds ? 'text-emerald-300' : 'text-rose-400/80')}>
                            {holds ? 'holds' : 'overfit'}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </Card>
            )}
            <p className="mt-1.5 text-[10px] text-krypt-muted/60">
              Combos ranked on the tune window only — a combo that flips negative in validation was
              curve-fit. Only trust gates whose validation column stays green.
            </p>
          </div>
        )}

        {res && res.join.joined === 0 && (
          <div className="mt-4 rounded-xl border border-dashed border-white/10 bg-black/20 px-4 py-6 text-center text-xs text-krypt-muted">
            No joinable history for {label(sport)} yet —{' '}
            {res.join.markets === 0
              ? 'no Polymarket history harvested on this machine.'
              : "this model doesn't persist a team-named walk-forward log yet (MLB, NBA and WNBA do)."}
            <span className="mt-1 block font-mono text-[10px] text-krypt-muted/60">
              log rows {res.join.log_rows} · priced markets {res.join.markets}
            </span>
            {res.join.markets === 0 && (
              <button
                onClick={() => {
                  void startBackfill()
                  setHarvestMsg('Harvest started — ~36k polite requests, an hour or two. Re-run later.')
                }}
                className="mx-auto mt-3 block rounded-lg border border-scout-red/40 bg-scout-red/10 px-3 py-1.5 text-[10px] font-bold uppercase tracking-widest text-scout-red-bright transition hover:bg-scout-red/20"
              >
                Start history harvest
              </button>
            )}
            {harvestMsg && <span className="mt-2 block text-[10px] text-emerald-300">{harvestMsg}</span>}
          </div>
        )}

        {res && res.join.joined > 0 && (
          <>
            <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
              <StatTile label="Games joined" value={res.join.joined.toLocaleString()} sub={`of ${res.join.markets.toLocaleString()} priced markets`} />
              <StatTile
                label="P&L (realistic)"
                value={`${(realistic?.pnl_usd ?? 0) >= 0 ? '+' : ''}$${(realistic?.pnl_usd ?? 0).toFixed(0)}`}
                tone={(realistic?.pnl_usd ?? 0) >= 0 ? 'success' : 'danger'}
                sub={`${realistic?.n_bets ?? 0} bets`}
              />
              <StatTile
                label="ROI (pessimistic)"
                value={res.sims.pessimistic.roi_pct != null ? `${res.sims.pessimistic.roi_pct >= 0 ? '+' : ''}${res.sims.pessimistic.roi_pct}%` : '—'}
                tone={(res.sims.pessimistic.roi_pct ?? 0) >= 0 ? 'success' : 'danger'}
                sub="the number to trust"
              />
              <StatTile label="Max drawdown" value={`-$${(realistic?.max_drawdown_usd ?? 0).toFixed(0)}`} sub="realistic fill" />
            </div>

            <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
              <Card padded={false} className="overflow-hidden">
                <table className="w-full border-collapse text-left">
                  <thead>
                    <tr className="border-b border-white/10 bg-white/[0.02]">
                      <th className={TH}>Fill</th>
                      <th className={cls(TH, 'text-right')}>Bets</th>
                      <th className={cls(TH, 'text-right')}>Win</th>
                      <th className={cls(TH, 'text-right')}>P&amp;L</th>
                      <th className={cls(TH, 'text-right')}>ROI</th>
                    </tr>
                  </thead>
                  <tbody>
                    {/* BOOK (real archived ask, no guessed padding) leads when
                        the harvest covers part of the sample. */}
                    {(res.sims.book
                      ? (['book', 'pessimistic', 'realistic', 'optimistic'] as const)
                      : (['pessimistic', 'realistic', 'optimistic'] as const)
                    ).map((f) => {
                      const s = res.sims[f]
                      if (!s) return null
                      return (
                        <tr key={f} className="border-b border-white/5 last:border-0">
                          <td
                            className={cls(TD, 'font-sans text-[10px] font-bold uppercase tracking-widest', FILL_TONE[f])}
                            title={f === 'book' ? `Real archived best ask — ${res.join.with_book ?? 0} of ${res.join.joined} games have harvested books` : undefined}
                          >
                            {f}
                          </td>
                          <td className={cls(TD, 'text-right text-krypt-muted')}>{s.n_bets}</td>
                          <td className={cls(TD, 'text-right text-krypt-muted')}>{pct(s.win_rate)}</td>
                          <td className={cls(TD, 'text-right font-semibold', s.pnl_usd >= 0 ? 'text-emerald-300' : 'text-rose-400')}>
                            {s.pnl_usd >= 0 ? '+' : ''}${s.pnl_usd.toFixed(0)}
                          </td>
                          <td className={cls(TD, 'text-right', (s.roi_pct ?? 0) >= 0 ? 'text-emerald-300' : 'text-rose-400')}>
                            {s.roi_pct != null ? `${s.roi_pct >= 0 ? '+' : ''}${s.roi_pct}%` : '—'}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </Card>
              <Card>
                <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
                  Cumulative P&amp;L · realistic fill ($)
                </div>
                {realistic && realistic.equity.length > 0 ? (
                  <EquityCurve daily={realistic.equity.map((e) => ({ date: e.date, units: e.pnl, cum_units: e.cum }))} />
                ) : (
                  <div className="py-6 text-center text-[10px] uppercase tracking-wider text-krypt-muted/60">No equity series</div>
                )}
              </Card>
            </div>
            <p className="mt-3 text-[10px] text-krypt-muted/70">
              {res.note} Join drops: {res.join.thin} thin · {res.join.stale} stale ·{' '}
              {res.join.outcome_mismatch} resolution mismatches.
            </p>
          </>
        )}
      </Card>
    </Section>
  )
}

// --------------------------------------------------------------- pick record

/** The live betting-side counterpart: what the picks actually returned in real
 *  dollars (flat 1u), so model skill sits next to realized ROI. */
function PickRecordStrip({ roi }: { roi: RoiSummary | null }) {
  const graded = roi && roi.overall.n_graded > 0 ? roi : null
  return (
    <Section
      title="Pick record · live $ROI (flat 1u)"
      description="What the VALUE picks actually returned once games settled — the money-side counterpart to the model skill above. Grows as capture + settle run."
    >
      {!graded ? (
        <Card>
          <div className="py-6 text-center">
            <div className="text-sm font-semibold text-white">No graded value picks yet</div>
            <p className="mx-auto mt-1.5 max-w-lg text-xs text-krypt-muted">
              Picks grade automatically once games settle (needs live odds). The full breakdown lives on the Performance page.
            </p>
          </div>
        </Card>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
          <div className="grid grid-cols-3 gap-3">
            <StatTile label="Units" value={`${signed(graded.overall.units)}u`} tone={graded.overall.units >= 0 ? 'success' : 'danger'} sub="net · flat 1u" />
            <StatTile label="ROI" value={`${signed(graded.overall.roi_pct)}%`} tone={graded.overall.roi_pct >= 0 ? 'success' : 'danger'} sub="units / staked" />
            <StatTile label="Graded" value={graded.overall.n_graded} sub={`of ${graded.overall.n_picks} picks`} />
          </div>
          <Card>
            <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
              Cumulative units · 30 days
            </div>
            {graded.daily.length === 0 ? (
              <div className="py-6 text-center text-[10px] uppercase tracking-wider text-krypt-muted/60">No daily series yet</div>
            ) : (
              <EquityCurve daily={graded.daily} />
            )}
          </Card>
        </div>
      )}
    </Section>
  )
}

// ----------------------------------------------------------------- demo data

const DEMO_BACKTEST: BacktestReport[] = [
  {
    sport: 'nba',
    model: 'nba-elo-v1',
    kind: 'log',
    n: 2631,
    accuracy: 0.6606,
    log_loss: 0.6251,
    brier: 0.2166,
    start: '2024-10-22',
    end: '2026-06-20',
    calibration: [
      { lo: 0.0, hi: 0.1, n: 41, predicted: 0.07, actual: 0.05 },
      { lo: 0.1, hi: 0.2, n: 118, predicted: 0.15, actual: 0.17 },
      { lo: 0.2, hi: 0.3, n: 214, predicted: 0.25, actual: 0.24 },
      { lo: 0.3, hi: 0.4, n: 288, predicted: 0.35, actual: 0.37 },
      { lo: 0.4, hi: 0.5, n: 331, predicted: 0.45, actual: 0.46 },
      { lo: 0.5, hi: 0.6, n: 340, predicted: 0.55, actual: 0.54 },
      { lo: 0.6, hi: 0.7, n: 356, predicted: 0.65, actual: 0.67 },
      { lo: 0.7, hi: 0.8, n: 314, predicted: 0.75, actual: 0.76 },
      { lo: 0.8, hi: 0.9, n: 266, predicted: 0.85, actual: 0.83 },
      { lo: 0.9, hi: 1.0, n: 163, predicted: 0.94, actual: 0.96 },
    ],
    sweep: [
      { threshold: 0.5, n: 2631, accuracy: 0.661, share: 1.0 },
      { threshold: 0.55, n: 2300, accuracy: 0.68, share: 0.874 },
      { threshold: 0.6, n: 1965, accuracy: 0.695, share: 0.747 },
      { threshold: 0.65, n: 1649, accuracy: 0.715, share: 0.627 },
      { threshold: 0.7, n: 1309, accuracy: 0.741, share: 0.498 },
      { threshold: 0.75, n: 988, accuracy: 0.782, share: 0.376 },
      { threshold: 0.8, n: 695, accuracy: 0.807, share: 0.264 },
      { threshold: 0.85, n: 429, accuracy: 0.834, share: 0.163 },
    ],
    summary: null,
    note: null,
  },
  {
    sport: 'tennis',
    model: 'tennis-elo-v1',
    kind: 'summary',
    n: 7905,
    accuracy: 0.6377,
    log_loss: 0.6251,
    brier: null,
    start: null,
    end: null,
    calibration: null,
    sweep: null,
    summary: {
      ATP: { n: 4049, accuracy: 0.6337, log_loss: 0.6274 },
      WTA: { n: 3856, accuracy: 0.6419, log_loss: 0.6226 },
    },
    note: 'Aggregate walk-forward metrics only — no per-game log persisted for a calibration curve.',
  },
]

// ----------------------------------------------------------------------- page

interface Props {
  demo: boolean
  apiUp: boolean | null
}

const KIND_RANK: Record<BacktestReport['kind'], number> = { log: 0, summary: 1, none: 2 }

/** BACKTEST — model-skill report off each engine's walk-forward log:
 *  calibration curve, Brier, and a confidence-threshold sweep, with the live
 *  pick-record ROI surfaced alongside. */
export default function BacktestPage({ demo, apiUp }: Props) {
  const [reports, setReports] = useState<BacktestReport[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [sel, setSel] = useState<string | null>(null)
  const [autobet, setAutobet] = useState<AutobetSettings | null>(null)
  const roi = useRoi(demo)

  useEffect(() => {
    if (demo) {
      setReports(DEMO_BACKTEST)
      // Demo: show the strategy overlay with a representative floor.
      setAutobet({ min_win_prob: 0.6, stake_mode: 'confidence', stake_pct_min: 2, stake_pct_max: 8 } as AutobetSettings)
      setLoading(false)
      return
    }
    let alive = true
    setLoading(true)
    setReports(null)
    void fetchBacktestAll().then((r) => {
      if (!alive) return
      setReports(r)
      setLoading(false)
    })
    // The active auto-bet strategy floor, overlaid on the sweep below.
    void fetchAutobet().then((st) => alive && setAutobet(st?.settings ?? null))
    return () => {
      alive = false
    }
  }, [demo])

  // Strategy win-prob floor to highlight on the sweep (null when off/unset).
  const floorProb = autobet && autobet.min_win_prob > 0 ? autobet.min_win_prob : null

  // Order sports by richness (log → summary → none) so the deepest lands first.
  const ordered = useMemo(
    () => (reports ? [...reports].sort((a, b) => KIND_RANK[a.kind] - KIND_RANK[b.kind] || (b.n ?? 0) - (a.n ?? 0)) : []),
    [reports],
  )
  const active = ordered.find((r) => r.sport === sel) ?? ordered[0] ?? null

  // Land on the richest sport once reports arrive.
  useEffect(() => {
    if (ordered.length && (sel == null || !ordered.some((r) => r.sport === sel))) {
      setSel(ordered[0].sport)
    }
  }, [ordered, sel])

  return (
    <Page
      title="Backtest"
      subtitle="How well each model's stated confidence has matched reality across its walk-forward history."
      actions={
        <>
          {demo && <Badge tone="gradient">DEMO</Badge>}
          {apiUp === false && !demo && <Badge tone="danger">API DOWN</Badge>}
        </>
      }
    >
      {loading ? (
        <div className="flex items-center gap-2 py-16 text-xs uppercase tracking-wider text-krypt-muted">
          <Loader2 size={14} className="animate-spin text-scout-red-bright" />
          Loading backtests…
        </div>
      ) : !active ? (
        <div className="flex flex-col items-center rounded-2xl border border-dashed border-white/10 bg-black/20 px-6 py-14 text-center">
          <FlaskConical size={22} className="mb-3 text-scout-red-bright" />
          <div className="text-base font-semibold text-white">No backtests available</div>
          <p className="mt-2 max-w-lg text-sm text-krypt-muted">
            Each engine persists its walk-forward backtest when it builds. Run a model refresh, then check back.
          </p>
        </div>
      ) : (
        <>
          {/* sport selector */}
          <div className="mb-6 flex flex-wrap gap-2">
            {ordered.map((r) => {
              const on = r.sport === active.sport
              return (
                <button
                  key={r.sport}
                  onClick={() => setSel(r.sport)}
                  className={cls(
                    'flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-[11px] font-semibold tracking-wider transition',
                    on
                      ? 'border-scout-red/50 bg-scout-red/10 text-scout-red-bright'
                      : 'border-white/10 bg-white/[0.03] text-krypt-muted hover:border-white/20 hover:text-white',
                  )}
                >
                  <span
                    className={cls(
                      'h-1.5 w-1.5 rounded-full',
                      r.kind === 'log' ? 'bg-emerald-400' : r.kind === 'summary' ? 'bg-amber-400' : 'bg-white/20',
                    )}
                    title={r.kind === 'log' ? 'per-game log' : r.kind === 'summary' ? 'aggregate only' : 'no backtest'}
                  />
                  {label(r.sport)}
                </button>
              )
            })}
          </div>

          <Section
            title={`${label(active.sport)} · ${active.model ?? 'model'}`}
            description={
              active.kind === 'log' && active.start
                ? `Walk-forward per-game log · ${active.start} → ${active.end}`
                : active.kind === 'summary'
                  ? 'Aggregate walk-forward metrics'
                  : undefined
            }
          >
            {active.kind === 'none' ? (
              <Card>
                <div className="py-8 text-center">
                  <div className="text-sm font-semibold text-white">No backtest persisted for {label(active.sport)}</div>
                  <p className="mx-auto mt-1.5 max-w-lg text-xs text-krypt-muted">{active.note}</p>
                </div>
              </Card>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                  <StatTile label="Games" value={active.n?.toLocaleString() ?? '—'} sub="in the backtest" />
                  <StatTile
                    label="Accuracy"
                    value={pct(active.accuracy)}
                    tone={(active.accuracy ?? 0) >= 0.5 ? 'success' : 'neutral'}
                    sub="straight-up winner"
                  />
                  <StatTile label="Log loss" value={num(active.log_loss, 3)} sub="lower is better" help="Penalizes confident wrong calls — lower means better-calibrated probabilities" />
                  <StatTile
                    label="Brier"
                    value={active.brier != null ? num(active.brier, 3) : '—'}
                    sub={active.brier != null ? 'lower is better' : 'per-game log only'}
                    help="Mean squared error of the probabilities (0 = perfect, 0.25 = a coin flip)"
                  />
                </div>

                {active.kind === 'log' && active.calibration && active.sweep ? (
                  <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
                    <Card>
                      <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
                        Calibration · predicted vs actual
                      </div>
                      <p className="mb-2 text-[10px] text-krypt-muted/70">
                        Dots on the dashed line = confidence matches reality. Size ∝ sample.
                      </p>
                      <Calibration bins={active.calibration} />
                    </Card>
                    <div>
                      <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
                        Confidence-threshold sweep
                      </div>
                      <p className="mb-2 text-[10px] text-krypt-muted/70">
                        Accuracy when you only bet games the model is at least this sure of.
                        {floorProb != null && (
                          <>
                            {' '}
                            <span className="text-sky-300">
                              Auto-bet floor: min win {pct(floorProb, 0)} — nearest band marked below.
                            </span>
                          </>
                        )}
                      </p>
                      <Sweep rows={active.sweep} floorProb={floorProb} />
                    </div>
                  </div>
                ) : active.kind === 'summary' && active.summary ? (
                  <div className="mt-4">
                    {active.note && <p className="mb-3 text-xs text-krypt-muted">{active.note}</p>}
                    <SummaryBreakdown summary={active.summary} />
                  </div>
                ) : null}
              </>
            )}
          </Section>

          {!demo && <RoiBacktestSection sport={active.sport} autobet={autobet} onSettings={setAutobet} />}

          <PickRecordStrip roi={roi} />
        </>
      )}
    </Page>
  )
}

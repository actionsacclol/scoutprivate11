import { useEffect, useState } from 'react'
import { CalendarClock, Loader2 } from 'lucide-react'
import type { ClvReport, PerfDaily, PerformanceReport } from '../types'
import type { RoiDaily, RoiSummary } from '../api'
import { fetchClv, fetchPerformance } from '../api'
import { DEMO_CLV, DEMO_PERFORMANCE } from '../demoIntel'
import { fmtPct } from '../oddsmath'
import { Badge, Card, Kbd, Page, Section, StatTile } from './common'
import { cls } from '../utils/format'
import { useRoi } from '../share/useRoi'

const num = (v: number | null | undefined, digits: number): string =>
  v != null && Number.isFinite(v) ? v.toFixed(digits) : '—'

const TH = 'px-3 py-2 text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70'
const TD = 'px-3 py-1.5 font-mono text-[11px]'

// ------------------------------------------------------------- daily chart

const STRIP_DAYS = 30

/** 30-day daily accuracy chart: slim bars, 50% reference line,
 *  emerald ≥50% / scout-red <50%. */
function DailyChart({ daily }: { daily: PerfDaily[] }) {
  const byDate = new Map(daily.map((d) => [d.date, d]))
  const barW = 18
  const gap = 4
  const h = 96
  const pad = 4
  const w = STRIP_DAYS * (barW + gap) - gap
  const days: Array<{ date: string; row: PerfDaily | undefined }> = []
  for (let i = STRIP_DAYS - 1; i >= 0; i--) {
    const date = new Date(Date.now() - i * 86_400_000).toISOString().slice(0, 10)
    days.push({ date, row: byDate.get(date) })
  }
  const mid = pad + (h - 2 * pad) / 2
  return (
    <div className="overflow-x-auto">
      <svg
        width="100%"
        height={h}
        viewBox={`0 0 ${w} ${h}`}
        preserveAspectRatio="none"
        className="min-w-[560px]"
      >
        <line
          x1={0}
          y1={mid}
          x2={w}
          y2={mid}
          stroke="rgba(255,255,255,0.18)"
          strokeWidth="1"
          strokeDasharray="3 3"
        />
        {days.map(({ date, row }, i) => {
          const x = i * (barW + gap)
          if (!row || !Number.isFinite(row.accuracy)) {
            return (
              <rect
                key={date}
                x={x}
                y={h - pad - 2}
                width={barW}
                height={2}
                rx={1}
                fill="rgba(255,255,255,0.06)"
              />
            )
          }
          const bh = Math.max(2, row.accuracy * (h - 2 * pad))
          const ok = row.accuracy >= 0.5
          return (
            <rect
              key={date}
              x={x}
              y={h - pad - bh}
              width={barW}
              height={bh}
              rx={2}
              fill={ok ? 'rgba(52,211,153,0.75)' : 'rgba(225,29,72,0.75)'}
            >
              <title>{`${date} · n=${row.n} · ACC ${fmtPct(row.accuracy)}% · logloss ${num(row.log_loss, 3)}`}</title>
            </rect>
          )
        })}
      </svg>
    </div>
  )
}

// ---------------------------------------------------------- pick record (ROI)

/** Cumulative-units equity curve: area + line split at zero — emerald above,
 *  rose below — in the same slim SVG style as the daily accuracy chart.
 *  Exported so the Backtest page can reuse it for its pick-record strip. */
export function EquityCurve({ daily }: { daily: RoiDaily[] }) {
  const h = 120
  const pad = 8
  const stepW = 20
  const w = Math.max(daily.length * stepW, 320)
  const vals = daily.map((d) => d.cum_units)
  const hi = Math.max(...vals, 0.5)
  const lo = Math.min(...vals, -0.5)
  const Y = (v: number) => pad + ((hi - v) / (hi - lo)) * (h - 2 * pad)
  const X = (i: number) => (i / Math.max(daily.length - 1, 1)) * w
  const zeroY = Y(0)
  const line = daily.map((d, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(d.cum_units).toFixed(1)}`).join(' ')
  const area = `${line} L${w},${zeroY.toFixed(1)} L0,${zeroY.toFixed(1)} Z`
  const last = daily[daily.length - 1]
  return (
    <div className="overflow-x-auto">
      <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="min-w-[560px]">
        <defs>
          <clipPath id="eq-above">
            <rect x={0} y={0} width={w} height={zeroY} />
          </clipPath>
          <clipPath id="eq-below">
            <rect x={0} y={zeroY} width={w} height={h - zeroY} />
          </clipPath>
        </defs>
        {/* zero baseline */}
        <line x1={0} y1={zeroY} x2={w} y2={zeroY} stroke="rgba(255,255,255,0.18)" strokeWidth="1" strokeDasharray="3 3" />
        {/* area + line, emerald above 0 / rose below */}
        <path d={area} fill="rgba(52,211,153,0.22)" clipPath="url(#eq-above)" />
        <path d={area} fill="rgba(225,29,72,0.22)" clipPath="url(#eq-below)" />
        <path d={line} fill="none" stroke="rgba(52,211,153,0.9)" strokeWidth="1.8" clipPath="url(#eq-above)" />
        <path d={line} fill="none" stroke="rgba(244,63,94,0.9)" strokeWidth="1.8" clipPath="url(#eq-below)" />
        {last && (
          <circle
            cx={X(daily.length - 1)}
            cy={Y(last.cum_units)}
            r={3}
            fill={last.cum_units >= 0 ? 'rgb(52,211,153)' : 'rgb(244,63,94)'}
          />
        )}
        {/* invisible per-day hover targets with native tooltips */}
        {daily.map((d, i) => (
          <rect key={d.date} x={X(i) - stepW / 2} y={0} width={stepW} height={h} fill="transparent">
            <title>{`${d.date} · ${d.units >= 0 ? '+' : ''}${d.units.toFixed(1)}u · cum ${d.cum_units >= 0 ? '+' : ''}${d.cum_units.toFixed(1)}u`}</title>
          </rect>
        ))}
      </svg>
    </div>
  )
}

const ROI_TIERS = ['STRONG', 'LEAN', 'MODEL'] as const

const TIER_TONE: Record<(typeof ROI_TIERS)[number], string> = {
  STRONG: 'text-emerald-300',
  LEAN: 'text-emerald-300/70',
  MODEL: 'text-krypt-muted',
}

const signed = (v: number, digits = 1) => `${v >= 0 ? '+' : ''}${v.toFixed(digits)}`

/** PICK RECORD — the betting-side report card (flat 1u stakes), distinct from
 *  the prediction-calibration MODEL METRICS below it. */
function PickRecord({ roi }: { roi: RoiSummary | null }) {
  const graded = roi && roi.overall.n_graded > 0 ? roi : null
  return (
    <Section
      title="Pick record · flat 1u stakes"
      description="Every VALUE pick graded at a flat 1 unit once games settle — units won, ROI and win rate. Separate from the model metrics below."
    >
      {!graded ? (
        <div className="rounded-2xl border border-dashed border-white/10 bg-black/20 px-6 py-8 text-center">
          <div className="text-sm font-semibold text-white">No graded value picks yet</div>
          <p className="mx-auto mt-1.5 max-w-lg text-xs text-krypt-muted">
            Picks grade automatically once games settle (needs live odds for value picks).
          </p>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatTile
              label="Value picks"
              value={graded.overall.n_graded}
              sub={`graded · ${graded.overall.n_picks} picks made`}
            />
            <StatTile
              label="Units"
              value={`${signed(graded.overall.units)}u`}
              tone={graded.overall.units >= 0 ? 'success' : 'danger'}
              sub="net · flat 1u per pick"
            />
            <StatTile
              label="ROI"
              value={`${signed(graded.overall.roi_pct)}%`}
              tone={graded.overall.roi_pct >= 0 ? 'success' : 'danger'}
              sub="units / picks staked"
              help="Return on investment: net units won divided by units staked (1u per pick)"
            />
            <StatTile
              label="Strong win %"
              value={
                graded.overall.by_tier.STRONG
                  ? `${fmtPct(graded.overall.by_tier.STRONG.win_pct)}%`
                  : '—'
              }
              tone={
                graded.overall.by_tier.STRONG && graded.overall.by_tier.STRONG.win_pct >= 0.5
                  ? 'success'
                  : 'neutral'
              }
              sub={
                graded.overall.by_tier.STRONG
                  ? `${graded.overall.by_tier.STRONG.n} strong picks`
                  : 'no strong picks yet'
              }
            />
          </div>

          <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            <Card>
              <div className="mb-2 flex items-baseline justify-between">
                <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
                  Cumulative units · 30 days
                </span>
                {graded.daily.length > 0 && (
                  <span
                    className={cls(
                      'font-mono text-xs font-bold',
                      graded.daily[graded.daily.length - 1].cum_units >= 0
                        ? 'text-emerald-300'
                        : 'text-rose-400',
                    )}
                  >
                    {signed(graded.daily[graded.daily.length - 1].cum_units)}u
                  </span>
                )}
              </div>
              {graded.daily.length === 0 ? (
                <div className="py-6 text-center text-[10px] uppercase tracking-wider text-krypt-muted/60">
                  No daily series yet
                </div>
              ) : (
                <EquityCurve daily={graded.daily} />
              )}
            </Card>

            <Card padded={false} className="overflow-hidden">
              <table className="w-full border-collapse text-left">
                <thead>
                  <tr className="border-b border-white/10 bg-white/[0.02]">
                    <th className={TH}>Tier</th>
                    <th className={cls(TH, 'text-right')}>N</th>
                    <th className={cls(TH, 'text-right')}>Units</th>
                    <th className={cls(TH, 'text-right')}>Win</th>
                  </tr>
                </thead>
                <tbody>
                  {ROI_TIERS.map((t) => {
                    const row = graded.overall.by_tier[t]
                    if (!row) return null
                    return (
                      <tr key={t} className="border-b border-white/5 last:border-0">
                        <td className={cls(TD, 'font-sans text-[10px] font-bold uppercase tracking-widest', TIER_TONE[t])}>
                          {t}
                        </td>
                        <td className={cls(TD, 'text-right text-krypt-muted')}>{row.n}</td>
                        <td
                          className={cls(
                            TD,
                            'text-right font-semibold',
                            row.units == null
                              ? 'text-krypt-muted/50'
                              : row.units >= 0
                                ? 'text-emerald-300'
                                : 'text-rose-400',
                          )}
                        >
                          {row.units == null ? 'not staked' : `${signed(row.units)}u`}
                        </td>
                        <td
                          className={cls(
                            TD,
                            'text-right',
                            row.win_pct >= 0.5 ? 'text-emerald-300' : 'text-rose-400',
                          )}
                        >
                          {fmtPct(row.win_pct)}%
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </Card>
          </div>
        </>
      )}
    </Section>
  )
}

// -------------------------------------------------------------------- page

interface Props {
  demo: boolean
  apiUp: boolean | null
}

/** PERFORMANCE — the model report card: tiles, per-model table, 30-day
 *  accuracy chart, CLV by sport. */
export default function PerfPage({ demo, apiUp }: Props) {
  const [perf, setPerf] = useState<PerformanceReport | null>(null)
  const [clv, setClv] = useState<ClvReport | null>(null)
  const [loading, setLoading] = useState(false)
  const roi = useRoi(demo)

  useEffect(() => {
    if (demo) {
      setPerf(DEMO_PERFORMANCE)
      setClv(DEMO_CLV)
      setLoading(false)
      return
    }
    let alive = true
    setLoading(true)
    setPerf(null)
    setClv(null)
    void Promise.all([fetchPerformance(), fetchClv()]).then(([p, c]) => {
      if (!alive) return
      setPerf(p)
      setClv(c)
      setLoading(false)
    })
    return () => {
      alive = false
    }
  }, [demo])

  const overall = perf?.overall ?? []
  const daily = perf?.daily ?? []
  const trackingSince = daily.length > 0 ? daily[0].date : new Date().toISOString().slice(0, 10)

  const settled = overall.reduce((a, g) => a + g.n, 0)
  const wAcc = settled
    ? overall.reduce((a, g) => a + (Number.isFinite(g.accuracy) ? g.accuracy * g.n : 0), 0) / settled
    : NaN
  const wLL = settled
    ? overall.reduce((a, g) => a + (Number.isFinite(g.log_loss) ? g.log_loss * g.n : 0), 0) / settled
    : NaN
  const clvPts = clv && clv.overall.n > 0 ? clv.overall.avg_clv_prob_pts : null

  return (
    <Page
      title="Performance"
      subtitle="The store grades every prediction automatically once games settle."
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
          Fetching performance…
        </div>
      ) : (
        <>
      <PickRecord roi={roi} />
      {overall.length === 0 ? (
        <>
          <Section>
            <div className="flex flex-col items-center rounded-2xl border border-dashed border-white/10 bg-black/20 px-6 py-14 text-center">
              <CalendarClock size={22} className="mb-3 text-scout-red-bright" />
              <div className="text-base font-semibold text-white">
                Tracking since <span className="font-mono">{trackingSince}</span> — no settled predictions yet
              </div>
              <p className="mt-2 max-w-lg text-sm text-krypt-muted">
                The store grades every prediction automatically: accuracy, log loss, Brier and CLV
                appear here as soon as captured games go final.
              </p>
            </div>
          </Section>
          <Section title="Enable tracking" description="Run the capture + settle jobs from the backend directory.">
            <Card>
              <ol className="space-y-3 text-sm text-white/85">
                <li className="flex flex-col gap-1">
                  <span className="text-xs font-semibold uppercase tracking-wider text-krypt-muted">
                    1 · Capture — a few times a day (records slates, predictions + odds snapshots)
                  </span>
                  <code className="w-fit rounded-lg border border-white/10 bg-black/40 px-3 py-1.5 font-mono text-xs text-emerald-300">
                    python -m kryptscout.jobs.capture
                  </code>
                </li>
                <li className="flex flex-col gap-1">
                  <span className="text-xs font-semibold uppercase tracking-wider text-krypt-muted">
                    2 · Settle — once daily, after the previous slate goes final
                  </span>
                  <code className="w-fit rounded-lg border border-white/10 bg-black/40 px-3 py-1.5 font-mono text-xs text-emerald-300">
                    python -m kryptscout.jobs.settle
                  </code>
                </li>
              </ol>
              <p className="mt-4 text-xs leading-relaxed text-krypt-muted">
                Schedule both with Windows Task Scheduler (<Kbd>schtasks</Kbd>) or cron — see{' '}
                <span className="font-mono text-[11px] text-white/70">backend/kryptscout/jobs/__init__.py</span>{' '}
                for ready-made commands. Multiple captures per day also power line movement and CLV.
              </p>
            </Card>
          </Section>
        </>
      ) : (
        <>
          <Section
            title="Model metrics"
            description="Prediction calibration — every model output graded on accuracy, independent of stakes or odds."
          >
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <StatTile label="Settled" value={settled} sub="graded predictions" />
              <StatTile
                label="Accuracy"
                value={Number.isFinite(wAcc) ? `${fmtPct(wAcc)}%` : '—'}
                tone={Number.isFinite(wAcc) && wAcc >= 0.5 ? 'success' : 'danger'}
                sub="weighted argmax hit rate"
              />
              <StatTile
                label="Log loss"
                value={num(wLL, 3)}
                sub="weighted · lower is better"
                help="Log loss scores how confident AND correct the predictions were — lower is better (0.693 = coin flip)"
              />
              <StatTile
                label="CLV avg"
                value={clvPts != null ? `${clvPts >= 0 ? '+' : ''}${num(clvPts, 2)}` : '—'}
                tone={clvPts != null ? (clvPts >= 0 ? 'success' : 'danger') : 'neutral'}
                sub="prob pts vs close · picked side"
                help="CLV = closing line value: whether our picks beat the market's final price — positive means we did"
              />
            </div>
          </Section>

          <Section
            title="Daily accuracy · last 30 days"
            description="Bar height = hit rate · dashed line = 50% · emerald ≥ 50%, red below."
          >
            <Card>
              {daily.length === 0 ? (
                <div className="py-6 text-center text-[10px] uppercase tracking-wider text-krypt-muted/60">
                  No daily series yet
                </div>
              ) : (
                <DailyChart daily={daily} />
              )}
            </Card>
          </Section>

          <div className="grid gap-4 xl:grid-cols-2">
            <Section
              title={`By sport + model · ${overall.length}`}
              description="ACC = argmax hit rate · logloss / Brier = calibration (lower is better)."
            >
              <Card padded={false} className="overflow-hidden">
                <table className="w-full border-collapse text-left">
                  <thead>
                    <tr className="border-b border-white/10 bg-white/[0.02]">
                      <th className={TH}>Sport</th>
                      <th className={TH}>Model</th>
                      <th className={cls(TH, 'text-right')}>N</th>
                      <th className={cls(TH, 'text-right')}>Acc</th>
                      <th className={cls(TH, 'text-right')}>Logloss</th>
                      <th className={cls(TH, 'text-right')}>Brier</th>
                    </tr>
                  </thead>
                  <tbody>
                    {overall.map((g) => (
                      <tr key={`${g.sport}-${g.model}`} className="border-b border-white/5 last:border-0">
                        <td className={cls(TD, 'font-sans text-[10px] font-bold uppercase tracking-widest text-scout-red-bright')}>
                          {g.sport}
                        </td>
                        <td className={cls(TD, 'text-[10px] text-white/80')}>{g.model}</td>
                        <td className={cls(TD, 'text-right text-krypt-muted')}>{g.n}</td>
                        <td
                          className={cls(
                            TD,
                            'text-right font-semibold',
                            Number.isFinite(g.accuracy) && g.accuracy >= 0.5
                              ? 'text-emerald-300'
                              : 'text-rose-400',
                          )}
                        >
                          {Number.isFinite(g.accuracy) ? `${fmtPct(g.accuracy)}%` : '—'}
                        </td>
                        <td className={cls(TD, 'text-right text-white/75')}>{num(g.log_loss, 3)}</td>
                        <td className={cls(TD, 'text-right text-white/75')}>{num(g.brier, 3)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            </Section>

            <Section
              title="Closing line value"
              description="Picked-side vig-free probability, close minus open — positive = beating the close."
            >
              <Card padded={false} className="overflow-hidden">
                {!clv || clv.overall.n === 0 || clv.by_sport.length === 0 ? (
                  <div className="px-4 py-8 text-center text-[10px] uppercase tracking-wider text-krypt-muted/60">
                    No CLV data — needs ≥2 odds snapshots per settled game
                  </div>
                ) : (
                  <table className="w-full border-collapse text-left">
                    <thead>
                      <tr className="border-b border-white/10 bg-white/[0.02]">
                        <th className={TH}>Sport</th>
                        <th className={cls(TH, 'text-right')}>N</th>
                        <th className={cls(TH, 'text-right')}>CLV pts</th>
                        <th className={cls(TH, 'text-right')}>BPS</th>
                      </tr>
                    </thead>
                    <tbody>
                      {clv.by_sport.map((s) => {
                        const pos = Number.isFinite(s.avg_clv_prob_pts) && s.avg_clv_prob_pts >= 0
                        return (
                          <tr key={s.sport} className="border-b border-white/5">
                            <td className={cls(TD, 'font-sans text-[10px] font-bold uppercase tracking-widest text-scout-red-bright')}>
                              {s.sport}
                            </td>
                            <td className={cls(TD, 'text-right text-krypt-muted')}>{s.n}</td>
                            <td className={cls(TD, 'text-right font-semibold', pos ? 'text-emerald-300' : 'text-rose-400')}>
                              {pos ? '+' : ''}
                              {num(s.avg_clv_prob_pts, 2)}
                            </td>
                            <td className={cls(TD, 'text-right', pos ? 'text-emerald-300/70' : 'text-rose-400/70')}>
                              {pos ? '+' : ''}
                              {num(s.avg_clv_bps, 0)}
                            </td>
                          </tr>
                        )
                      })}
                      <tr className="bg-white/[0.03]">
                        <td className={cls(TD, 'font-sans text-[10px] font-bold uppercase tracking-widest text-white')}>
                          All
                        </td>
                        <td className={cls(TD, 'text-right text-krypt-muted')}>{clv.overall.n}</td>
                        <td
                          className={cls(
                            TD,
                            'text-right font-semibold',
                            clv.overall.avg_clv_prob_pts >= 0 ? 'text-emerald-300' : 'text-rose-400',
                          )}
                        >
                          {clv.overall.avg_clv_prob_pts >= 0 ? '+' : ''}
                          {num(clv.overall.avg_clv_prob_pts, 2)}
                        </td>
                        <td
                          className={cls(
                            TD,
                            'text-right',
                            clv.overall.avg_clv_bps >= 0 ? 'text-emerald-300/70' : 'text-rose-400/70',
                          )}
                        >
                          {clv.overall.avg_clv_bps >= 0 ? '+' : ''}
                          {num(clv.overall.avg_clv_bps, 0)}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                )}
              </Card>
            </Section>

            {clv?.by_tier && (
              <Section
                title="CLV by pick tier"
                description="Does each tier beat the CLOSE? This — not win rate — separates a real edge from a hot streak. A win% far above the CLV means variance you can't bank on. Watch STRONG as picks accumulate."
              >
                <Card padded={false} className="overflow-hidden">
                  <table className="w-full border-collapse text-left">
                    <thead>
                      <tr className="border-b border-white/10 bg-white/[0.02]">
                        <th className={TH}>Tier</th>
                        <th className={cls(TH, 'text-right')}>N</th>
                        <th className={cls(TH, 'text-right')}>Win%</th>
                        <th className={cls(TH, 'text-right')}>CLV bps (±err)</th>
                        <th className={cls(TH, 'text-right')}>Verdict</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(['STRONG', 'LEAN', 'MODEL'] as const).map((tier) => {
                        const d = clv.by_tier![tier]
                        const bps = d.avg_clv_bps
                        const pos = bps != null && bps >= 0
                        // The error bar swallowing the estimate = inconclusive.
                        const inconclusive =
                          d.se_bps != null && bps != null && Math.abs(bps) < d.se_bps
                        let verdict = { label: 'collecting…', tone: 'text-krypt-muted/50' }
                        if (d.n >= 5) {
                          if (d.significant)
                            verdict = pos
                              ? { label: 'REAL EDGE', tone: 'text-emerald-300' }
                              : { label: 'REAL FADE', tone: 'text-rose-400' }
                          else if (inconclusive)
                            verdict = { label: '≈ zero (variance)', tone: 'text-amber-300/80' }
                          else verdict = { label: `provisional`, tone: 'text-amber-300/60' }
                        }
                        return (
                          <tr key={tier} className="border-b border-white/5">
                            <td
                              className={cls(
                                TD,
                                'font-sans text-[10px] font-bold uppercase tracking-widest',
                                tier === 'STRONG' ? 'text-scout-red-bright' : 'text-krypt-muted',
                              )}
                            >
                              {tier}
                            </td>
                            <td className={cls(TD, 'text-right text-krypt-muted')}>{d.n}</td>
                            <td className={cls(TD, 'text-right text-krypt-muted')}>
                              {d.win_rate != null ? `${Math.round(d.win_rate * 100)}%` : '—'}
                            </td>
                            <td
                              className={cls(
                                TD,
                                'text-right font-semibold',
                                d.n === 0
                                  ? 'text-krypt-muted/40'
                                  : pos
                                    ? 'text-emerald-300'
                                    : 'text-rose-400',
                              )}
                            >
                              {d.n === 0 || bps == null
                                ? '—'
                                : `${pos ? '+' : ''}${num(bps, 0)}${d.se_bps != null ? ` ±${num(d.se_bps, 0)}` : ''}`}
                            </td>
                            <td
                              className={cls(
                                TD,
                                'text-right text-[10px] font-bold uppercase tracking-wider',
                                verdict.tone,
                              )}
                            >
                              {verdict.label}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                  {clv.note && (
                    <p className="px-4 py-3 text-[10px] leading-relaxed text-krypt-muted/60">
                      {clv.note}
                    </p>
                  )}
                </Card>
              </Section>
            )}
          </div>
        </>
      )}
        </>
      )}
    </Page>
  )
}

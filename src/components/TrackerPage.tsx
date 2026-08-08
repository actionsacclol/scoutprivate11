/**
 * TRACKER — two records, side by side.
 *
 *   MY BETS    the USER'S bet log and P&L (the headline record): every
 *              straight bet AND parlay they hit TRACK on, graded
 *              automatically when games settle.
 *   BOT PICKS  the bot's own record: EVERY +EV value pick it has ever
 *              recommended (pending and graded, flat 1u), so users can see
 *              the whole bot's P&L — not just the picks they tailed. This is
 *              the same number the dashboard's UNITS tile reports.
 */
import { useEffect, useMemo, useState } from 'react'
import { Bot, Loader2, NotebookPen, X } from 'lucide-react'
import type { GameCard } from '../types'
import { SPORT_TAG, type Sport } from '../types'
import type { GuardianWarning, PickLogRow, RoiDaily, UserClvReport } from '../api'
import { fetchGuardian, fetchUserClv } from '../api'
import type { MyBetRow, MyParlayRow } from '../mybets'
import { useMyBets } from '../mybets'
import { usePickLog } from '../share/usePickLog'
import { bookAbbrev, fmtAmerican, fmtPct } from '../oddsmath'
import { fmtKick } from '../cardview'
import BookLink from './BookLink'
import { Badge, Card, Empty, Page, Section, StatTile } from './common'
import { cls } from '../utils/format'
import { useUiMode } from '../uiMode'

const signed = (v: number, digits = 1) => `${v >= 0 ? '+' : ''}${v.toFixed(digits)}`

const TH = 'px-3 py-2 text-left text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70'
const TD = 'px-3 py-1.5 font-mono text-[11px]'

// ------------------------------------------------------------- equity curve

/** Cumulative-units equity curve — same zero-split style as PerfPage: area +
 *  line, emerald above zero / rose below, dashed baseline, day tooltips. */
function EquityCurve({ daily, clipId }: { daily: RoiDaily[]; clipId: string }) {
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
  const line = daily
    .map((d, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(d.cum_units).toFixed(1)}`)
    .join(' ')
  const area = `${line} L${w},${zeroY.toFixed(1)} L0,${zeroY.toFixed(1)} Z`
  const last = daily[daily.length - 1]
  return (
    <div className="overflow-x-auto">
      <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="min-w-[480px]">
        <defs>
          <clipPath id={`${clipId}-above`}>
            <rect x={0} y={0} width={w} height={zeroY} />
          </clipPath>
          <clipPath id={`${clipId}-below`}>
            <rect x={0} y={zeroY} width={w} height={h - zeroY} />
          </clipPath>
        </defs>
        <line x1={0} y1={zeroY} x2={w} y2={zeroY} stroke="rgba(255,255,255,0.18)" strokeWidth="1" strokeDasharray="3 3" />
        <path d={area} fill="rgba(52,211,153,0.22)" clipPath={`url(#${clipId}-above)`} />
        <path d={area} fill="rgba(225,29,72,0.22)" clipPath={`url(#${clipId}-below)`} />
        <path d={line} fill="none" stroke="rgba(52,211,153,0.9)" strokeWidth="1.8" clipPath={`url(#${clipId}-above)`} />
        <path d={line} fill="none" stroke="rgba(244,63,94,0.9)" strokeWidth="1.8" clipPath={`url(#${clipId}-below)`} />
        {last && (
          <circle
            cx={X(daily.length - 1)}
            cy={Y(last.cum_units)}
            r={3}
            fill={last.cum_units >= 0 ? 'rgb(52,211,153)' : 'rgb(244,63,94)'}
          />
        )}
        {daily.map((d, i) => (
          <rect key={d.date} x={X(i) - stepW / 2} y={0} width={stepW} height={h} fill="transparent">
            <title>{`${d.date} · ${signed(d.units)}u · cum ${signed(d.cum_units)}u`}</title>
          </rect>
        ))}
      </svg>
    </div>
  )
}

// ----------------------------------------------------------------- bet rows

function StatusChip({ result }: { result: MyBetRow['result'] }) {
  if (result === null) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded border border-amber-400/30 bg-amber-500/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest text-amber-300">
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-300" />
        PENDING
      </span>
    )
  }
  const tone: Record<'W' | 'L' | 'P', string> = {
    W: 'border-emerald-400/30 bg-emerald-500/10 text-emerald-300',
    L: 'border-rose-400/30 bg-rose-500/10 text-rose-300',
    P: 'border-white/10 bg-white/5 text-krypt-muted',
  }
  const label: Record<'W' | 'L' | 'P', string> = { W: 'WIN', L: 'LOSS', P: 'PUSH' }
  return (
    <span
      className={cls(
        'inline-flex items-center rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest',
        tone[result],
      )}
    >
      {label[result]}
    </span>
  )
}

/** Two-step delete: first click arms ("SURE?"), second deletes. Disarms on
 *  mouse-leave / 3s. Only rendered for ungraded rows — graded is permanent. */
function DeleteBtn({ onDelete, disabled }: { onDelete: () => void; disabled: boolean }) {
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    if (!armed) return
    const t = setTimeout(() => setArmed(false), 3000)
    return () => clearTimeout(t)
  }, [armed])
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => {
        if (armed) onDelete()
        else setArmed(true)
      }}
      onMouseLeave={() => setArmed(false)}
      title={armed ? 'Click again to delete this bet' : 'Delete (ungraded bets only)'}
      className={cls(
        'inline-flex h-6 items-center justify-center rounded-md border transition disabled:cursor-not-allowed disabled:opacity-40',
        armed
          ? 'w-14 border-rose-400/50 bg-rose-500/20 text-[9px] font-bold tracking-widest text-rose-200'
          : 'w-6 border-white/10 bg-white/[0.03] text-krypt-muted/60 hover:border-rose-400/40 hover:bg-rose-500/10 hover:text-rose-300',
      )}
    >
      {armed ? 'SURE?' : <X size={11} />}
    </button>
  )
}

function BetRow({
  bet,
  busy,
  onRemove,
}: {
  bet: MyBetRow
  busy: boolean
  onRemove: (id: number) => void
}) {
  const kick = fmtKick(bet.kickoff)
  const tennis = bet.sport === 'tennis'
  const matchup = tennis ? `${bet.home} vs ${bet.away}` : `${bet.away} @ ${bet.home}`
  const tag = SPORT_TAG[bet.sport as Sport] ?? bet.sport.toUpperCase().slice(0, 4)
  return (
    <tr className="border-b border-white/5 last:border-0 hover:bg-white/[0.02]">
      <td className={cls(TD, 'whitespace-nowrap font-sans')}>
        <StatusChip result={bet.result} />
      </td>
      <td className={cls(TD, 'whitespace-nowrap text-[10px] text-krypt-muted/70')}>
        {kick.date} {kick.time}
      </td>
      <td className={cls(TD, 'font-sans')}>
        <span className="mr-2 rounded border border-white/10 bg-white/5 px-1 py-0.5 align-middle text-[8px] font-bold uppercase tracking-widest text-krypt-muted">
          {tag}
        </span>
        <span className="text-[11px] text-white/70" title={matchup}>
          {matchup}
        </span>
      </td>
      <td className={cls(TD, 'font-sans text-[11px] font-bold text-white')} title={bet.team}>
        {bet.team}
      </td>
      <td className={cls(TD, 'text-right text-white/85')}>{fmtAmerican(bet.price)}</td>
      <td className={cls(TD, 'text-[10px]')}>
        {bet.book ? (
          <BookLink book={bet.book} className="text-white/75 hover:text-white">
            {bookAbbrev(bet.book)}
          </BookLink>
        ) : (
          <span className="text-krypt-muted/50" title="Tracked at the model fair line — no book price">
            FAIR
          </span>
        )}
      </td>
      <td className={cls(TD, 'text-right text-krypt-muted')}>{bet.stake_units.toFixed(1)}u</td>
      <td
        className={cls(
          TD,
          'text-right font-semibold',
          bet.result === null
            ? 'text-krypt-muted/40'
            : (bet.units ?? 0) > 0
              ? 'text-emerald-300'
              : (bet.units ?? 0) < 0
                ? 'text-rose-400'
                : 'text-krypt-muted',
        )}
      >
        {bet.result === null ? '—' : `${signed(bet.units ?? 0, 2)}u`}
      </td>
      <td className={cls(TD, 'max-w-[140px] font-sans')}>
        {bet.note ? (
          <span className="block truncate text-[10px] text-krypt-muted/70" title={bet.note}>
            {bet.note}
          </span>
        ) : (
          <span className="text-krypt-muted/30">—</span>
        )}
      </td>
      <td className={cls(TD, 'w-16 text-right')}>
        {bet.result === null && <DeleteBtn disabled={busy} onDelete={() => onRemove(bet.id)} />}
      </td>
    </tr>
  )
}

// -------------------------------------------------------------- parlay rows

/** Per-leg result dot: emerald W, rose L, muted P, pulsing amber pending. */
function LegDot({ result }: { result: MyBetRow['result'] }) {
  if (result === null)
    return <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-amber-300" title="Leg pending" />
  const tone: Record<'W' | 'L' | 'P', string> = {
    W: 'bg-emerald-300',
    L: 'bg-rose-400',
    P: 'bg-white/30',
  }
  const label: Record<'W' | 'L' | 'P', string> = { W: 'Leg won', L: 'Leg lost', P: 'Leg pushed' }
  return <span className={cls('h-1.5 w-1.5 shrink-0 rounded-full', tone[result])} title={label[result]} />
}

function ParlayCard({
  parlay,
  busy,
  simple,
  onRemove,
}: {
  parlay: MyParlayRow
  busy: boolean
  simple: boolean
  onRemove: (id: number) => void
}) {
  const units = parlay.units ?? 0
  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.02] px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <StatusChip result={parlay.result} />
        <span className="text-[11px] font-bold uppercase tracking-widest text-white">
          {parlay.legs.length}-leg parlay
        </span>
        <span
          className="font-mono text-[11px] text-white/85"
          title={simple ? 'Combined price when you tracked it' : 'Combined American price at track time'}
        >
          {fmtAmerican(parlay.price)}
        </span>
        <span className="font-mono text-[10px] text-krypt-muted">
          {parlay.stake_units.toFixed(1)}u stake
        </span>
        {parlay.note && (
          <span className="max-w-[180px] truncate text-[10px] text-krypt-muted/70" title={parlay.note}>
            {parlay.note}
          </span>
        )}
        <span
          className={cls(
            'ml-auto font-mono text-xs font-semibold',
            parlay.result === null
              ? 'text-krypt-muted/40'
              : units > 0
                ? 'text-emerald-300'
                : units < 0
                  ? 'text-rose-400'
                  : 'text-krypt-muted',
          )}
        >
          {parlay.result === null ? '—' : `${signed(units, 2)}u`}
        </span>
        {parlay.result === null && (
          <DeleteBtn disabled={busy} onDelete={() => onRemove(parlay.id)} />
        )}
      </div>
      <div className="mt-2 space-y-1 border-t border-white/5 pt-2">
        {parlay.legs.map((leg) => {
          const kick = fmtKick(leg.kickoff)
          const tag = SPORT_TAG[leg.sport as Sport] ?? leg.sport.toUpperCase().slice(0, 4)
          const opponent =
            leg.side === 'home' ? leg.away : leg.side === 'away' ? leg.home : `${leg.away} / ${leg.home}`
          return (
            <div key={`${leg.game_id}:${leg.side}`} className="flex items-center gap-2">
              <LegDot result={leg.result} />
              <span className="rounded border border-white/10 bg-white/5 px-1 py-0.5 text-[8px] font-bold uppercase tracking-widest text-krypt-muted">
                {tag}
              </span>
              <span className="min-w-0 flex-1 truncate text-[11px] font-semibold text-white/85" title={leg.team}>
                {leg.team}
                <span className="ml-1.5 font-normal text-krypt-muted/60">
                  {leg.side === 'draw' ? opponent : `vs ${opponent}`}
                </span>
              </span>
              <span className="whitespace-nowrap font-mono text-[10px] text-krypt-muted/70">
                {kick.date} {kick.time}
              </span>
              <span className="w-14 text-right font-mono text-[11px] text-white/85">
                {fmtAmerican(leg.price)}
              </span>
              <span className="w-10 text-right font-mono text-[9px] text-krypt-muted/60">
                {leg.book ? bookAbbrev(leg.book) : 'FAIR'}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ----------------------------------------------------------- bot pick rows

function TierChip({ tier }: { tier: 'STRONG' | 'LEAN' }) {
  return (
    <span
      className={cls(
        'rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest',
        tier === 'STRONG'
          ? 'border-emerald-400/30 bg-emerald-500/10 text-emerald-300'
          : 'border-emerald-400/15 bg-emerald-500/[0.05] text-emerald-300/70',
      )}
      title={tier === 'STRONG' ? 'Value pick with EV ≥ 4% per $1' : 'Value pick with positive EV below 4%'}
    >
      {tier}
    </span>
  )
}

function PickRow({ pick }: { pick: PickLogRow }) {
  const kick = fmtKick(pick.kickoff)
  const tennis = pick.sport === 'tennis'
  const matchup = tennis ? `${pick.home} vs ${pick.away}` : `${pick.away} @ ${pick.home}`
  const tag = SPORT_TAG[pick.sport as Sport] ?? pick.sport.toUpperCase().slice(0, 4)
  return (
    <tr className="border-b border-white/5 last:border-0 hover:bg-white/[0.02]">
      <td className={cls(TD, 'whitespace-nowrap font-sans')}>
        <StatusChip result={pick.result} />
      </td>
      <td className={cls(TD, 'whitespace-nowrap text-[10px] text-krypt-muted/70')}>
        {kick.date} {kick.time}
      </td>
      <td className={cls(TD, 'font-sans')}>
        <span className="mr-2 rounded border border-white/10 bg-white/5 px-1 py-0.5 align-middle text-[8px] font-bold uppercase tracking-widest text-krypt-muted">
          {tag}
        </span>
        <span className="text-[11px] text-white/70" title={matchup}>
          {matchup}
        </span>
      </td>
      <td className={cls(TD, 'font-sans text-[11px] font-bold text-white')} title={pick.team}>
        {pick.team}
      </td>
      <td className={cls(TD, 'font-sans')}>
        <TierChip tier={pick.tier} />
      </td>
      <td className={cls(TD, 'text-right text-white/85')}>{fmtAmerican(pick.price)}</td>
      <td className={cls(TD, 'text-[10px]')}>
        {pick.book ? (
          <BookLink book={pick.book} className="text-white/75 hover:text-white">
            {bookAbbrev(pick.book)}
          </BookLink>
        ) : (
          <span className="text-krypt-muted/50">—</span>
        )}
      </td>
      <td className={cls(TD, 'text-right text-emerald-300/90')}>
        {pick.ev !== null ? `+${fmtPct(pick.ev)}%` : '—'}
      </td>
      <td
        className={cls(
          TD,
          'text-right font-semibold',
          pick.result === null
            ? 'text-krypt-muted/40'
            : (pick.units ?? 0) > 0
              ? 'text-emerald-300'
              : (pick.units ?? 0) < 0
                ? 'text-rose-400'
                : 'text-krypt-muted',
        )}
      >
        {pick.result === null ? '—' : `${signed(pick.units ?? 0, 2)}u`}
      </td>
    </tr>
  )
}

// --------------------------------------------------------------------- page

type StatusFilter = 'all' | 'pending' | 'settled'
type View = 'mine' | 'bot'

function FilterChips<T extends string>({
  value,
  onChange,
  chips,
}: {
  value: T
  onChange: (v: T) => void
  chips: Array<{ id: T; label: string; count: number }>
}) {
  return (
    <>
      {chips.map((chip) => {
        const active = value === chip.id
        return (
          <button
            key={chip.id}
            onClick={() => onChange(chip.id)}
            className={cls(
              'flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[10px] font-bold tracking-widest transition',
              active
                ? 'border-scout-red/50 bg-white/10 text-white ring-1 ring-scout-red/40'
                : 'border-white/10 bg-white/[0.03] text-krypt-muted hover:border-white/20 hover:text-white',
            )}
          >
            {chip.label}
            <span
              className={cls(
                'min-w-[16px] rounded px-1 text-center font-mono text-[9px] font-semibold leading-4',
                active ? 'bg-scout-red/25 text-scout-red-bright' : 'bg-white/5 text-krypt-muted',
              )}
            >
              {chip.count}
            </span>
          </button>
        )
      })}
    </>
  )
}

function SportChips({
  value,
  onChange,
  counts,
  total,
}: {
  value: string
  onChange: (s: string) => void
  counts: Array<[string, number]>
  total: number
}) {
  if (counts.length <= 1) return null
  return (
    <span className="ml-1 flex flex-wrap items-center gap-1.5 border-l border-white/10 pl-2.5">
      {[['all', total] as [string, number], ...counts].map(([s, n]) => {
        const active = value === s
        const label = s === 'all' ? 'ALL' : (SPORT_TAG[s as Sport] ?? s.toUpperCase().slice(0, 4))
        return (
          <button
            key={s}
            onClick={() => onChange(s)}
            className={cls(
              'flex items-center gap-1 rounded-lg border px-2 py-1 text-[10px] font-semibold tracking-wider transition',
              active
                ? 'border-white/20 bg-white/10 text-white'
                : 'border-white/10 bg-white/[0.02] text-krypt-muted/70 hover:border-white/20 hover:text-white',
            )}
          >
            {label}
            <span className="font-mono text-[9px] opacity-70">{n}</span>
          </button>
        )
      })}
    </span>
  )
}

/** The bot's full pick record: headline tiles, equity curve, pick log. */
function BotPicksView({ demo, apiUp, simple }: { demo: boolean; apiUp: boolean | null; simple: boolean }) {
  const report = usePickLog(demo)
  const [status, setStatus] = useState<StatusFilter>('all')
  const [sportF, setSportF] = useState<string>('all')

  const picks = useMemo(() => report?.picks ?? [], [report])
  const pending = useMemo(() => picks.filter((p) => p.result === null), [picks])
  const settled = useMemo(() => picks.filter((p) => p.result !== null), [picks])
  const sportCounts = useMemo(() => {
    const c = new Map<string, number>()
    for (const p of picks) c.set(p.sport, (c.get(p.sport) ?? 0) + 1)
    return [...c.entries()].sort((a, b) => b[1] - a[1])
  }, [picks])
  const shown = useMemo(() => {
    const base = status === 'pending' ? pending : status === 'settled' ? settled : picks
    return sportF === 'all' ? base : base.filter((p) => p.sport === sportF)
  }, [status, sportF, picks, pending, settled])

  if (report === null) {
    return apiUp === false && !demo ? (
      <Empty
        title="Bot record unavailable"
        message={
          simple
            ? 'Data feed offline — the pick record returns when it reconnects. Demo mode shows a sample.'
            : 'API offline — /api/picklog is unreachable. Enable demo mode to review a sample record.'
        }
      />
    ) : (
      <div className="flex items-center gap-2 py-16 text-xs uppercase tracking-wider text-krypt-muted">
        <Loader2 size={14} className="animate-spin text-scout-red-bright" />
        Fetching the bot&apos;s pick record…
      </div>
    )
  }

  if (picks.length === 0) {
    return (
      <Empty
        title="No value picks recorded yet"
        message="The bot logs every +EV pick it recommends as boards are captured. Give it a slate or two — the full record builds here automatically."
      />
    )
  }

  const pnl = report.pnl
  const units = pnl.units
  const hasGraded = pnl.graded > 0

  return (
    <>
      {/* Headline strip — the WHOLE bot record, tailed or not. */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div
          className={cls(
            'rounded-2xl border px-4 py-3 shadow-krypt-card',
            units >= 0
              ? 'border-emerald-400/25 bg-emerald-500/[0.06]'
              : 'border-rose-400/25 bg-rose-500/[0.06]',
          )}
        >
          <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
            Bot P&amp;L
          </div>
          <div
            className={cls(
              'mt-1 font-mono text-3xl font-bold leading-none tracking-tight',
              units >= 0 ? 'text-emerald-300' : 'text-rose-400',
            )}
          >
            {hasGraded ? `${signed(units)}u` : '—'}
          </div>
          <div className="mt-1.5 text-[10px] uppercase tracking-wider text-krypt-muted/70">
            {hasGraded ? 'every value pick · flat 1u' : 'grades when games settle'}
          </div>
        </div>
        <StatTile label="Record" value={pnl.record} sub="wins - losses - pushes" />
        <StatTile
          label="ROI"
          value={hasGraded ? `${signed(pnl.roi_pct ?? 0)}%` : '—'}
          tone={!hasGraded ? 'neutral' : (pnl.roi_pct ?? 0) >= 0 ? 'success' : 'danger'}
          sub="units won / units staked"
          help="Return on investment across every graded value pick at a flat 1u stake"
        />
        <StatTile
          label="Picks"
          value={pnl.n}
          sub={`${pnl.graded} graded · ${pending.length} pending`}
        />
      </div>

      {pnl.daily.length > 0 && (
        <Card className="mt-4">
          <div className="mb-2 flex items-baseline justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
              Cumulative units · every bot pick
            </span>
            <span
              className={cls(
                'font-mono text-xs font-bold',
                pnl.daily[pnl.daily.length - 1].cum_units >= 0 ? 'text-emerald-300' : 'text-rose-400',
              )}
            >
              {signed(pnl.daily[pnl.daily.length - 1].cum_units)}u
            </span>
          </div>
          <EquityCurve daily={pnl.daily} clipId="trk-bot-eq" />
        </Card>
      )}

      <Section
        title={`Pick log · ${shown.length} of ${picks.length}`}
        description={
          simple
            ? 'Every +EV pick the bot has made — including the ones you didn’t bet.'
            : 'Every recorded value pick (STRONG/LEAN with a live price), graded at a flat 1u by the settle job — the same number the dashboard UNITS tile reports.'
        }
        actions={
          <div className="flex flex-wrap items-center gap-1.5">
            <FilterChips
              value={status}
              onChange={setStatus}
              chips={[
                { id: 'all', label: 'ALL', count: picks.length },
                { id: 'pending', label: 'PENDING', count: pending.length },
                { id: 'settled', label: 'SETTLED', count: settled.length },
              ]}
            />
            <SportChips value={sportF} onChange={setSportF} counts={sportCounts} total={picks.length} />
          </div>
        }
      >
        {shown.length === 0 ? (
          <Empty
            title="No picks match the current filters"
            message="Widen the status or sport filter above — the record has picks outside this view."
          />
        ) : (
          <Card padded={false} className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-left">
                <thead>
                  <tr className="border-b border-white/10 bg-white/[0.02]">
                    <th className={TH}>Status</th>
                    <th className={TH}>Kickoff</th>
                    <th className={TH}>Matchup</th>
                    <th className={TH}>Pick</th>
                    <th className={TH}>Tier</th>
                    <th className={cls(TH, 'text-right')}>Price</th>
                    <th className={TH}>Book</th>
                    <th className={cls(TH, 'text-right')}>EV</th>
                    <th className={cls(TH, 'text-right')}>Units</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((p) => (
                    <PickRow key={`${p.game_id}:${p.model}`} pick={p} />
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </Section>
    </>
  )
}

/** SLIP GUARDIAN — injury warnings (OUT / doubtful / IR) on games carrying
 *  the user's pending bets. The assistant move: surface the scratch BEFORE
 *  kickoff, while there's still time to hedge or accept it knowingly. */
function SlipGuardian() {
  const [warnings, setWarnings] = useState<GuardianWarning[] | null>(null)
  useEffect(() => {
    let alive = true
    void fetchGuardian().then((r) => alive && setWarnings(r?.warnings ?? null))
    return () => {
      alive = false
    }
  }, [])
  if (!warnings || warnings.length === 0) return null
  return (
    <div className="mt-4 rounded-xl border border-amber-400/25 bg-amber-500/[0.07] px-3 py-2.5">
      <div className="mb-1 text-[10px] font-bold uppercase tracking-[0.18em] text-amber-300">
        ⚠ Slip guardian · {warnings.length} injury flag{warnings.length === 1 ? '' : 's'} on your pending bets
      </div>
      <ul className="space-y-0.5">
        {warnings.slice(0, 6).map((w, i) => (
          <li key={`${w.bet_id}-${i}`} className="text-xs text-amber-100/90">
            <span className="font-semibold">{w.player}</span>{' '}
            <span className="font-mono text-[10px] uppercase text-amber-300/90">{w.status}</span>{' '}
            ({w.injured_team}) — affects your <span className="font-semibold">{w.bet_team}</span> bet,{' '}
            {w.away} @ {w.home}
          </li>
        ))}
        {warnings.length > 6 && (
          <li className="text-[10px] uppercase tracking-wider text-amber-300/70">
            +{warnings.length - 6} more
          </li>
        )}
      </ul>
    </div>
  )
}

/** CLV COACH — every logged bet vs the last pregame odds capture. Beating
 *  the close consistently is the strongest known predictor of long-run
 *  profit; this is the stat that makes users better, not just graded. */
function ClvCoach() {
  const [clv, setClv] = useState<UserClvReport | null>(null)
  useEffect(() => {
    let alive = true
    void fetchUserClv().then((r) => alive && setClv(r))
    return () => {
      alive = false
    }
  }, [])
  if (!clv || clv.overall.n_evaluated === 0) return null
  const o = clv.overall
  const good = (o.avg_clv_pct ?? 0) >= 1
  const bad = (o.avg_clv_pct ?? 0) <= -1
  return (
    <Card className="mt-4">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
          CLV coach · your prices vs the close
        </span>
        <span className="text-[9px] uppercase tracking-wider text-krypt-muted/60">
          {o.n_evaluated} of {o.n_bets} bets evaluated
        </span>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <StatTile
          label="Beat the close"
          value={o.beat_rate != null ? `${fmtPct(o.beat_rate)}%` : '—'}
          tone={o.beat_rate != null && o.beat_rate > 0.52 ? 'success' : 'neutral'}
          sub={`${o.beat_close} bets closed worse than you got`}
          help="Share of your bets whose price (or totals line) ended up better than the market's final pregame number"
        />
        <StatTile
          label="Avg CLV"
          value={o.avg_clv_pct != null ? `${o.avg_clv_pct >= 0 ? '+' : ''}${o.avg_clv_pct}%` : '—'}
          tone={good ? 'success' : bad ? 'danger' : 'neutral'}
          sub="moneyline price edge vs close"
          help="Average price advantage vs the closing price — sustained positive CLV is the strongest predictor of long-run profit"
        />
        <StatTile
          label="Totals line move"
          value={o.avg_line_move_pts != null ? `${o.avg_line_move_pts >= 0 ? '+' : ''}${o.avg_line_move_pts} pts` : '—'}
          tone={(o.avg_line_move_pts ?? 0) > 0 ? 'success' : 'neutral'}
          sub="line movement in your favor"
        />
      </div>
      <p
        className={cls(
          'mt-3 rounded-lg border px-2.5 py-2 text-xs leading-relaxed',
          good
            ? 'border-emerald-400/25 bg-emerald-500/[0.08] text-emerald-200'
            : bad
              ? 'border-rose-400/25 bg-rose-500/[0.08] text-rose-200'
              : 'border-white/10 bg-white/[0.03] text-krypt-muted',
        )}
      >
        {clv.verdict}
      </p>
    </Card>
  )
}

export default function TrackerPage({
  demo,
  apiUp,
}: {
  boards: Record<string, GameCard[]>
  demo: boolean
  apiUp: boolean | null
}) {
  const simple = useUiMode() === 'simple'
  const { bets, parlays, pnl, loading, busy, remove, removeParlay } = useMyBets(demo)
  // The bot's record leads — it's the product's proof; MY BETS is one tab away.
  const [view, setView] = useState<View>('bot')
  const [status, setStatus] = useState<StatusFilter>('all')
  const [sportF, setSportF] = useState<string>('all')

  // Newest kickoff first — pending (upcoming) bets naturally float to the top.
  const sorted = useMemo(
    () => [...bets].sort((a, b) => (a.kickoff < b.kickoff ? 1 : a.kickoff > b.kickoff ? -1 : 0)),
    [bets],
  )
  const pending = useMemo(() => sorted.filter((b) => b.result === null), [sorted])
  const settled = useMemo(() => sorted.filter((b) => b.result !== null), [sorted])

  const sportCounts = useMemo(() => {
    const c = new Map<string, number>()
    for (const b of sorted) c.set(b.sport, (c.get(b.sport) ?? 0) + 1)
    return [...c.entries()].sort((a, b) => b[1] - a[1])
  }, [sorted])

  const shown = useMemo(() => {
    const base = status === 'pending' ? pending : status === 'settled' ? settled : sorted
    return sportF === 'all' ? base : base.filter((b) => b.sport === sportF)
  }, [status, sportF, sorted, pending, settled])

  const sortedParlays = useMemo(
    () =>
      [...parlays].sort((a, b) => (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0)),
    [parlays],
  )

  const units = pnl?.units ?? 0
  const hasGraded = (pnl?.graded ?? 0) > 0
  const pendingParlays = sortedParlays.filter((p) => p.result === null).length
  const empty = bets.length === 0 && sortedParlays.length === 0

  const viewTabs: Array<{ id: View; label: string; help: string }> = [
    {
      id: 'bot',
      label: 'BOT PICKS',
      help: simple
        ? 'Every pick the bot has made, and how it all added up'
        : "The bot's full value-pick record at flat 1u — tailed or not",
    },
    {
      id: 'mine',
      label: 'MY BETS',
      help: 'Your tracked bets and parlays — your P&L',
    },
  ]

  return (
    <Page
      title="Tracker"
      subtitle={
        simple
          ? 'Your bets and the bot’s full record — everything grades automatically when games settle.'
          : 'BOT PICKS = every +EV pick the bot made, flat 1u · MY BETS = what you tracked, at your stakes.'
      }
      actions={
        <>
          {demo && <Badge tone="gradient">DEMO</Badge>}
          {apiUp === false && !demo && <Badge tone="danger">API DOWN</Badge>}
        </>
      }
    >
      {/* View switch: the user's record vs the whole bot's record. */}
      <div className="mb-4 flex items-center gap-1.5">
        {viewTabs.map((tab) => {
          const active = view === tab.id
          return (
            <button
              key={tab.id}
              onClick={() => setView(tab.id)}
              title={tab.help}
              aria-pressed={active}
              className={cls(
                'flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-[11px] font-bold tracking-widest transition',
                active
                  ? 'border-scout-red/50 bg-white/10 text-white shadow-scout-glow ring-1 ring-scout-red/40'
                  : 'border-white/10 bg-white/[0.03] text-krypt-muted hover:border-white/20 hover:text-white',
              )}
            >
              {tab.id === 'bot' && <Bot size={12} className={active ? 'text-scout-red-bright' : undefined} />}
              {tab.label}
            </button>
          )
        })}
      </div>

      {view === 'bot' ? (
        <BotPicksView demo={demo} apiUp={apiUp} simple={simple} />
      ) : loading ? (
        <div className="flex items-center gap-2 py-16 text-xs uppercase tracking-wider text-krypt-muted">
          <Loader2 size={14} className="animate-spin text-scout-red-bright" />
          Fetching your bets…
        </div>
      ) : empty ? (
        <>
          {apiUp === false && !demo && (
            <div className="mb-4 rounded-xl border border-amber-400/25 bg-amber-500/[0.07] px-3 py-2 text-[10px] font-semibold uppercase tracking-wider text-amber-300">
              {simple
                ? 'Data feed offline — bets you track will appear once it reconnects. Demo mode has a sample log.'
                : 'API offline — the live bet log is unavailable. Enable demo mode to review a sample tracker.'}
            </div>
          )}
          <Empty
            title="No bets tracked yet"
            message="Hit TRACK on any pick — or TRACK PARLAY on a slip. Your record lives here, graded automatically when games settle."
            action={
              <span className="inline-flex items-center gap-2 text-[10px] font-bold uppercase tracking-widest text-krypt-muted">
                <NotebookPen size={12} className="text-scout-red-bright" />
                Find TRACK BET on every pick card
              </span>
            }
          />
        </>
      ) : (
        <>
          {/* Headline P&L strip — the user's record IS the headline number. */}
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <div
              className={cls(
                'rounded-2xl border px-4 py-3 shadow-krypt-card',
                units >= 0
                  ? 'border-emerald-400/25 bg-emerald-500/[0.06]'
                  : 'border-rose-400/25 bg-rose-500/[0.06]',
              )}
            >
              <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
                My P&amp;L
              </div>
              <div
                className={cls(
                  'mt-1 font-mono text-3xl font-bold leading-none tracking-tight',
                  units >= 0 ? 'text-emerald-300' : 'text-rose-400',
                )}
              >
                {hasGraded ? `${signed(units)}u` : '—'}
              </div>
              <div className="mt-1.5 text-[10px] uppercase tracking-wider text-krypt-muted/70">
                {hasGraded ? 'net units · bets + parlays you tracked' : 'grades when games settle'}
              </div>
            </div>
            <StatTile
              label="Record"
              value={pnl?.record ?? '0-0-0'}
              sub="wins - losses - pushes"
            />
            <StatTile
              label="ROI"
              value={hasGraded ? `${signed(pnl?.roi_pct ?? 0)}%` : '—'}
              tone={!hasGraded ? 'neutral' : (pnl?.roi_pct ?? 0) >= 0 ? 'success' : 'danger'}
              sub="units won / units staked"
              help="Return on investment across your graded bets and parlays"
            />
            <StatTile
              label="Bets"
              value={pnl?.n ?? bets.length + sortedParlays.length}
              sub={`${pnl?.graded ?? 0} graded · ${pending.length + pendingParlays} pending`}
            />
          </div>

          {/* Equity curve */}
          {pnl && pnl.daily.length > 0 && (
            <Card className="mt-4">
              <div className="mb-2 flex items-baseline justify-between">
                <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
                  Cumulative units · your bets
                </span>
                <span
                  className={cls(
                    'font-mono text-xs font-bold',
                    pnl.daily[pnl.daily.length - 1].cum_units >= 0
                      ? 'text-emerald-300'
                      : 'text-rose-400',
                  )}
                >
                  {signed(pnl.daily[pnl.daily.length - 1].cum_units)}u
                </span>
              </div>
              <EquityCurve daily={pnl.daily} clipId="trk-eq" />
            </Card>
          )}

          {!demo && <SlipGuardian />}

          {!demo && <ClvCoach />}

          {/* Parlays — one slip per row, legs inside. */}
          {sortedParlays.length > 0 && (
            <Section
              title={`Parlays · ${sortedParlays.length}`}
              description={
                simple
                  ? 'A parlay only wins if every team wins — one loss loses the slip.'
                  : 'Graded once every leg settles: a lost leg loses the slip, pushed legs drop out of the payout. A parlay counts as one bet in your P&L.'
              }
            >
              <div className="space-y-2">
                {sortedParlays.map((p) => (
                  <ParlayCard
                    key={p.id}
                    parlay={p}
                    busy={busy}
                    simple={simple}
                    onRemove={(id) => void removeParlay(id)}
                  />
                ))}
              </div>
            </Section>
          )}

          {/* Bet log */}
          {sorted.length > 0 && (
            <Section
              title={`Bet log · ${shown.length} of ${sorted.length}`}
              description={
                simple
                  ? 'Pending bets can be removed until kickoff — settled ones are your permanent record.'
                  : 'Bets track at your default stake (SETTINGS → TRACKING). To change one before it grades, delete and re-track — settled rows are permanent.'
              }
              actions={
                <div className="flex flex-wrap items-center gap-1.5">
                  <FilterChips
                    value={status}
                    onChange={setStatus}
                    chips={[
                      { id: 'all', label: 'ALL', count: sorted.length },
                      { id: 'pending', label: 'PENDING', count: pending.length },
                      { id: 'settled', label: 'SETTLED', count: settled.length },
                    ]}
                  />
                  <SportChips value={sportF} onChange={setSportF} counts={sportCounts} total={sorted.length} />
                </div>
              }
            >
              {shown.length === 0 ? (
                <Empty
                  title="No bets match the current filters"
                  message="Widen the status or sport filter above — your log has bets outside this view."
                />
              ) : (
                <Card padded={false} className="overflow-hidden">
                  <div className="overflow-x-auto">
                    <table className="w-full border-collapse text-left">
                      <thead>
                        <tr className="border-b border-white/10 bg-white/[0.02]">
                          <th className={TH}>Status</th>
                          <th className={TH}>Kickoff</th>
                          <th className={TH}>Matchup</th>
                          <th className={TH}>Side</th>
                          <th className={cls(TH, 'text-right')}>Price</th>
                          <th className={TH}>Book</th>
                          <th className={cls(TH, 'text-right')}>Stake</th>
                          <th className={cls(TH, 'text-right')}>Units</th>
                          <th className={TH}>Note</th>
                          <th className={TH} />
                        </tr>
                      </thead>
                      <tbody>
                        {shown.map((b) => (
                          <BetRow key={b.id} bet={b} busy={busy} onRemove={(id) => void remove(id)} />
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Card>
              )}
            </Section>
          )}
        </>
      )}

      <div className="mt-2 text-[9px] uppercase tracking-wider text-krypt-muted/50">
        {simple
          ? 'BOT PICKS is the bot’s whole record · MY BETS only counts what you tracked. Only bet what you can afford to lose. 21+.'
          : 'BOT PICKS = every value pick at flat 1u (the dashboard UNITS number) · MY BETS = your record at your stakes · not betting advice · 21+'}
      </div>
    </Page>
  )
}

import { useEffect, useMemo, useState } from 'react'
import { CloudSun, Loader2, Search, TrendingUp } from 'lucide-react'
import type { BoardMap, GameCard, InjuryReport, Sport, WeatherInfo } from '../types'
import { SPORTS } from '../types'
import type { SharpMoneyReport } from '../api'
import { fetchInjuries, fetchSharpMoney, fetchWeather } from '../api'
import { demoInjuries, demoWeather } from '../demoIntel'
import { fmtKick, matchLabel } from '../cardview'
import SportPills from './SportPills'
import { Badge, Card, Empty, Page, Section } from './common'
import { injurySeverity, InjuryBadge } from './DetailDrawer'
import { cls } from '../utils/format'

const TH = 'px-3 py-2 text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70'
const TD = 'px-3 py-1.5'

type SevFilter = 'all' | 'out' | 'quest' | 'dtd'

const FILTERS: Array<{ id: SevFilter; label: string }> = [
  { id: 'all', label: 'ALL' },
  { id: 'out', label: 'OUT / IR' },
  { id: 'quest', label: 'QUESTIONABLE' },
  { id: 'dtd', label: 'DAY-TO-DAY' },
]

// College (ncaaf / ncaab) has no league-wide injury feed either.
const NO_INJURY_FEED: ReadonlySet<Sport> = new Set<Sport>(['soccer', 'tennis', 'ncaaf', 'ncaab'])
const OUTDOOR: ReadonlySet<Sport> = new Set<Sport>(['nfl', 'ncaaf', 'mlb'])

interface WxRow {
  card: GameCard
  wx: WeatherInfo | null
  loaded: boolean
}

const usd = (v: number) => `$${Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0)}`

/** SHARP MONEY — whale flow + top-100 sports-wallet positioning per slate
 *  game (public Polymarket Data API). Context, not a validated signal: the
 *  research says top wallets often profit on execution, not picks. */
function SharpMoneySection({ report, loading }: { report: SharpMoneyReport | null; loading: boolean }) {
  return (
    <Section
      title="Sharp money · Polymarket whales"
      description="24h whale flow (≥$500 prints), each side's biggest holder, and any top-100 sports-leaderboard wallets positioned. Context next to the model — not a validated signal (top wallets often earn on execution, and market makers hold both sides)."
    >
      {loading ? (
        <div className="flex items-center gap-2 py-8 text-xs uppercase tracking-wider text-krypt-muted">
          <Loader2 size={13} className="animate-spin text-scout-red-bright" />
          Scanning whale flow…
        </div>
      ) : !report || report.games.length === 0 ? (
        <Empty
          title="No sharp-money data for this slate"
          message="Games may be finished (markets close at kickoff), the sport may not be on Polymarket, or the Data API is unreachable."
        />
      ) : (
        <Card padded={false} className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="border-b border-white/10 bg-white/[0.02]">
                  <th className={TH}>Game</th>
                  <th className={cls(TH, 'text-right')}>Whale flow (24h)</th>
                  <th className={TH}>Lean</th>
                  <th className={TH}>Top-100 wallets</th>
                </tr>
              </thead>
              <tbody>
                {report.games.map((g) => {
                  const leanTeam = g.lean === 'home' ? g.home : g.lean === 'away' ? g.away : null
                  return (
                    <tr key={g.game_id} className="border-b border-white/5 last:border-0">
                      <td className={cls(TD, 'text-xs text-white')}>
                        {g.away} <span className="text-krypt-muted/60">@</span> {g.home}
                      </td>
                      <td className={cls(TD, 'text-right font-mono text-[11px]')}>
                        <span className={g.flow_home_usd >= g.flow_away_usd ? 'text-emerald-300' : 'text-krypt-muted'}>
                          H {usd(g.flow_home_usd)}
                        </span>
                        <span className="px-1 text-krypt-muted/40">·</span>
                        <span className={g.flow_away_usd > g.flow_home_usd ? 'text-emerald-300' : 'text-krypt-muted'}>
                          A {usd(g.flow_away_usd)}
                        </span>
                        <span className="ml-2 text-[9px] uppercase tracking-wider text-krypt-muted/50">
                          {g.whale_trades} prints
                        </span>
                      </td>
                      <td className={cls(TD, 'text-xs')}>
                        {leanTeam ? (
                          <span className="inline-flex items-center gap-1 font-semibold text-sky-300">
                            <TrendingUp size={11} />
                            {leanTeam}
                          </span>
                        ) : (
                          <span className="text-krypt-muted/50">—</span>
                        )}
                      </td>
                      <td className={cls(TD, 'font-mono text-[10px] text-krypt-muted')}>
                        {g.sharp_home.length > 0 && <span className="text-emerald-300/90">H: {g.sharp_home.join(', ')}</span>}
                        {g.sharp_home.length > 0 && g.sharp_away.length > 0 && <span className="px-1 text-krypt-muted/40">·</span>}
                        {g.sharp_away.length > 0 && <span className="text-amber-300/90">A: {g.sharp_away.join(', ')}</span>}
                        {g.sharp_home.length === 0 && g.sharp_away.length === 0 && '—'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </Section>
  )
}

interface Props {
  boards: BoardMap
  demo: boolean
  apiUp: boolean | null
}

/** INTEL — league intelligence browser: injuries (searchable, filterable)
 *  and weather for today's outdoor slates. */
export default function IntelPage({ boards, demo, apiUp }: Props) {
  const [sport, setSport] = useState<Sport>('nba')
  const [rows, setRows] = useState<InjuryReport[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<SevFilter>('all')
  const [wxRows, setWxRows] = useState<WxRow[]>([])
  const [sharp, setSharp] = useState<SharpMoneyReport | null>(null)
  const [sharpLoading, setSharpLoading] = useState(false)

  // Sharp-money scan for the selected sport (live only — no demo fixture).
  useEffect(() => {
    if (demo) {
      setSharp(null)
      setSharpLoading(false)
      return
    }
    let alive = true
    setSharp(null)
    setSharpLoading(true)
    void fetchSharpMoney(sport).then((r) => {
      if (!alive) return
      setSharp(r)
      setSharpLoading(false)
    })
    return () => {
      alive = false
    }
  }, [sport, demo])

  const counts = useMemo(() => {
    const c: Partial<Record<Sport, number>> = {}
    for (const s of SPORTS) c[s] = boards[s]?.length ?? 0
    return c
  }, [boards])

  // Injury feed for the selected sport.
  useEffect(() => {
    setQuery('')
    setFilter('all')
    if (NO_INJURY_FEED.has(sport)) {
      setRows([])
      setLoading(false)
      return
    }
    if (demo) {
      const seen = new Set<string>()
      const all: InjuryReport[] = []
      for (const card of boards[sport] ?? []) {
        for (const r of demoInjuries(card)) {
          const k = `${r.team}:${r.player}`
          if (!seen.has(k)) {
            seen.add(k)
            all.push(r)
          }
        }
      }
      setRows(all)
      setLoading(false)
      return
    }
    let alive = true
    setLoading(true)
    setRows(null)
    void fetchInjuries(sport).then((r) => {
      if (!alive) return
      setRows(r)
      setLoading(false)
    })
    return () => {
      alive = false
    }
  }, [sport, demo, boards])

  // Weather for today's outdoor board (NFL / MLB only).
  useEffect(() => {
    if (!OUTDOOR.has(sport)) {
      setWxRows([])
      return
    }
    const cards = [...(boards[sport] ?? [])].sort(
      (a, b) => new Date(a.game.start_time).getTime() - new Date(b.game.start_time).getTime(),
    )
    if (demo) {
      setWxRows(cards.map((card) => ({ card, wx: demoWeather(card), loaded: true })))
      return
    }
    let alive = true
    setWxRows(cards.map((card) => ({ card, wx: null, loaded: false })))
    for (const card of cards) {
      void fetchWeather(card.game.id).then((wx) => {
        if (!alive) return
        setWxRows((prev) =>
          prev.map((r) => (r.card.game.id === card.game.id ? { ...r, wx, loaded: true } : r)),
        )
      })
    }
    return () => {
      alive = false
    }
  }, [sport, demo, boards])

  const visible = useMemo(() => {
    const all = (rows ?? []).filter((r) => r.status !== 'Active')
    const q = query.trim().toLowerCase()
    const out = all.filter((r) => {
      if (filter !== 'all' && injurySeverity(r.status) !== filter) return false
      if (q && !`${r.team} ${r.player} ${r.position} ${r.detail}`.toLowerCase().includes(q)) return false
      return true
    })
    out.sort((a, b) => a.team.localeCompare(b.team) || a.player.localeCompare(b.player))
    return out
  }, [rows, query, filter])

  const sevCounts = useMemo(() => {
    const all = (rows ?? []).filter((r) => r.status !== 'Active')
    const c = { out: 0, quest: 0, dtd: 0 }
    for (const r of all) c[injurySeverity(r.status)]++
    return c
  }, [rows])

  const noFeed = NO_INJURY_FEED.has(sport)

  return (
    <Page
      title="Intel"
      subtitle="Injury reports, game-day weather, and Polymarket sharp-money flow across the slate."
      actions={
        <>
          {demo && <Badge tone="gradient">DEMO</Badge>}
          {apiUp === false && !demo && <Badge tone="danger">API DOWN</Badge>}
        </>
      }
    >
      <Section>
        <SportPills active={sport} counts={counts} onSelect={setSport} />
      </Section>

      {!demo && <SharpMoneySection report={sharp} loading={sharpLoading} />}

      <Section
        title={`Injuries · ${sport.toUpperCase()}`}
        description={
          noFeed
            ? undefined
            : `${sevCounts.out} out/IR · ${sevCounts.quest} questionable · ${sevCounts.dtd} day-to-day`
        }
        actions={
          !noFeed ? (
            <div className="flex items-center gap-1.5">
              {FILTERS.map((f) => (
                <button
                  key={f.id}
                  onClick={() => setFilter(f.id)}
                  className={cls(
                    'rounded-lg border px-2 py-1 text-[9px] font-bold tracking-widest transition',
                    filter === f.id
                      ? 'border-scout-red/40 bg-scout-red/10 text-scout-red-bright'
                      : 'border-white/10 bg-white/[0.03] text-krypt-muted hover:border-white/20 hover:text-white',
                  )}
                >
                  {f.label}
                </button>
              ))}
            </div>
          ) : undefined
        }
      >
        {noFeed ? (
          <Empty
            title={`No injury feed for ${sport.toUpperCase()}`}
            message="Injury reports cover NBA, WNBA, NFL, NHL and MLB. Soccer, tennis and college (NCAAF / NCAAB) have no league-wide feed."
          />
        ) : (
          <Card padded={false} className="overflow-hidden">
            <div className="flex items-center gap-2 border-b border-white/10 bg-white/[0.02] px-3 py-2">
              <Search size={12} className="shrink-0 text-krypt-muted/60" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search team, player, position, ailment…"
                className="w-full bg-transparent text-xs text-white placeholder:text-krypt-muted/50 focus:outline-none"
              />
              <span className="shrink-0 font-mono text-[10px] text-krypt-muted/50">
                {visible.length} rows
              </span>
            </div>
            {loading || rows === null ? (
              <div className="flex items-center gap-2 px-4 py-10 text-[10px] uppercase tracking-wider text-krypt-muted">
                <Loader2 size={13} className="animate-spin text-scout-red-bright" />
                {rows === null && !loading ? 'Injury feed unavailable' : 'Fetching injury feed…'}
              </div>
            ) : visible.length === 0 ? (
              <div className="px-4 py-10 text-center text-[10px] uppercase tracking-wider text-krypt-muted/60">
                No injuries match the current filter
              </div>
            ) : (
              <div className="max-h-[520px] overflow-y-auto">
                <table className="w-full border-collapse text-left">
                  <thead className="sticky top-0 bg-krypt-panel">
                    <tr className="border-b border-white/10">
                      <th className={TH}>Team</th>
                      <th className={TH}>Player</th>
                      <th className={TH}>Pos</th>
                      <th className={TH}>Status</th>
                      <th className={TH}>Detail</th>
                      <th className={cls(TH, 'text-right')}>Updated</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.slice(0, 250).map((r, i) => (
                      <tr
                        key={`${r.team}-${r.player}-${i}`}
                        className="border-b border-white/5 transition last:border-0 hover:bg-white/[0.03]"
                      >
                        <td className={cls(TD, 'text-[10px] font-semibold uppercase tracking-wider text-krypt-muted')}>
                          {r.team}
                        </td>
                        <td className={cls(TD, 'text-xs font-semibold text-white/90')}>{r.player}</td>
                        <td className={cls(TD, 'font-mono text-[10px] text-krypt-muted')}>{r.position}</td>
                        <td className={TD}>
                          <InjuryBadge status={r.status} />
                        </td>
                        <td className={cls(TD, 'max-w-[280px] truncate text-[11px] text-krypt-muted')} title={r.comment}>
                          {r.detail}
                        </td>
                        <td className={cls(TD, 'text-right font-mono text-[10px] text-krypt-muted/60')}>
                          {r.date}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {visible.length > 250 && (
                  <div className="px-3 py-2 text-[9px] uppercase tracking-wider text-krypt-muted/50">
                    +{visible.length - 250} more — refine the search
                  </div>
                )}
              </div>
            )}
          </Card>
        )}
      </Section>

      {OUTDOOR.has(sport) && (
        <Section
          title="Weather · today's outdoor slate"
          description="Forecast at kickoff for every game on the board — domes flagged."
        >
          {wxRows.length === 0 ? (
            <Empty
              title="No games on the board"
              message="Weather chips appear for each scheduled outdoor game once the slate loads."
            />
          ) : (
            <div className="grid gap-2 [grid-template-columns:repeat(auto-fill,minmax(300px,1fr))]">
              {wxRows.map(({ card, wx, loaded }) => {
                const kick = fmtKick(card.game.start_time)
                return (
                  <Card key={card.game.id} padded={false} className="px-3 py-2.5">
                    <div className="flex items-center gap-2">
                      <CloudSun size={13} className="shrink-0 text-scout-red-bright" />
                      <span className="min-w-0 flex-1 truncate text-xs font-semibold text-white/90">
                        {matchLabel(card)}
                      </span>
                      <span className="shrink-0 font-mono text-[10px] text-krypt-muted/60">
                        {kick.date} {kick.time}
                      </span>
                    </div>
                    <div className="mt-1.5 flex items-center gap-2 pl-[21px]">
                      {!loaded ? (
                        <span className="text-[10px] uppercase tracking-wider text-krypt-muted/50">
                          Fetching…
                        </span>
                      ) : wx === null ? (
                        <span className="text-[10px] uppercase tracking-wider text-krypt-muted/50">
                          No weather data
                        </span>
                      ) : (
                        <>
                          {wx.is_dome && <Badge tone="neutral">{wx.retractable ? 'ROOF' : 'DOME'}</Badge>}
                          <span className="truncate font-mono text-[10px] uppercase text-white/80">
                            {wx.summary}
                          </span>
                        </>
                      )}
                    </div>
                  </Card>
                )
              })}
            </div>
          )}
        </Section>
      )}
    </Page>
  )
}

import { useMemo, useState } from 'react'
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react'
import type { BoardMap, Edge, GameCard, Sport } from '../types'
import { SPORTS, SPORT_TAG } from '../types'
import { bookAbbrev, fmtAmerican, fmtPct } from '../oddsmath'
import { edgeSideName, fmtKick, matchLabel } from '../cardview'
import { NO_VALUE_SPORTS } from '../picks'
import BookLink from './BookLink'
import { Badge, Card, Empty, Page, Section, StatTile } from './common'
import { cls } from '../utils/format'

interface Entry {
  edge: Edge
  card: GameCard
}

type SortKey = 'ev' | 'kelly' | 'price' | 'kick'

const TH = 'px-3 py-2 text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70'
const TD = 'px-3 py-1.5 align-middle'

function SortHeader({
  label,
  col,
  sort,
  dir,
  onSort,
  right,
  help,
}: {
  label: string
  col: SortKey
  sort: SortKey
  dir: 1 | -1
  onSort: (k: SortKey) => void
  right?: boolean
  help?: string
}) {
  const active = sort === col
  return (
    <th className={cls(TH, right && 'text-right')}>
      <button
        title={help}
        onClick={() => onSort(col)}
        className={cls(
          'inline-flex items-center gap-1 uppercase tracking-widest transition hover:text-white',
          active ? 'text-scout-red-bright' : 'text-krypt-muted/70',
        )}
      >
        {label}
        {active ? (dir === -1 ? <ArrowDown size={9} /> : <ArrowUp size={9} />) : <ArrowUpDown size={9} className="opacity-40" />}
      </button>
    </th>
  )
}

/** Tiny model-vs-market dual bar. */
function DualBar({ model, market }: { model: number; market: number }) {
  return (
    <div className="flex items-center gap-2">
      <div className="w-16 shrink-0 space-y-[3px]">
        <div className="h-[3px] overflow-hidden rounded-full bg-white/[0.06]">
          <div className="h-full rounded-full bg-emerald-400/80" style={{ width: `${model * 100}%` }} />
        </div>
        <div className="h-[3px] overflow-hidden rounded-full bg-white/[0.06]">
          <div className="h-full rounded-full bg-white/30" style={{ width: `${market * 100}%` }} />
        </div>
      </div>
      <span className="font-mono text-[10px] leading-none">
        <span className="text-emerald-300">{fmtPct(model)}</span>
        <span className="text-krypt-muted/40"> / </span>
        <span className="text-krypt-muted">{fmtPct(market)}</span>
      </span>
    </div>
  )
}

interface Props {
  boards: BoardMap
  demo: boolean
  apiUp: boolean | null
  onJump: (sport: Sport, gameId: string) => void
}

/** VALUE — cross-sport edge scanner: stat tiles + dense sortable edge table. */
export default function ValuePage({ boards, demo, apiUp, onJump }: Props) {
  const [sort, setSort] = useState<SortKey>('ev')
  const [dir, setDir] = useState<1 | -1>(-1)

  const entries = useMemo<Entry[]>(() => {
    const all: Entry[] = []
    for (const sport of SPORTS) {
      // Negative-alpha sports never make the scanner — their "edges" are the
      // model being wrong, not the market (same rule as pick derivation).
      if (NO_VALUE_SPORTS.has(sport)) continue
      for (const card of boards[sport] ?? []) {
        for (const edge of card.edges) all.push({ edge, card })
      }
    }
    return all
  }, [boards])

  const sorted = useMemo(() => {
    const val = (e: Entry): number => {
      switch (sort) {
        case 'ev':
          return e.edge.ev_per_dollar
        case 'kelly':
          return e.edge.kelly_fraction
        case 'price':
          return e.edge.price
        case 'kick':
          return new Date(e.card.game.start_time).getTime()
      }
    }
    return [...entries].sort((a, b) => (val(a) - val(b)) * dir)
  }, [entries, sort, dir])

  const onSort = (k: SortKey) => {
    if (k === sort) setDir((d) => (d === -1 ? 1 : -1))
    else {
      setSort(k)
      setDir(k === 'kick' ? 1 : -1)
    }
  }

  const stats = useMemo(() => {
    if (entries.length === 0) return { n: 0, avg: 0, top: 0, sports: 0 }
    const evs = entries.map((e) => e.edge.ev_per_dollar)
    return {
      n: entries.length,
      avg: evs.reduce((a, b) => a + b, 0) / evs.length,
      top: Math.max(...evs),
      sports: new Set(entries.map((e) => e.edge.sport)).size,
    }
  }, [entries])

  return (
    <Page
      title="Value"
      subtitle="Every positive-EV edge across all nine boards — model vs vig-free market."
      actions={
        <>
          {demo && <Badge tone="gradient">DEMO</Badge>}
          {apiUp === false && !demo && <Badge tone="danger">API DOWN</Badge>}
        </>
      }
    >
      <Section>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile label="Total edges" value={stats.n} sub="across all books" />
          <StatTile
            label="Avg EV"
            value={stats.n ? `+${fmtPct(stats.avg)}%` : '—'}
            tone={stats.n ? 'success' : 'neutral'}
            sub="per $1 staked"
          />
          <StatTile
            label="Top EV"
            value={stats.n ? `+${fmtPct(stats.top)}%` : '—'}
            tone={stats.n ? 'success' : 'neutral'}
            sub="best single edge"
          />
          <StatTile label="Sports active" value={`${stats.sports}/7`} sub="with ≥1 edge" />
        </div>
      </Section>

      <Section
        title="Edge scanner"
        description="Sorted by EV — click a row to jump to the game card on its board."
      >
        {sorted.length === 0 ? (
          <Empty
            title="No positive-EV edges on the board"
            message="Edges appear when a book's price beats the model's fair line after removing the vig. Check back after the next odds poll — or enable demo mode."
          />
        ) : (
          <Card padded={false} className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-left">
                <thead>
                  <tr className="border-b border-white/10 bg-white/[0.02]">
                    <th className={TH}>#</th>
                    <th className={TH}>Sport</th>
                    <th className={TH}>Matchup</th>
                    <th className={TH}>Side</th>
                    <th className={TH}>Book</th>
                    <SortHeader label="Price" col="price" sort={sort} dir={dir} onSort={onSort} right />
                    <th className={TH} title="Model win probability vs market-implied probability (vig removed)">
                      Model / Mkt
                    </th>
                    <SortHeader
                      label="EV"
                      col="ev"
                      sort={sort}
                      dir={dir}
                      onSort={onSort}
                      right
                      help="EV = expected value: average profit per $1 bet if the model probability is right"
                    />
                    <SortHeader
                      label="Kelly"
                      col="kelly"
                      sort={sort}
                      dir={dir}
                      onSort={onSort}
                      right
                      help="Kelly = bankroll-sizing formula (full-Kelly stake fraction)"
                    />
                    <SortHeader label="Kickoff" col="kick" sort={sort} dir={dir} onSort={onSort} right />
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((e, i) => {
                    const kick = fmtKick(e.card.game.start_time)
                    return (
                      <tr
                        key={`${e.edge.game_id}-${e.edge.side}-${e.edge.book}`}
                        onClick={() => onJump(e.edge.sport, e.edge.game_id)}
                        className="cursor-pointer border-b border-white/5 transition last:border-0 hover:bg-white/[0.04]"
                        title="Jump to this game's board card"
                      >
                        <td className={cls(TD, 'font-mono text-[10px] text-krypt-muted/40')}>
                          {String(i + 1).padStart(2, '0')}
                        </td>
                        <td className={TD}>
                          <span className="rounded border border-white/10 bg-white/5 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest text-krypt-muted">
                            {SPORT_TAG[e.edge.sport]}
                          </span>
                        </td>
                        <td className={cls(TD, 'max-w-[220px] truncate text-xs font-semibold text-white/90')}>
                          {matchLabel(e.card)}
                        </td>
                        <td className={cls(TD, 'text-xs font-semibold text-white')}>
                          {edgeSideName(e.edge, e.card)}
                        </td>
                        <td className={cls(TD, 'font-mono text-[10px] text-krypt-muted')}>
                          <BookLink book={e.edge.book} className="hover:text-white">
                            {bookAbbrev(e.edge.book)}
                          </BookLink>
                        </td>
                        <td className={cls(TD, 'text-right font-mono text-[11px] text-white/85')}>
                          {fmtAmerican(e.edge.price)}
                        </td>
                        <td className={TD}>
                          <DualBar model={e.edge.model_prob} market={e.edge.market_prob} />
                        </td>
                        <td className={cls(TD, 'text-right font-mono text-[11px] font-semibold text-emerald-300')}>
                          +{fmtPct(e.edge.ev_per_dollar)}%
                        </td>
                        <td className={cls(TD, 'text-right font-mono text-[10px] text-emerald-300/70')}>
                          {fmtPct(e.edge.kelly_fraction)}%
                        </td>
                        <td className={cls(TD, 'whitespace-nowrap text-right font-mono text-[10px] text-krypt-muted')}>
                          {kick.date} {kick.time}
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
    </Page>
  )
}

/**
 * BOOST EVALUATOR — score an odds boost or a profit-boost token against the
 * sharp fair price (GameCard.fair: Kalshi, then Polymarket, then a flagged
 * sportsbook fallback). Pick a game and a side, type what the book is
 * offering, get the effective price, EV per $1, quarter-Kelly and a
 * PLAY / MARGINAL / PASS verdict. TRACK logs it to MY BETS at the effective
 * price so it grades like any other bet.
 *
 * The model is not consulted anywhere on this panel — on purpose.
 */
import { useEffect, useMemo, useState } from 'react'
import { Check, Zap } from 'lucide-react'
import type { BoardMap, GameCard } from '../types'
import { SPORTS, SPORT_TAG } from '../types'
import { bestPrice, bookAbbrev, fmtAmerican, fmtPct } from '../oddsmath'
import { byStartAsc, fmtKick, matchLabel, sideViews, type SideView } from '../cardview'
import { evaluateBoost, parseAmerican, type BoostKind } from '../boost'
import { useMyBets } from '../mybets'
import { defaultStake } from '../prefs'
import { Card, PrimaryButton, StripBtn } from './common'
import { cls } from '../utils/format'

interface Props {
  boards: BoardMap
  demo: boolean
}

const INPUT =
  'w-full rounded-lg border border-white/10 bg-black/30 px-2.5 py-1.5 font-mono text-[11px] text-white outline-none transition focus:border-white/25 placeholder:text-krypt-muted/40'
const LABEL = 'mb-1 block text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70'

function fairFor(card: GameCard, side: SideView['key']): number | null {
  const fair = card.fair
  if (!fair) return null
  if (side === 'home') return fair.home
  if (side === 'away') return fair.away
  return fair.draw ?? null
}

export default function BoostEvaluator({ boards, demo }: Props) {
  const cards = useMemo(
    () =>
      SPORTS.flatMap((s) => boards[s] ?? [])
        .filter((c) => !c.game.completed && c.prediction)
        .sort(byStartAsc),
    [boards],
  )
  const [gameId, setGameId] = useState('')
  const card = cards.find((c) => c.game.id === gameId) ?? null
  const sides = card ? sideViews(card) : []
  const [sideKey, setSideKey] = useState<SideView['key']>('home')
  const side = sides.find((s) => s.key === sideKey) ?? sides[0] ?? null
  const [kind, setKind] = useState<BoostKind>('odds')
  const [boosted, setBoosted] = useState('')
  const [base, setBase] = useState('')
  const [pct, setPct] = useState('50')
  const [stake, setStake] = useState(String(defaultStake()))
  const [maxStake, setMaxStake] = useState('')
  const [book, setBook] = useState('draftkings')
  const [tracked, setTracked] = useState<string | null>(null)
  const { track, busy } = useMyBets(demo)

  // Default the base price to the chosen book's own line for the side, else
  // the best available price — what the token would actually be applied to.
  useEffect(() => {
    if (!card || !side) return
    const own = card.odds.find((q) => q.book === book)?.[side.mlKey]
    const bp = own ?? bestPrice(card.odds, side.mlKey)?.price ?? null
    setBase(bp !== null && bp !== undefined ? String(bp) : '')
    setTracked(null)
  }, [card, side, book])

  const fairProb = card && side ? fairFor(card, side.key) : null
  const evaluation =
    fairProb !== null
      ? evaluateBoost({
          fairProb,
          kind,
          boostedPrice: parseAmerican(boosted),
          basePrice: parseAmerican(base),
          profitBoostPct: Number(pct),
          stake: Number(stake) || null,
          maxStake: Number(maxStake) || null,
        })
      : null

  const verdictTone =
    evaluation?.verdict === 'PLAY'
      ? 'border-emerald-400/30 bg-emerald-500/10 text-emerald-200'
      : evaluation?.verdict === 'MARGINAL'
        ? 'border-amber-400/30 bg-amber-500/10 text-amber-200'
        : 'border-rose-400/30 bg-rose-500/10 text-rose-200'

  const onTrack = async () => {
    if (!card || !side || !evaluation) return
    const label =
      kind === 'odds'
        ? `odds boost ${fmtAmerican(evaluation.effectiveAmerican)}`
        : `${pct}% profit boost on ${fmtAmerican(parseAmerican(base) as number)}`
    const ok = await track(
      {
        game_id: card.game.id,
        side: side.key,
        price: evaluation.effectiveAmerican,
        book: book.trim() || null,
        stake_units: evaluation.stake ?? undefined,
        note: `BOOST · ${label} · fair ${fmtAmerican(evaluation.fairAmerican)} @ ${card.fair?.source ?? '?'}`,
      },
      card,
    )
    if (ok) setTracked(card.game.id + side.key)
  }

  return (
    <Card className="p-4">
      <div className="grid gap-3 lg:grid-cols-[1.4fr_1fr]">
        {/* ---------------------------------------------------------- inputs */}
        <div className="space-y-3">
          <div>
            <label className={LABEL}>Game</label>
            <select className={INPUT} value={gameId} onChange={(e) => setGameId(e.target.value)}>
              <option value="">Pick a game…</option>
              {cards.map((c) => {
                const k = fmtKick(c.game.start_time)
                return (
                  <option key={c.game.id} value={c.game.id}>
                    {SPORT_TAG[c.game.sport]} · {matchLabel(c)} · {k.date} {k.time}
                    {c.fair ? '' : ' · (no fair yet)'}
                  </option>
                )
              })}
            </select>
          </div>

          {card && (
            <div className="flex flex-wrap gap-1.5">
              {sides.map((s) => (
                <StripBtn key={s.key} active={side?.key === s.key} onClick={() => setSideKey(s.key)} title={s.name}>
                  {s.tag} · {s.name}
                </StripBtn>
              ))}
            </div>
          )}

          <div className="flex flex-wrap gap-1.5">
            <StripBtn active={kind === 'odds'} onClick={() => setKind('odds')} title="The book shows a boosted price">
              ODDS BOOST
            </StripBtn>
            <StripBtn active={kind === 'profit'} onClick={() => setKind('profit')} title="A % token applied to a normal price's winnings">
              PROFIT BOOST
            </StripBtn>
          </div>

          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {kind === 'odds' ? (
              <div>
                <label className={LABEL}>Boosted price</label>
                <input className={INPUT} value={boosted} onChange={(e) => setBoosted(e.target.value)} placeholder="+200" inputMode="numeric" />
              </div>
            ) : (
              <>
                <div>
                  <label className={LABEL}>Base price</label>
                  <input className={INPUT} value={base} onChange={(e) => setBase(e.target.value)} placeholder="+150" inputMode="numeric" />
                </div>
                <div>
                  <label className={LABEL}>Boost %</label>
                  <input className={INPUT} value={pct} onChange={(e) => setPct(e.target.value)} placeholder="50" inputMode="numeric" />
                </div>
              </>
            )}
            <div>
              <label className={LABEL}>Book</label>
              <input className={INPUT} value={book} onChange={(e) => setBook(e.target.value)} placeholder="draftkings" />
            </div>
            <div>
              <label className={LABEL}>Stake</label>
              <input className={INPUT} value={stake} onChange={(e) => setStake(e.target.value)} inputMode="decimal" />
            </div>
            <div>
              <label className={LABEL}>Promo max stake</label>
              <input className={INPUT} value={maxStake} onChange={(e) => setMaxStake(e.target.value)} placeholder="optional" inputMode="decimal" />
            </div>
          </div>
        </div>

        {/* ---------------------------------------------------------- verdict */}
        <div className="flex flex-col justify-between rounded-xl border border-white/10 bg-black/20 p-3">
          {!card ? (
            <p className="text-xs text-krypt-muted">Pick a game to price a boost against the exchanges.</p>
          ) : !card.fair || fairProb === null ? (
            <p className="text-xs text-krypt-muted">
              No full-market price for this game yet. Fair needs both sides quoted by an exchange or a book; check back closer to kickoff.
            </p>
          ) : (
            <>
              <div>
                <div className="flex items-baseline justify-between">
                  <span className="text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70">Fair · {bookAbbrev(card.fair.source)}</span>
                  <span className="font-mono text-sm font-bold text-white">
                    {fmtPct(fairProb)}% · {fmtAmerican(evaluation?.fairAmerican ?? 0)}
                  </span>
                </div>
                {!card.fair.sharp && (
                  <p className="mt-1 text-[10px] leading-snug text-amber-300/80">
                    No exchange quote yet — this fair is {bookAbbrev(card.fair.source)}'s own devigged line, i.e. the book you are trying to beat. Treat PLAY as provisional.
                  </p>
                )}
                {Object.keys(card.fair.sources).length > 1 && card.fair.draw == null && side && (
                  <p className="mt-1 font-mono text-[9px] uppercase tracking-wider text-krypt-muted/60">
                    {Object.entries(card.fair.sources)
                      .map(([b, homeP]) => `${bookAbbrev(b)} ${fmtPct(side.key === 'home' ? homeP : 1 - homeP)}%`)
                      .join(' · ')}
                  </p>
                )}
              </div>

              {evaluation ? (
                <div className="mt-3 space-y-2">
                  <div className={cls('rounded-lg border px-2.5 py-2', verdictTone)}>
                    <div className="flex items-center justify-between">
                      <span className="flex items-center gap-1 text-[10px] font-bold uppercase tracking-[0.2em]">
                        <Zap size={12} /> {evaluation.verdict}
                      </span>
                      <span className="font-mono text-sm font-bold">
                        {evaluation.evPerDollar >= 0 ? '+' : ''}
                        {fmtPct(evaluation.evPerDollar)}% per $1
                      </span>
                    </div>
                    <div className="mt-1 font-mono text-[10px] opacity-80">
                      Effective {fmtAmerican(evaluation.effectiveAmerican)} · implies {fmtPct(evaluation.impliedProb)}% · edge{' '}
                      {evaluation.edgePts >= 0 ? '+' : ''}
                      {evaluation.edgePts.toFixed(1)} pts · ¼ Kelly {fmtPct(evaluation.quarterKelly, 2)}%
                      {evaluation.evAtStake !== null && ` · EV ${evaluation.evAtStake >= 0 ? '+' : ''}${evaluation.evAtStake.toFixed(2)} on ${evaluation.stake}`}
                    </div>
                  </div>
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[10px] leading-snug text-krypt-muted">
                      PLAY needs +2%: the reference is only good to about a point.
                    </span>
                    <PrimaryButton onClick={onTrack} disabled={busy || tracked === card.game.id + (side?.key ?? '')} className="shrink-0 !px-3 !py-1.5 !text-xs">
                      {tracked === card.game.id + (side?.key ?? '') ? (
                        <>
                          <Check size={14} /> TRACKED
                        </>
                      ) : (
                        'TRACK'
                      )}
                    </PrimaryButton>
                  </div>
                </div>
              ) : (
                <p className="mt-3 text-xs text-krypt-muted">
                  {kind === 'odds' ? 'Type the boosted price (e.g. +200).' : 'Type the base price and the boost percentage.'}
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </Card>
  )
}

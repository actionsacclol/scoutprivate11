/**
 * TOUR — skippable coach-marks that play right after first-run onboarding.
 *
 * Arms itself the moment prefs.onboarded flips true (reactive via
 * usePrefsVersion) and prefs.toured isn't set; finishing OR skipping sets
 * prefs.toured, resetTour() in prefs.ts re-arms it (ABOUT page button).
 *
 * Opens with a "want a tour?" card (the explicit skip), then walks two kinds
 * of steps: TAB steps spotlight a sidebar button ([data-tour="<PageId>"] —
 * see Sidebar.tsx), and PICK-CARD steps navigate to PICKS and spotlight the
 * parts of the first pick card ([data-tour="pick-*"] — see PicksPage.tsx):
 * grade tag, risk tag, price, the plain-English why, the numbers, bet size,
 * line shop and TRACK. Copy rule: short words, zero jargon — assume the
 * reader has never placed a bet. Elements inside a page mount a frame or two
 * after navigation, so targets are polled over rAF; a target that never
 * appears (e.g. an empty pick board) is skipped in the direction of travel.
 * The dimmer blocks stray clicks so the tour can't be wandered out of;
 * NEXT/BACK/SKIP are the only controls (Esc deliberately does nothing,
 * matching Onboarding). Safe in both UI modes and in demo.
 */
import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { ArrowLeft, ArrowRight, Compass } from 'lucide-react'
import { loadPrefs, savePrefs, usePrefsVersion } from '../prefs'
import { GhostButton, PrimaryButton } from './common'
import { cls } from '../utils/format'
import type { PageId } from './Sidebar'

interface TourStep {
  /** Tab to navigate to before spotlighting. */
  page: PageId
  /** data-tour key of the element to spotlight (first match wins). */
  target: string
  label: string
  title: string
  copy: string
}

const STEPS: TourStep[] = [
  {
    page: 'dashboard',
    target: 'dashboard',
    label: 'DASHBOARD',
    title: 'Home base',
    copy: 'The big picture — what games are on and how the picks are doing. If you ever feel lost, come back here.',
  },
  {
    page: 'today',
    target: 'today',
    label: 'TODAY',
    title: "Today's games",
    copy: "Every game playing today, all sports, one list. Nothing to do here — it's just a quick look at the day.",
  },
  {
    page: 'picks',
    target: 'picks',
    label: 'PICKS',
    title: 'The main tab',
    copy: "One pick per game — what to bet, where, and how much. Let's read one card together, piece by piece.",
  },
  {
    page: 'picks',
    target: 'pick-tier',
    label: 'PICK CARD',
    title: 'How good is this bet?',
    copy: 'The tags grade the bet. STRONG — the payout is clearly bigger than it should be; our best bets. LEAN — a small edge. FAVORITE — we like the team, but no book is paying extra, so skipping it costs you nothing.',
  },
  {
    page: 'picks',
    target: 'pick-risk',
    label: 'PICK CARD',
    title: 'Low risk or high risk?',
    copy: "Risk is how often a bet like this loses. LOW RISK wins often but pays little. HIGH RISK loses most of the time but pays big when it hits. Neither is better — just know which one you're holding.",
  },
  {
    page: 'picks',
    target: 'pick-headline',
    label: 'PICK CARD',
    title: 'The bet and the price',
    copy: 'The team, the price, and the book paying the most. Minus price (-150): bet $150 to win $100. Plus price (+150): bet $100 to win $150. BET NOW opens that book.',
  },
  {
    page: 'picks',
    target: 'pick-sentence',
    label: 'PICK CARD',
    title: "Why it's a good deal",
    copy: 'We compare our win chance to what the market believes. When our number is higher, the payout is bigger than the risk — that gap is the only reason to ever bet.',
  },
  {
    page: 'picks',
    target: 'pick-numbers',
    label: 'PICK CARD',
    title: 'The numbers behind it',
    copy: "Green bar: our win chance. Gray bar: the market's. EV is your average profit per $1 — +5% EV means about 5 cents per dollar over many bets like this.",
  },
  {
    page: 'picks',
    target: 'pick-stake',
    label: 'PICK CARD',
    title: 'How much to bet',
    copy: 'Spelled out right here — a small slice of your bankroll (money set aside just for betting). Stick to it. Going big on one game is how people go broke.',
  },
  {
    page: 'picks',
    target: 'pick-shop',
    label: 'PICK CARD',
    title: 'Always take the best price',
    copy: 'The same bet at every book — green is the biggest payout. Below the list: the worst price still worth taking. Anything worse turns a good bet into a bad one.',
  },
  {
    page: 'picks',
    target: 'pick-track',
    label: 'PICK CARD',
    title: 'Track it',
    copy: 'Hit TRACK BET and Scout saves the bet, then grades it when the game ends — win or lose. No spreadsheet needed.',
  },
  {
    page: 'boards',
    target: 'boards',
    label: 'BOARDS',
    title: 'All the numbers',
    copy: 'Every game with every price we can find. Click any game to see the full breakdown.',
  },
  {
    page: 'parlay',
    target: 'parlay',
    label: 'PARLAY',
    title: 'Combo bets',
    copy: "Stack picks into one big bet. Scout does the math and tells you straight if it's a bad deal. It usually is.",
  },
  {
    page: 'tracker',
    target: 'tracker',
    label: 'TRACKER',
    title: 'Your bets',
    copy: 'Every bet you tracked lands here. When the game ends, Scout grades it for you — win or lose.',
  },
  {
    page: 'autobet',
    target: 'autobet',
    label: 'AUTO-BET',
    title: 'The robot',
    copy: 'Connect Polymarket and Scout can place bets by itself, with limits you set. It stays OFF until you turn it on.',
  },
  {
    page: 'perf',
    target: 'perf',
    label: 'PERFORMANCE',
    title: 'Our report card',
    copy: "Every pick Scout ever made, graded. Wins AND losses — nothing gets hidden. Check it whenever you're not sure about us.",
  },
  {
    page: 'settings',
    target: 'settings',
    label: 'SETTINGS',
    title: 'Make it yours',
    copy: "Books, stake size, data, everything. That's the tour — go check today's picks!",
  },
]

/** Spotlight padding + tooltip card geometry. */
const PAD = 4
const CARD_W = 340
const CARD_EST_H = 270
/** rAF frames to wait for a step's target before skipping the step. */
const RETRY_FRAMES = 24

function targetEl(target: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-tour="${target}"]`)
}

export default function TourGuide({ onNav }: { onNav: (page: PageId) => void }) {
  usePrefsVersion()
  const prefs = loadPrefs()
  const visible = prefs.onboarded && !prefs.toured

  // -1 = the "want a tour?" invite card; 0.. = spotlight steps.
  const [step, setStep] = useState(-1)
  const [rect, setRect] = useState<DOMRect | null>(null)
  // Direction of travel — a missing target skips onward, not back at you.
  const dirRef = useRef(1)

  useEffect(() => {
    if (visible) {
      setStep(-1)
      dirRef.current = 1
    }
  }, [visible])

  const done = (landOnPicks: boolean) => {
    savePrefs({ ...loadPrefs(), toured: true })
    if (landOnPicks) onNav('picks')
  }

  // Navigate to the step's tab, then poll for its target over rAF — page
  // content (e.g. the first pick card) mounts a frame or two after nav.
  // A target that never shows up (empty pick board) skips the step in the
  // current direction; running off the end finishes the tour.
  useEffect(() => {
    if (!visible || step < 0) {
      setRect(null)
      return
    }
    const { page, target } = STEPS[step]
    onNav(page)
    setRect(null)
    let raf = 0
    let tries = 0
    const attempt = () => {
      const el = targetEl(target)
      if (el) {
        el.scrollIntoView({ block: 'nearest' })
        setRect(el.getBoundingClientRect())
        return
      }
      if (++tries < RETRY_FRAMES) {
        raf = requestAnimationFrame(attempt)
        return
      }
      // The page is mounted by now (we waited on it), so sibling in-page
      // targets can be checked synchronously — hop over every missing one.
      // Tab steps (target === page) always exist and stop the scan.
      let next = step + dirRef.current
      while (
        next >= 0 &&
        next < STEPS.length &&
        STEPS[next].target !== STEPS[next].page &&
        !targetEl(STEPS[next].target)
      ) {
        next += dirRef.current
      }
      if (next >= STEPS.length) done(true)
      else setStep(Math.max(0, next))
    }
    attempt()
    const measure = () => {
      const el = targetEl(STEPS[step].target)
      if (el) setRect(el.getBoundingClientRect())
    }
    window.addEventListener('resize', measure)
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', measure)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, step, onNav])

  if (!visible) return null

  const go = (dir: number) => {
    dirRef.current = dir
    setStep((s) => s + dir)
  }

  // Card placement: right of the spotlight when it fits (sidebar buttons),
  // otherwise below it, otherwise above (wide in-page targets) — clamped
  // on-screen either way. While polling (rect null) the card sits centered.
  let cardStyle: CSSProperties = {
    left: '50%',
    top: '50%',
    transform: 'translate(-50%, -50%)',
    width: CARD_W,
  }
  if (rect) {
    const fitsRight = rect.right + 14 + CARD_W <= window.innerWidth - 12
    const left = fitsRight
      ? rect.right + 14
      : Math.min(Math.max(12, rect.left), window.innerWidth - CARD_W - 12)
    const top = fitsRight
      ? Math.max(12, Math.min(rect.top - 10, window.innerHeight - CARD_EST_H - 12))
      : rect.bottom + 14 + CARD_EST_H <= window.innerHeight - 12
        ? rect.bottom + 14
        : Math.max(12, rect.top - CARD_EST_H - 14)
    cardStyle = { left, top, width: CARD_W }
  }

  return (
    <div className="fixed inset-0 z-50">
      {step === -1 ? (
        <div className="flex h-full items-center justify-center bg-krypt-void/95 p-6 backdrop-blur-sm">
          <div className="glass hud w-full max-w-md animate-pop-in rounded-2xl border border-white/10 p-6 shadow-krypt-card">
            <div className="flex items-center gap-2.5">
              <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-krypt-gradient shadow-krypt-glow">
                <Compass size={17} className="text-white" />
              </span>
              <h2 className="text-lg font-bold leading-tight text-white">Want a quick tour?</h2>
            </div>
            <p className="mt-3 text-sm leading-relaxed text-krypt-muted">
              The nine tabs, plus a piece-by-piece read of one pick card — how to spot a good
              bet, how risky it is, and how much to put on. About two minutes.
            </p>
            <div className="mt-5 flex items-center justify-between border-t border-white/5 pt-4">
              <GhostButton onClick={() => done(false)} className="px-3 py-2 text-xs">
                NO THANKS
              </GhostButton>
              <PrimaryButton onClick={() => setStep(0)} className="px-4 py-2 text-xs">
                SHOW ME AROUND
                <ArrowRight size={13} />
              </PrimaryButton>
            </div>
          </div>
        </div>
      ) : (
        <>
          {/* Spotlight: the transparent hole IS this div — the giant shadow
              dims everything else. A pulsing ring marks the target. */}
          {rect && (
            <>
              <div
                className="absolute rounded-lg"
                style={{
                  left: rect.left - PAD,
                  top: rect.top - PAD,
                  width: rect.width + PAD * 2,
                  height: rect.height + PAD * 2,
                  boxShadow: '0 0 0 9999px rgba(3, 6, 12, 0.82)',
                }}
              />
              <div
                className="absolute animate-pulse rounded-lg border-2 border-scout-red shadow-scout-glow"
                style={{
                  left: rect.left - PAD,
                  top: rect.top - PAD,
                  width: rect.width + PAD * 2,
                  height: rect.height + PAD * 2,
                }}
              />
            </>
          )}

          <button
            onClick={() => done(false)}
            className="absolute right-5 top-4 text-[10px] font-semibold uppercase tracking-[0.25em] text-krypt-muted/50 transition hover:text-white"
            title="Skip the tour — replay it anytime from the ABOUT tab"
          >
            Skip tour
          </button>

          <div
            className="glass hud absolute animate-pop-in rounded-2xl border border-white/10 p-5 shadow-krypt-card"
            style={cardStyle}
          >
            <div className="font-mono text-[10px] uppercase tracking-wider text-krypt-muted/60">
              {step + 1} / {STEPS.length} · {STEPS[step].label}
            </div>
            <h3 className="mt-2 text-base font-bold leading-tight text-white">
              {STEPS[step].title}
            </h3>
            <p className="mt-1.5 text-sm leading-relaxed text-krypt-muted">{STEPS[step].copy}</p>

            <div className="mt-4 flex gap-1">
              {STEPS.map((s, i) => (
                <div
                  key={`${s.page}-${s.target}`}
                  className={cls(
                    'h-[3px] flex-1 rounded-full transition-colors',
                    i < step && 'bg-scout-red/40',
                    i === step && 'bg-scout-red shadow-scout-glow',
                    i > step && 'bg-white/10',
                  )}
                />
              ))}
            </div>

            <div className="mt-4 flex items-center justify-between border-t border-white/5 pt-3">
              {step > 0 ? (
                <GhostButton onClick={() => go(-1)} className="px-3 py-1.5 text-xs">
                  <ArrowLeft size={13} />
                  BACK
                </GhostButton>
              ) : (
                <span />
              )}
              {step < STEPS.length - 1 ? (
                <PrimaryButton onClick={() => go(1)} className="px-4 py-1.5 text-xs">
                  NEXT
                  <ArrowRight size={13} />
                </PrimaryButton>
              ) : (
                <PrimaryButton onClick={() => done(true)} className="px-4 py-1.5 text-xs">
                  GO GET THE PICKS
                  <ArrowRight size={13} />
                </PrimaryButton>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

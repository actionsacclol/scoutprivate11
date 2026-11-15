/**
 * First-run onboarding — a 3-step overlay that orients a new user in under
 * 30 seconds:
 *
 *   1. GET POLYMARKET — create an account (referral link) + the video guide.
 *      Polymarket is Scout's sharpest price source AND the only market the
 *      bot can auto-bet, so it's the one signup that unlocks everything.
 *   2. HOW YOU READ — SIMPLE (plain-English picks) vs PRO (full terminal).
 *   3. GET THE PICKS — the daily flow, then ENTER THE TERMINAL.
 *
 * Shown once (prefs.onboarded); re-armable via resetOnboarding() in prefs.ts.
 * Esc deliberately does nothing — SKIP (top-right) is the only early exit.
 * Safe in both modes and in demo (no data dependencies at all).
 */
import { useEffect, useState } from 'react'
import {
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  BarChart3,
  MessageSquare,
  PlayCircle,
  Send,
  Target,
  Terminal,
} from 'lucide-react'
import { openBook } from '../config/referrals'
import { loadPrefs, savePrefs, usePrefsVersion } from '../prefs'
import { loadUiMode, saveUiMode, type UiMode } from '../uiMode'
import { GhostButton, PrimaryButton } from './common'
import { cls, openExternal } from '../utils/format'

/** Start-to-finish setup walkthrough (registering + connecting Scout). */
const GUIDE_URL = 'https://www.youtube.com/watch?v=kmSmzH6Aixs'

const STEP_LABELS = ['Where to bet', 'How you read', 'Get the picks']

/**
 * Set the live UI mode via the SAME mechanism the TitleBar uses: the
 * UiModeContext only exposes the value (App owns the setter and passes it to
 * the TitleBar's ModeSwitch as a prop), so we persist with saveUiMode and
 * then click the TitleBar's own mode button — that runs App's real setUiMode
 * (state update + persist + landing-page correction), exactly like a user
 * click. If the switch isn't in the DOM the persisted value still applies on
 * next launch.
 */
function applyUiMode(mode: UiMode): void {
  saveUiMode(mode)
  const group = document.querySelector('[role="group"][aria-label="Interface mode"]')
  if (!group) return
  for (const btn of Array.from(group.querySelectorAll('button'))) {
    if (btn.textContent?.trim().toLowerCase() === mode) {
      if (btn.getAttribute('aria-pressed') !== 'true') (btn as HTMLButtonElement).click()
      return
    }
  }
}

const MODE_CARDS: Array<{ mode: UiMode; title: string; copy: string; icon: typeof Terminal }> = [
  {
    mode: 'simple',
    title: 'SIMPLE',
    copy: 'Plain-English picks. Just tell me what to bet.',
    icon: MessageSquare,
  },
  {
    mode: 'pro',
    title: 'PRO',
    copy: 'Full terminal — every number, every market.',
    icon: Terminal,
  },
]

const FLOW_ROWS: Array<{ icon: typeof Target; title: string; copy: string }> = [
  {
    icon: Target,
    title: 'Picks every morning',
    copy: 'One clear call per game — best price flagged across every book.',
  },
  {
    icon: BarChart3,
    title: 'Auto-graded record',
    copy: 'Every pick is tracked and graded — the record speaks for itself.',
  },
  {
    icon: Send,
    title: 'Optional auto-post',
    copy: 'Push the daily card to your Discord channels.',
  },
]

export default function Onboarding() {
  // Visibility is derived from prefs (reactive via usePrefsVersion) so the
  // ABOUT page's "replay onboarding" re-opens the flow without a reload.
  usePrefsVersion()
  const visible = !loadPrefs().onboarded
  const [step, setStep] = useState(0)
  const [mode, setMode] = useState<UiMode>(loadUiMode)

  useEffect(() => {
    if (visible) {
      setStep(0)
      setMode(loadUiMode())
    }
  }, [visible])

  if (!visible) return null

  const chooseMode = (m: UiMode) => {
    setMode(m)
    applyUiMode(m)
  }

  const finish = () => {
    // preferredBooks is left as-is (the house default already pins
    // Polymarket first — see prefs.DEFAULT_BOOK_ORDER).
    savePrefs({ ...loadPrefs(), onboarded: true, mode })
  }

  const skip = () => {
    // Deliberately keeps whatever was (or wasn't) chosen so far out of prefs —
    // skipping only marks the flow as seen.
    savePrefs({ ...loadPrefs(), onboarded: true })
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-krypt-void/95 p-6 backdrop-blur-sm">
      <button
        onClick={skip}
        className="absolute right-5 top-4 text-[10px] font-semibold uppercase tracking-[0.25em] text-krypt-muted/50 transition hover:text-white"
        title="Skip setup — everything here can be changed later"
      >
        Skip
      </button>

      <div className="glass hud relative w-full max-w-xl animate-pop-in rounded-2xl border border-white/10 p-6 shadow-krypt-card">
        {/* Header: logo + wordmark + step label */}
        <div className="flex items-center gap-2.5">
          <img
            src="./krypt.png"
            alt=""
            className="h-6 w-6"
            style={{ filter: 'drop-shadow(0 0 8px rgba(225,29,72,0.75))' }}
            onError={(e) => {
              ;(e.target as HTMLImageElement).style.display = 'none'
            }}
          />
          <span className="text-sm font-bold tracking-[0.28em]">
            <span className="text-krypt-gradient">KRYPT</span> SCOUT
          </span>
          <span className="ml-auto font-mono text-[10px] uppercase tracking-wider text-krypt-muted/60">
            {step + 1} / 3 · {STEP_LABELS[step]}
          </span>
        </div>

        {/* Slim progress rail */}
        <div className="mt-4 flex gap-1.5">
          {STEP_LABELS.map((label, i) => (
            <div
              key={label}
              className={cls(
                'h-[3px] flex-1 rounded-full transition-colors',
                i < step && 'bg-scout-red/40',
                i === step && 'bg-scout-red shadow-scout-glow',
                i > step && 'bg-white/10',
              )}
            />
          ))}
        </div>

        {/* Step body */}
        <div className="mt-5 min-h-[280px]">
          {step === 0 && (
            <div className="animate-fade-in">
              <h2 className="text-lg font-bold leading-tight text-white">
                You are set up. Want somewhere to bet?
              </h2>
              <p className="mt-1 text-sm leading-relaxed text-krypt-muted">
                Scout already works — nothing else to install or sign up for. It prices every
                game against the exchanges (Polymarket and Kalshi). If you want to bet there,
                or let auto-bet place for you, a Polymarket account is the quickest way in.
                This step is optional.
              </p>
              <div className="mt-4 space-y-2">
                <button
                  type="button"
                  onClick={() => openBook('polymarket')}
                  title="Opens polymarket.com (referral link)"
                  className="flex w-full items-center gap-3 rounded-2xl border border-scout-red/40 bg-scout-red/10 px-4 py-3 text-left transition hover:bg-scout-red/20"
                >
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-krypt-gradient font-mono text-xs font-bold text-white shadow-krypt-glow">
                    1
                  </span>
                  <span className="min-w-0">
                    <span className="block text-[12px] font-bold uppercase tracking-widest text-white">
                      Create your Polymarket account
                    </span>
                    <span className="mt-0.5 block text-[11px] leading-relaxed text-krypt-muted">
                      polymarket.com · takes about two minutes, email signup works
                    </span>
                  </span>
                  <ArrowUpRight size={15} className="ml-auto shrink-0 text-scout-red-bright" />
                </button>
                <button
                  type="button"
                  onClick={() => openExternal(GUIDE_URL)}
                  title="Opens YouTube"
                  className="flex w-full items-center gap-3 rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-3 text-left transition hover:border-white/25 hover:bg-white/5"
                >
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-white/10 bg-white/5">
                    <PlayCircle size={17} className="text-scout-red-bright" />
                  </span>
                  <span className="min-w-0">
                    <span className="block text-[12px] font-bold uppercase tracking-widest text-white">
                      Watch the setup guide
                    </span>
                    <span className="mt-0.5 block text-[11px] leading-relaxed text-krypt-muted">
                      Registering + connecting Scout, start to finish, on YouTube
                    </span>
                  </span>
                  <ArrowUpRight size={15} className="ml-auto shrink-0 text-krypt-muted" />
                </button>
              </div>
              <p className="mt-3 text-[10px] uppercase tracking-wider text-krypt-muted/60">
                Have an account, or just looking? Hit NEXT. Signup link is a referral · 18+/21+ where legal.
              </p>
            </div>
          )}

          {step === 1 && (
            <div className="animate-fade-in">
              <h2 className="text-lg font-bold leading-tight text-white">How you read</h2>
              <p className="mt-1 text-sm leading-relaxed text-krypt-muted">
                Two ways to run the terminal — flip anytime from the title bar.
              </p>
              <div className="mt-4 grid grid-cols-2 gap-3">
                {MODE_CARDS.map(({ mode: m, title, copy, icon: Icon }) => {
                  const active = mode === m
                  return (
                    <button
                      key={m}
                      type="button"
                      onClick={() => chooseMode(m)}
                      aria-pressed={active}
                      className={cls(
                        'flex flex-col items-start gap-2 rounded-2xl border p-4 text-left transition',
                        active
                          ? 'border-scout-red/60 bg-scout-red/10 ring-1 ring-scout-red/50'
                          : 'border-white/10 bg-white/[0.03] hover:border-white/25 hover:bg-white/5',
                      )}
                    >
                      <Icon
                        size={18}
                        className={active ? 'text-scout-red-bright' : 'text-krypt-muted'}
                      />
                      <span
                        className={cls(
                          'text-sm font-bold tracking-[0.2em]',
                          active ? 'text-white' : 'text-white/80',
                        )}
                      >
                        {title}
                      </span>
                      <span className="text-[11px] leading-relaxed text-krypt-muted">{copy}</span>
                    </button>
                  )
                })}
              </div>
            </div>
          )}

          {step === 2 && (
            <div className="animate-fade-in">
              <h2 className="text-lg font-bold leading-tight text-white">Get the picks</h2>
              <p className="mt-1 text-sm leading-relaxed text-krypt-muted">
                The daily flow — no setup needed to start.
              </p>
              <div className="mt-4 space-y-2">
                {FLOW_ROWS.map(({ icon: Icon, title, copy }) => (
                  <div
                    key={title}
                    className="flex items-start gap-3 rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2.5"
                  >
                    <Icon size={14} className="mt-0.5 shrink-0 text-scout-red-bright" />
                    <div className="min-w-0">
                      <div className="text-[11px] font-bold uppercase tracking-widest text-white/90">
                        {title}
                      </div>
                      <div className="mt-0.5 text-[11px] leading-relaxed text-krypt-muted">
                        {copy}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
              <p className="mt-3 text-[10px] uppercase tracking-wider text-krypt-muted/60">
                Auto-posting is optional — set it up later in the DISCORD tab.
              </p>
            </div>
          )}
        </div>

        {/* Footer nav */}
        <div className="mt-5 flex items-center justify-between border-t border-white/5 pt-4">
          {step > 0 ? (
            <GhostButton onClick={() => setStep((s) => s - 1)} className="px-3 py-2 text-xs">
              <ArrowLeft size={13} />
              BACK
            </GhostButton>
          ) : (
            <span />
          )}
          {step < 2 ? (
            <PrimaryButton onClick={() => setStep((s) => s + 1)} className="px-4 py-2 text-xs">
              NEXT
              <ArrowRight size={13} />
            </PrimaryButton>
          ) : (
            <PrimaryButton onClick={finish} className="px-4 py-2 text-xs">
              ENTER THE TERMINAL
              <ArrowRight size={13} />
            </PrimaryButton>
          )}
        </div>
      </div>
    </div>
  )
}

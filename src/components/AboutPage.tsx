import { useEffect, useState } from 'react'
import { Compass, Download, ExternalLink, MessageCircle, RefreshCw, RotateCcw } from 'lucide-react'
import type { UpdaterState } from '@shared/types'
import type { Sport } from '../types'
import { useModelMeta } from '../modelMeta'
import { resetOnboarding, resetTour } from '../prefs'
import { Badge, Card, GhostButton, PrimaryButton, Section } from './common'
import { openExternal } from '../utils/format'

// Model names / backtest accuracies / notes come from src/modelMeta.ts
// (single source of truth; /api/models overrides at runtime). This array only
// fixes the table's display order.
const MODEL_ORDER: Sport[] = [
  'nba',
  'wnba',
  'tennis',
  'nfl',
  'ncaaf',
  'ncaab',
  'mlb',
  'nhl',
  'soccer',
]

/**
 * Updates row under the version badge — desktop only (window.scout?.updater).
 * Today the updater is always 'unconfigured' (no publish config); the row
 * stays subtle and just says so. Once releases exist it surfaces
 * available → downloading → downloaded with a single opt-in button.
 */
function UpdatesRow() {
  const api = window.scout?.updater
  const [st, setSt] = useState<UpdaterState | null>(null)
  const [busy, setBusy] = useState(false)
  const [checked, setChecked] = useState(false)

  useEffect(() => {
    if (!api) return
    let mounted = true
    void api.state().then((s) => {
      if (mounted) setSt(s)
    })
    const off = api.onState((s) => {
      if (!mounted) return
      setSt(s)
      if (s.status !== 'checking') setChecked(true)
    })
    return () => {
      mounted = false
      off()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (!api || !st) return null

  const check = async () => {
    setBusy(true)
    try {
      const s = await api.check()
      setSt(s)
      setChecked(true)
    } finally {
      setBusy(false)
    }
  }

  const statusLine = (() => {
    switch (st.status) {
      case 'unconfigured':
        return 'updates unavailable: not configured'
      case 'error':
        return 'updates unavailable'
      case 'checking':
        return 'checking for updates…'
      case 'downloading':
        return `downloading${st.version ? ` v${st.version}` : ''} — ${Math.round(st.progress ?? 0)}%`
      case 'downloaded':
        return `v${st.version ?? '?'} downloaded — restart to install`
      case 'idle':
        return checked ? 'up to date' : null
      default:
        return null
    }
  })()

  return (
    <div className="mt-4 flex flex-col items-center gap-2">
      {st.status === 'available' || st.status === 'downloading' || st.status === 'downloaded' ? (
        <PrimaryButton
          disabled={st.status === 'downloading'}
          onClick={() => void api.downloadAndInstall()}
        >
          <Download size={13} />
          {st.status === 'available'
            ? `v${st.version ?? '?'} available — DOWNLOAD & RESTART`
            : st.status === 'downloading'
              ? 'DOWNLOADING…'
              : 'RESTART & INSTALL'}
        </PrimaryButton>
      ) : (
        <GhostButton onClick={() => void check()} disabled={busy || st.status === 'checking'}>
          <RefreshCw size={13} className={busy || st.status === 'checking' ? 'animate-spin' : ''} />
          CHECK FOR UPDATES
        </GhostButton>
      )}
      {statusLine && (
        <p
          className="text-[11px] text-krypt-muted/60"
          title={st.status === 'error' || st.status === 'unconfigured' ? st.error : undefined}
        >
          {statusLine}
        </p>
      )}
    </div>
  )
}

/** ABOUT — Krypt boilerplate: wordmark, version, models, links, license. */
export default function AboutPage() {
  const [version, setVersion] = useState<string | null>(null)
  useEffect(() => {
    if (window.scout) void window.scout.app.version().then(setVersion)
  }, [])

  // About has no demo prop — always try /api/models once; fetch failure
  // (backend down / demo desktop) leaves the baked fallbacks in place.
  const modelMeta = useModelMeta(false)

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-2xl px-8 py-12">
        {/* Hero */}
        <div className="flex flex-col items-center text-center">
          <img
            src="./krypt.png"
            alt=""
            className="h-16 w-16"
            style={{ filter: 'drop-shadow(0 0 18px rgba(225,29,72,0.85))' }}
            onError={(e) => {
              ;(e.target as HTMLImageElement).style.display = 'none'
            }}
          />
          <h1 className="mt-4 text-4xl font-bold tracking-[0.18em]">
            <span className="text-krypt-gradient">KRYPT</span>{' '}
            <span className="text-white">SCOUT</span>
          </h1>
          <div className="mt-3 flex items-center gap-2">
            <Badge tone="neutral">{version ? `v${version}` : 'DEV BUILD'}</Badge>
            <Badge tone="gradient">KRYPT SUITE</Badge>
          </div>
          <UpdatesRow />
          <p className="mt-5 max-w-lg text-sm leading-relaxed text-krypt-muted">
            A free moneyline scouting terminal. Scout builds its own win probabilities for nine
            sports, converts them to fair lines, and scans every book for positive-EV prices — with
            injuries, weather, line movement, Kelly sizing and automatic performance grading (accuracy,
            log loss, CLV) built in.
          </p>
        </div>

        {/* Models */}
        <Section title="Models · backtest accuracy" description="Argmax hit rate on held-out seasons.">
          <Card padded={false} className="overflow-hidden">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="border-b border-white/10 bg-white/[0.02]">
                  <th className="px-4 py-2 text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70">Sport</th>
                  <th className="px-4 py-2 text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70">Model</th>
                  <th className="px-4 py-2 text-right text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70">Backtest</th>
                  <th className="px-4 py-2 text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70">Approach</th>
                </tr>
              </thead>
              <tbody>
                {MODEL_ORDER.map((s) => {
                  const m = modelMeta[s]
                  return (
                    <tr key={s} className="border-b border-white/5 last:border-0">
                      <td className="px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-scout-red-bright">
                        {s.toUpperCase()}
                      </td>
                      <td className="px-4 py-2 font-mono text-[10px] text-white/80">{m.model}</td>
                      <td className="px-4 py-2 text-right font-mono text-[11px] font-semibold text-white">
                        {m.acc.toFixed(1)}%
                        {m.live && (
                          <span
                            className="ml-1 inline-block h-1 w-1 rounded-full bg-emerald-400/80 align-middle"
                            title="Live from /api/models"
                          />
                        )}
                      </td>
                      <td className="px-4 py-2 text-[11px] text-krypt-muted">{m.note}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            <p className="border-t border-white/10 bg-white/[0.02] px-4 py-2.5 text-[11px] leading-relaxed text-krypt-muted">
              Honest note: the closing line still beats every v1 baseline above. Beating the close —
              not just the coin flip — is the v2 target, which is exactly what the built-in CLV
              tracking measures.
            </p>
          </Card>
        </Section>

        {/* Links */}
        <div className="mt-2 flex items-center justify-center gap-3">
          <GhostButton onClick={() => openExternal('https://krypt.cc')}>
            <ExternalLink size={13} />
            krypt.cc
          </GhostButton>
          <GhostButton onClick={() => openExternal('https://discord.gg/muzFKR657F')}>
            <MessageCircle size={13} />
            Discord
          </GhostButton>
          <span title="Re-run the first-run setup (books, mode)">
            <GhostButton onClick={resetOnboarding}>
              <RotateCcw size={13} />
              Replay onboarding
            </GhostButton>
          </span>
          <span title="Re-run the tab-by-tab tour">
            <GhostButton onClick={resetTour}>
              <Compass size={13} />
              Replay tour
            </GhostButton>
          </span>
        </div>

        <p className="mt-8 text-center text-xs text-krypt-muted">
          Free &amp; open source · no ads, no telemetry · MIT license
        </p>
        <p className="mt-2 text-center text-[11px] text-krypt-muted/60">
          Sportsbook and market links are referral links — Krypt may earn a commission when you
          sign up through them. This never affects model output, odds, or picks.
        </p>
        <p className="mt-2 text-center text-[11px] text-krypt-muted/60">
          Credit to the open-source sports-prediction work that came before us:{' '}
          <span className="font-mono text-white/60">kyleskom</span>,{' '}
          <span className="font-mono text-white/60">saccofrancesco</span>,{' '}
          <span className="font-mono text-white/60">kochlisGit</span>,{' '}
          <span className="font-mono text-white/60">gmalbert</span> — and to the data &amp;
          tooling projects: nflverse, sportsdataverse, TennisMyLife, football-data.co.uk,
          tennis-data.co.uk.
        </p>
        <p className="mt-2 text-center text-[11px] text-krypt-muted/60">
          Historical order-book data: the{' '}
          <span className="font-mono text-white/60">pmxt</span> public Polymarket archive
          (CC BY 4.0) — powers the backtest&apos;s real-fill BOOK column. Hockey expected
          goals: <span className="font-mono text-white/60">MoneyPuck.com</span> — the NHL engine
          reads its team game file when you drop it in (free for non-commercial use, credit
          required; never fetched or shipped by the app).
        </p>
        <p className="mt-2 text-center text-[11px] text-krypt-muted/60">
          Part of the <span className="text-krypt-gradient font-semibold">Krypt</span> suite of free
          desktop tools — find the rest at krypt.cc/tools.
        </p>
        <p className="mt-6 text-center text-[10px] uppercase tracking-wider text-krypt-muted/40">
          Not betting advice. Play responsibly.
        </p>
      </div>
    </div>
  )
}

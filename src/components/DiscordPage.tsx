/**
 * DISCORD — post each day's picks to user-configured Discord webhooks.
 *
 * Config + sending live in the Electron main process (renderer CSP blocks
 * discord.com), reached via window.scout?.discord?. — the whole surface is
 * optional while that side rolls out, so every call is guarded and browser
 * dev mode degrades to a note + the client-side message preview.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Plus, RefreshCw, Send, Timer, Trash2, Webhook } from 'lucide-react'
import type { DiscordConfig, DiscordInfo } from '@shared/types'
import type { BoardMap, GameCard } from '../types'
import { SPORTS, SPORT_TAG } from '../types'
import { daysFromToday } from '../cardview'
import { derivePicks, type Pick, type PickTier } from '../picks'
import { bookAbbrev, fmtAmerican } from '../oddsmath'
import { Badge, Card, GhostButton, Page, PrimaryButton, Section, Toggle } from './common'
import { cls } from '../utils/format'

const WEBHOOK_PREFIX = 'https://discord.com/api/webhooks/'
const TIERS: PickTier[] = ['STRONG', 'LEAN', 'MODEL']
const DEFAULT_TIERS: string[] = ['STRONG', 'LEAN']
/** Hard cap on preview lines — mirrors the poster's per-message cap. */
const PREVIEW_CAP = 12

const inputCls =
  'rounded-xl border border-white/10 bg-black/30 px-3 py-2 font-mono text-xs text-white ' +
  'placeholder:font-sans placeholder:text-krypt-muted/50 ' +
  'focus:border-scout-red/40 focus:outline-none focus:ring-1 focus:ring-scout-red/30'

/** "in 3h 42m" style countdown to an ISO timestamp. */
function fmtIn(iso: string | null | undefined, now: number): string {
  if (!iso) return '—'
  const ms = new Date(iso).getTime() - now
  if (!Number.isFinite(ms)) return '—'
  if (ms <= 30_000) return 'due now'
  const mins = Math.round(ms / 60_000)
  if (mins < 1) return 'in <1m'
  const h = Math.floor(mins / 60)
  return h > 0 ? `in ${h}h ${mins % 60}m` : `in ${mins}m`
}

/** "3h 12m ago" style age of an ISO timestamp. */
function fmtAgo(iso: string, now: number): string {
  const ms = now - new Date(iso).getTime()
  if (!Number.isFinite(ms) || ms < 0) return '—'
  const mins = Math.floor(ms / 60_000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const h = Math.floor(mins / 60)
  if (h < 24) return `${h}h ${mins % 60}m ago`
  return `${Math.floor(h / 24)}d ago`
}

/** "…/webhooks/…a1b2" — never render a full webhook URL (it's a secret). */
function maskUrl(url: string): string {
  return `…/webhooks/…${url.slice(-4)}`
}

function newId(): string {
  try {
    return crypto.randomUUID()
  } catch {
    return `wh-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
  }
}

/** Pieces of one faux-Discord line: `TEN` **ZVEREV** ML +471 @ POLY · … —
 *  split so the preview can style the tag chip and bold team like the real
 *  embed's markdown does. */
function previewParts(p: Pick): { tag: string; rest: string } {
  const tag = SPORT_TAG[p.sport] ?? p.sport.toUpperCase().slice(0, 4)
  const chance = `${Math.round(p.modelProb * 100)}% to win`
  const rest =
    p.kind === 'value'
      ? ` ML ${fmtAmerican(p.price as number)} @ ${bookAbbrev(p.book as string)} · EV +${((p.ev ?? 0) * 100).toFixed(1)}% · ${chance}`
      : ` ML ${fmtAmerican(p.fairPrice)} fair · model pick · ${chance}`
  return { tag, rest }
}

function previewLine(p: Pick): string {
  const { tag, rest } = previewParts(p)
  return `${tag} ${p.team}${rest}`
}

type TestState = { s: 'busy' | 'ok' | 'err'; msg?: string }

interface Props {
  boards: BoardMap
  demo: boolean
}

export default function DiscordPage({ boards, demo }: Props) {
  const api = window.scout?.discord

  // ---- main-process state (config + send history) -------------------------
  const [info, setInfo] = useState<DiscordInfo | null>(null)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!api) return
    let alive = true
    const pull = async () => {
      try {
        const i = await api.info()
        if (alive) setInfo(i)
      } catch {
        /* main process unavailable — keep the last snapshot */
      }
    }
    void pull()
    const poll = setInterval(() => void pull(), 30_000)
    const tick = setInterval(() => setNow(Date.now()), 15_000)
    return () => {
      alive = false
      clearInterval(poll)
      clearInterval(tick)
    }
  }, [api])

  const update = useCallback(
    async (partial: Partial<DiscordConfig>) => {
      if (!api) return
      try {
        setInfo(await api.setConfig(partial))
      } catch {
        /* keep previous snapshot */
      }
    },
    [api],
  )

  const cfg = info?.config
  const webhooks = cfg?.webhooks ?? []
  const tiersOn = new Set(cfg?.tiers ?? DEFAULT_TIERS)

  // ---- add / remove / test webhooks ---------------------------------------
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [addErr, setAddErr] = useState<string | null>(null)
  const [tests, setTests] = useState<Record<string, TestState>>({})

  const addWebhook = () => {
    const n = name.trim()
    const u = url.trim()
    if (!n) {
      setAddErr('Give the webhook a name (e.g. "The Boys #picks").')
      return
    }
    if (!u.startsWith(WEBHOOK_PREFIX)) {
      setAddErr(`URL must start with ${WEBHOOK_PREFIX}`)
      return
    }
    setAddErr(null)
    void update({ webhooks: [...webhooks, { id: newId(), name: n, url: u }] })
    setName('')
    setUrl('')
  }

  const removeWebhook = (id: string) => {
    void update({ webhooks: webhooks.filter((w) => w.id !== id) })
  }

  const testWebhook = async (id: string, u: string) => {
    if (!api) return
    setTests((t) => ({ ...t, [id]: { s: 'busy' } }))
    try {
      const r = await api.test(u)
      setTests((t) => ({
        ...t,
        [id]: r.ok ? { s: 'ok' } : { s: 'err', msg: r.error ?? 'send failed' },
      }))
    } catch {
      setTests((t) => ({ ...t, [id]: { s: 'err', msg: 'test failed' } }))
    }
  }

  const toggleTier = (t: PickTier) => {
    const next = new Set(tiersOn)
    if (next.has(t)) next.delete(t)
    else next.add(t)
    void update({ tiers: TIERS.filter((x) => next.has(x)) })
  }

  // ---- SEND NOW ------------------------------------------------------------
  const [sending, setSending] = useState(false)
  const [sentAt, setSentAt] = useState<number | null>(null)
  const sendNow = async () => {
    if (!api || sending) return
    setSending(true)
    try {
      setInfo(await api.sendNow())
      setSentAt(Date.now())
    } catch {
      /* outcome lands in lastSends on the next poll */
    } finally {
      setSending(false)
      setNow(Date.now())
    }
  }
  // The freshest send — doubles as the SEND NOW result feedback line.
  const lastSend = info?.lastSends[0] ?? null
  const justSent = sentAt !== null && now - sentAt < 20_000 && lastSend !== null

  // ---- client-side MESSAGE PREVIEW ----------------------------------------
  // Same derivation the PICKS page uses: today's slate + selected tiers,
  // capped. The real post is assembled by the main process from the backend's
  // recorded picks — this is a faithful facsimile, not the actual payload.
  const previewPicks = useMemo(() => {
    const cards: GameCard[] = []
    for (const s of SPORTS) cards.push(...(boards[s] ?? []))
    const tiers = new Set(cfg?.tiers ?? DEFAULT_TIERS)
    return derivePicks(cards)
      // Upcoming only — mirrors the sender: started/final games are
      // never posted (stale odds, unbettable picks).
      .filter(
        (p) =>
          daysFromToday(p.card.game.start_time) <= 0 &&
          new Date(p.card.game.start_time).getTime() > Date.now() &&
          tiers.has(p.tier),
      )
      .slice(0, PREVIEW_CAP)
  }, [boards, cfg])

  const dateLabel = new Date()
    .toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    .toUpperCase()

  return (
    <Page
      title="Discord"
      subtitle="Send each day's picks to your servers — webhooks, a daily auto-post, and a manual send."
      actions={
        api ? (
          <PrimaryButton
            onClick={() => void sendNow()}
            disabled={sending || webhooks.length === 0}
            className="!px-4 !py-2 text-xs"
          >
            {sending ? <RefreshCw size={13} className="animate-spin" /> : <Send size={13} />}
            {sending ? 'SENDING…' : 'SEND NOW'}
          </PrimaryButton>
        ) : undefined
      }
    >
      {!api ? (
        <Section>
          <Card>
            <div className="flex items-center gap-3">
              <Webhook size={16} className="shrink-0 text-scout-red-bright" />
              <div>
                <div className="text-sm font-semibold text-white">
                  Webhook posting runs inside the desktop app
                </div>
                <div className="mt-0.5 text-xs text-krypt-muted">
                  Browser dev mode can&apos;t reach Discord — configure webhooks and auto-posting in
                  the Krypt Scout desktop app. The message preview below still shows what a post
                  looks like.
                </div>
              </div>
            </div>
          </Card>
        </Section>
      ) : (
        <>
          {/* WEBHOOKS */}
          <Section
            title="Webhooks"
            description="Each daily post goes to every webhook on this list. Create one in Discord: Server Settings → Integrations → Webhooks."
          >
            <Card>
              {webhooks.length === 0 ? (
                <div className="rounded-xl border border-dashed border-white/10 bg-black/20 px-3 py-4 text-center text-xs text-krypt-muted">
                  No webhooks yet — add your first below to start posting picks.
                </div>
              ) : (
                <div className="space-y-1.5">
                  {webhooks.map((w) => {
                    const t = tests[w.id]
                    return (
                      <div
                        key={w.id}
                        className="flex items-center gap-3 rounded-xl border border-white/10 bg-black/20 px-3 py-2"
                      >
                        <Webhook size={13} className="shrink-0 text-scout-red-bright" />
                        <span className="min-w-0 truncate text-sm font-semibold text-white" title={w.name}>
                          {w.name}
                        </span>
                        <span className="shrink-0 font-mono text-[10px] text-krypt-muted/70">
                          {maskUrl(w.url)}
                        </span>
                        {t?.s === 'ok' && <Badge tone="success">TEST OK</Badge>}
                        {t?.s === 'err' && (
                          <span
                            className="min-w-0 truncate font-mono text-[10px] text-rose-400"
                            title={t.msg}
                          >
                            {t.msg ?? 'ERR'}
                          </span>
                        )}
                        <div className="ml-auto flex shrink-0 items-center gap-1.5">
                          <button
                            onClick={() => void testWebhook(w.id, w.url)}
                            disabled={t?.s === 'busy'}
                            title="Post today's REAL picks embed to this webhook — exactly what the daily auto-post sends"
                            className="inline-flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] px-2 py-1 text-[9px] font-bold tracking-widest text-krypt-muted transition hover:border-white/20 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            {t?.s === 'busy' ? (
                              <RefreshCw size={10} className="animate-spin" />
                            ) : (
                              <Send size={10} />
                            )}
                            TEST
                          </button>
                          <button
                            onClick={() => removeWebhook(w.id)}
                            title="Remove this webhook"
                            className="inline-flex items-center rounded-lg border border-rose-500/25 bg-rose-500/[0.06] px-2 py-1 text-rose-300/80 transition hover:bg-rose-500/15 hover:text-rose-200"
                          >
                            <Trash2 size={11} />
                          </button>
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}

              {/* ADD form */}
              <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-white/5 pt-3">
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Name (e.g. The Boys #picks)"
                  spellCheck={false}
                  className={cls(inputCls, 'w-52')}
                />
                <input
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') addWebhook()
                  }}
                  placeholder="https://discord.com/api/webhooks/…"
                  spellCheck={false}
                  className={cls(inputCls, 'min-w-[260px] flex-1')}
                />
                <GhostButton onClick={addWebhook} className="!px-3 !py-2 !text-xs">
                  <Plus size={12} />
                  Add webhook
                </GhostButton>
              </div>
              {addErr && <p className="mt-2 font-mono text-[10px] text-rose-400">{addErr}</p>}
            </Card>
          </Section>

          {/* DAILY AUTO-POST */}
          <Section
            title="Daily auto-post"
            description="Once a day the app posts the day's picks (from its tracked record) to every webhook above."
          >
            <Card>
              <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
                <div className="flex items-center gap-3">
                  <Toggle
                    checked={cfg?.enabled ?? false}
                    onChange={() => void update({ enabled: !(cfg?.enabled ?? false) })}
                    label="Daily auto-post"
                  />
                  <div>
                    <div className="text-sm font-semibold text-white">Daily auto-post</div>
                    <div className="text-xs text-krypt-muted">
                      Post automatically at the time below, local time.
                    </div>
                  </div>
                </div>
                <label className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-widest text-krypt-muted">
                  Send at
                  <input
                    type="time"
                    value={cfg?.sendAt ?? '11:00'}
                    onChange={(e) => {
                      if (e.target.value) void update({ sendAt: e.target.value })
                    }}
                    disabled={!cfg}
                    className={cls(inputCls, '!py-1.5 disabled:cursor-not-allowed disabled:opacity-40')}
                  />
                </label>
                <div className="flex items-center gap-1.5">
                  {TIERS.map((t) => {
                    const on = tiersOn.has(t)
                    return (
                      <button
                        key={t}
                        onClick={() => toggleTier(t)}
                        disabled={!cfg}
                        title={`Include ${t} picks in the post`}
                        className={cls(
                          'rounded-lg border px-2 py-1 text-[10px] font-semibold tracking-wider transition disabled:cursor-not-allowed disabled:opacity-40',
                          on
                            ? t === 'MODEL'
                              ? 'border-white/20 bg-white/10 text-white'
                              : 'border-emerald-400/30 bg-emerald-500/10 text-emerald-300'
                            : 'border-white/10 bg-white/[0.02] text-krypt-muted/50 hover:border-white/20 hover:text-krypt-muted',
                        )}
                      >
                        {t}
                      </button>
                    )
                  })}
                </div>
                <span className="ml-auto flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-widest text-krypt-muted">
                  <Timer size={11} className="text-scout-red-bright" />
                  Next post
                  <span className="font-mono text-[11px] normal-case tracking-normal text-white/80">
                    {cfg?.enabled ? fmtIn(info?.nextSend, now) : 'paused'}
                  </span>
                </span>
              </div>

              {justSent && lastSend && (
                <p
                  className={cls(
                    'mt-3 animate-fade-in font-mono text-[10px]',
                    lastSend.ok ? 'text-emerald-300' : 'text-rose-400',
                  )}
                >
                  {lastSend.ok
                    ? `Sent — ${lastSend.summary ?? 'ok'}`
                    : `Send failed — ${lastSend.error ?? 'unknown error'}`}
                </p>
              )}

              <div className="mt-4">
                <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-[0.2em] text-white/80">
                  Recent sends
                </div>
                {!info || info.lastSends.length === 0 ? (
                  <div className="rounded-xl border border-white/10 bg-black/40 p-3 text-[10px] uppercase tracking-wider text-krypt-muted/50">
                    No sends yet — use SEND NOW or wait for the daily auto-post.
                  </div>
                ) : (
                  <div className="max-h-48 overflow-y-auto rounded-xl border border-white/10 bg-black/40">
                    {info.lastSends.map((r, i) => (
                      <div
                        key={`${r.startedAt}-${i}`}
                        className="flex items-center gap-3 border-b border-white/5 px-3 py-1.5 last:border-0"
                      >
                        <span className="w-24 shrink-0 font-mono text-[10px] text-krypt-muted">
                          {fmtAgo(r.startedAt, now)}
                        </span>
                        <Badge tone={r.ok ? 'success' : 'danger'}>{r.ok ? 'OK' : 'ERR'}</Badge>
                        <span
                          className={cls(
                            'min-w-0 flex-1 truncate font-mono text-[10px]',
                            r.ok ? 'text-krypt-muted' : 'text-rose-400/90',
                          )}
                          title={r.summary ?? r.error}
                        >
                          {r.summary ?? r.error ?? '—'}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </Card>
          </Section>
        </>
      )}

      {/* MESSAGE PREVIEW */}
      <Section
        title="Message preview"
        description="What today's post will look like in Discord."
        actions={
          <span className="flex items-center gap-2">
            {demo && <Badge tone="gradient">DEMO</Badge>}
            <Badge tone="warn">PREVIEW</Badge>
          </span>
        }
      >
        <Card>
          {/* Faux Discord chat well */}
          <div className="rounded-xl bg-[#313338] p-4">
            <div className="flex items-start gap-3">
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-scout-red/90 text-sm font-bold text-white">
                ◈
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline gap-2">
                  <span className="text-sm font-semibold text-white">Krypt Scout</span>
                  <span className="rounded bg-[#5865F2] px-1 py-px text-[9px] font-bold uppercase text-white">
                    App
                  </span>
                  <span className="text-[10px] text-[#949BA4]">
                    Today at {cfg?.sendAt ?? '11:00'}
                  </span>
                </div>
                {/* Embed: dark card, crimson spine */}
                <div className="mt-1.5 max-w-[520px] overflow-hidden rounded border-l-4 border-[#E11D48] bg-[#2B2D31]">
                  <div className="px-3.5 py-3">
                    <div className="text-sm font-bold text-white">
                      TODAY&apos;S PICKS — {dateLabel}
                    </div>
                    {previewPicks.length === 0 ? (
                      <div className="mt-2 text-xs text-[#949BA4]">
                        Nothing on today&apos;s slate yet — the post fills in as games land.
                      </div>
                    ) : (
                      <div className="mt-2 space-y-1">
                        {previewPicks.map((p) => (
                          <div
                            key={p.gameId}
                            className="truncate font-mono text-[12px] leading-5 text-[#DBDEE1]"
                            title={previewLine(p)}
                          >
                            <span
                              className={cls(
                                'mr-1.5 inline-block h-1.5 w-1.5 rounded-full align-middle',
                                p.tier === 'STRONG'
                                  ? 'bg-emerald-400'
                                  : p.tier === 'LEAN'
                                    ? 'bg-amber-300'
                                    : 'bg-[#949BA4]',
                              )}
                            />
                            <span className="rounded bg-white/10 px-1 py-px text-[10px] text-[#B5BAC1]">
                              {previewParts(p).tag}
                            </span>{' '}
                            <span className="font-bold">{p.team}</span>
                            {previewParts(p).rest}
                          </div>
                        ))}
                      </div>
                    )}
                    {/* Mirrors buildPayload: masked tools link in the body,
                        then the "Krypt Scout · N picks · krypt.cc" footer. */}
                    <div className="mt-2.5 text-[11px] leading-5">
                      <span className="text-[#00A8FC]">
                        Get the free terminal — krypt.cc/tools/scout
                      </span>
                      <span className="text-[#DBDEE1]"> · flat 1u · not betting advice</span>
                    </div>
                    <div className="mt-2 border-t border-white/5 pt-2 text-[10px] text-[#949BA4]">
                      Krypt Scout · {previewPicks.length} pick{previewPicks.length === 1 ? '' : 's'} · krypt.cc
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <p className="mt-3 text-[10px] uppercase tracking-wider text-krypt-muted/60">
            Preview built from the picks on this screen (today&apos;s slate · selected tiers · max{' '}
            {PREVIEW_CAP}). The actual post is assembled from the app&apos;s recorded picks and can
            differ slightly.
          </p>
        </Card>
      </Section>
    </Page>
  )
}

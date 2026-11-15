/**
 * AI AGENTS — let an AI client (Claude, Cursor, Codex, …) read this app.
 *
 * The MCP server lives in the Python backend (backend/kryptscout/mcp) and is
 * off until switched on here. This page is three things: the switch, the list
 * of AI clients with a one-click Add for each (Electron writes a launch entry
 * into that client's own config — no token in it), and a plain record of who
 * connected and what they changed.
 *
 * Read-only unless the user says otherwise: an agent sees the boards, the
 * fair, the pick record and the bet log, and can keep the bet log. Betting
 * (the place_bet tool) is a second switch on this page — off, paper or live,
 * with a per-bet cap and a daily allowance — enforced in the backend
 * (backend/kryptscout/autobet/agent.py), not here.
 */
import { useCallback, useEffect, useState } from 'react'
import { Bot, Check, Copy, FolderOpen, Loader2, Plug, RefreshCw, ShieldCheck, Unplug } from 'lucide-react'
import type { McpClientId, McpClientSetup, McpInstallResult } from '../../shared/types'
import type { AutobetSettings, McpStatus } from '../api'
import { fetchAutobet, fetchMcpStatus, rotateMcpToken, saveAutobetSettings, setMcpEnabled } from '../api'
import { Badge, Card, Empty, GhostButton, Page, PrimaryButton, Section, Toggle } from './common'
import { cls } from '../utils/format'

interface Props {
  demo: boolean
  apiUp: boolean | null
}

type Notice = { tone: 'ok' | 'err'; text: string }
type AgentMode = AutobetSettings['agent_mode']

const MODE_WORDS: Record<AgentMode, string> = {
  off: 'Agents cannot bet. They can still read everything and keep your bet log.',
  paper:
    'An agent can place PAPER bets: recorded at the price the live order book would have filled, graded like real ones, no order sent and no money moved.',
  live: 'An agent can place REAL orders on the exchanges you connected on the Auto-Bet page, inside the caps here.',
}

/** A small number box that saves when you leave it. */
function Cap({
  label,
  value,
  prefix,
  onCommit,
}: {
  label: string
  value: number
  prefix?: string
  onCommit: (v: number) => void
}) {
  const [text, setText] = useState(String(value))
  useEffect(() => setText(String(value)), [value])
  const commit = () => {
    const parsed = parseFloat(text)
    if (Number.isFinite(parsed) && parsed !== value) onCommit(parsed)
    else setText(String(value))
  }
  return (
    <label className="flex items-center gap-2">
      <span className="text-[10px] font-semibold uppercase tracking-wider text-krypt-muted">{label}</span>
      <span className="flex items-center overflow-hidden rounded-lg border border-white/10 bg-black/30">
        {prefix && <span className="pl-2 text-[11px] text-krypt-muted">{prefix}</span>}
        <input
          value={text}
          inputMode="decimal"
          onChange={(e) => setText(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
          className="w-16 bg-transparent px-2 py-1 text-right font-mono text-xs text-white outline-none"
        />
      </span>
    </label>
  )
}

function ago(iso: string | undefined): string {
  if (!iso) return '—'
  const s = (Date.now() - Date.parse(iso)) / 1000
  if (!Number.isFinite(s)) return '—'
  if (s < 90) return 'just now'
  if (s < 5400) return `${Math.round(s / 60)}m ago`
  if (s < 172_800) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86_400)}d ago`
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

export default function AgentsPage({ demo, apiUp }: Props) {
  const live = !demo && apiUp === true
  const bridge = window.scout?.mcp

  const [status, setStatus] = useState<McpStatus | null>(null)
  const [clients, setClients] = useState<McpClientSetup[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [open, setOpen] = useState<McpClientId | null>(null)
  const [copied, setCopied] = useState<McpClientId | null>(null)
  const [armRotate, setArmRotate] = useState(false)
  const [bet, setBet] = useState<AutobetSettings | null>(null)
  const [connected, setConnected] = useState<string[]>([])
  const [armLive, setArmLive] = useState(false)

  const pullStatus = useCallback(async () => {
    const next = await fetchMcpStatus()
    if (next) setStatus(next)
  }, [])

  const pullBetting = useCallback(async () => {
    const auto = await fetchAutobet()
    if (!auto) return
    setBet(auto.settings)
    setConnected((auto.venues ?? []).filter((v) => v.connected).map((v) => v.label))
  }, [])

  const pullClients = useCallback(async () => {
    if (!bridge) return
    try {
      setClients(await bridge.clients())
    } catch {
      /* main process unavailable — keep the last list */
    }
  }, [bridge])

  useEffect(() => {
    if (!live) return
    void pullStatus()
    void pullClients()
    void pullBetting()
    const t = window.setInterval(() => void pullStatus(), 10_000)
    return () => clearInterval(t)
  }, [live, pullStatus, pullClients, pullBetting])

  useEffect(() => {
    if (!armLive) return
    const t = window.setTimeout(() => setArmLive(false), 5000)
    return () => clearTimeout(t)
  }, [armLive])

  const saveBetting = async (patch: Partial<AutobetSettings>) => {
    const saved = await saveAutobetSettings(patch)
    if (saved) {
      setBet(saved)
      setNotice(null)
    } else {
      setNotice({ tone: 'err', text: 'The app could not save that. Try again in a moment.' })
    }
  }

  const pickMode = (mode: AgentMode) => {
    if (!bet || mode === bet.agent_mode) return
    // Real money takes two clicks.
    if (mode === 'live' && !armLive) {
      setArmLive(true)
      return
    }
    setArmLive(false)
    void saveBetting({ agent_mode: mode })
  }

  useEffect(() => {
    if (!armRotate) return
    const t = window.setTimeout(() => setArmRotate(false), 4000)
    return () => clearTimeout(t)
  }, [armRotate])

  const toggle = async () => {
    if (!status || busy) return
    setBusy('switch')
    const next = await setMcpEnabled(!status.enabled)
    setBusy(null)
    if (next) {
      setStatus(next)
      setNotice(null)
    } else {
      setNotice({ tone: 'err', text: 'The app could not change that setting. Try again in a moment.' })
    }
  }

  const act = async (id: McpClientId, kind: 'install' | 'uninstall') => {
    if (!bridge || busy) return
    setBusy(`${kind}:${id}`)
    let result: McpInstallResult
    try {
      result = kind === 'install' ? await bridge.install(id) : await bridge.uninstall(id)
    } catch (err) {
      result = { ok: false, message: (err as Error)?.message ?? String(err), files: [], backups: [] }
    }
    setBusy(null)
    setNotice({ tone: result.ok ? 'ok' : 'err', text: result.message })
    void pullClients()
  }

  const rotate = async () => {
    if (!armRotate) {
      setArmRotate(true)
      return
    }
    setArmRotate(false)
    setBusy('rotate')
    const next = await rotateMcpToken()
    setBusy(null)
    if (next) {
      setStatus(next)
      setNotice({
        tone: 'ok',
        text: 'New access token made. Clients added from this page pick it up on their next call.',
      })
    }
  }

  if (!live) {
    return (
      <Page title="AI Agents" subtitle="Let Claude, Cursor or Codex read your boards.">
        <Empty
          title="Needs the live app"
          message={
            demo
              ? 'Agent access is part of the running backend. Leave demo mode to set it up.'
              : 'The backend is not answering yet. This page fills in as soon as it is up.'
          }
        />
      </Page>
    )
  }

  const enabled = status?.enabled === true
  const activeClients = (status?.clients ?? []).filter((c) => c.active)

  return (
    <Page
      title="AI Agents"
      subtitle="Let Claude, Cursor or Codex read your boards, the sharp fair and your record — and bet, if you say so."
      actions={
        <>
          {enabled ? <Badge tone="success">AGENT ACCESS ON</Badge> : <Badge>OFF</Badge>}
          {activeClients.length > 0 && <Badge tone="gradient">{activeClients.length} CONNECTED</Badge>}
        </>
      }
    >
      {/* ------------------------------------------------------------ switch */}
      <Card className="mb-6">
        <div className="flex items-start gap-4">
          <div className="mt-0.5 rounded-xl border border-scout-red/30 bg-scout-red-dim p-2.5 text-scout-red-bright">
            <Bot size={20} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-3">
              <h2 className="text-base font-semibold text-white">Allow AI agents</h2>
              <Toggle checked={enabled} onChange={() => void toggle()} label="Allow AI agents" />
              {busy === 'switch' && <Loader2 size={13} className="animate-spin text-krypt-muted" />}
            </div>
            <p className="mt-1.5 max-w-2xl text-sm text-krypt-muted">
              When this is on, an AI client you connect below can ask this app for the same things you see on
              screen. It stays on this computer: only programs running here can reach it, and only with the
              app&apos;s access token. The app has to be open while an agent uses it.
            </p>
            <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 text-[11px] text-krypt-muted">
              <span className="inline-flex items-center gap-1.5">
                <ShieldCheck size={12} className="text-emerald-300" />
                {bet && bet.agent_mode !== 'off'
                  ? `An agent can read, keep your bet log, and place ${bet.agent_mode === 'live' ? 'REAL' : 'paper'} bets inside your caps.`
                  : 'An agent can read, and keep your bet log. It cannot place a bet unless you switch that on below.'}
              </span>
              {status?.tokenMasked && (
                <span className="font-mono text-krypt-muted/80">token {status.tokenMasked}</span>
              )}
              {status?.tokenMasked && (
                <button
                  onClick={() => void rotate()}
                  disabled={busy !== null}
                  className={cls(
                    'inline-flex items-center gap-1 font-semibold uppercase tracking-wider transition disabled:opacity-40',
                    armRotate ? 'text-scout-red-bright' : 'text-krypt-muted hover:text-white',
                  )}
                  title="Makes a new access token. Do this if you think the old one leaked."
                >
                  <RefreshCw size={10} className={busy === 'rotate' ? 'animate-spin' : undefined} />
                  {armRotate ? 'Click again to replace the token' : 'New token'}
                </button>
              )}
            </div>
          </div>
        </div>
      </Card>

      {/* ----------------------------------------------------------- betting */}
      {bet && (
        <Card className="mb-6">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-base font-semibold text-white">Let agents bet</h2>
            <div className="flex items-center overflow-hidden rounded-lg border border-white/10 bg-white/[0.03]">
              {(['off', 'paper', 'live'] as const).map((mode) => {
                const active = bet.agent_mode === mode
                const arming = mode === 'live' && armLive
                return (
                  <button
                    key={mode}
                    onClick={() => pickMode(mode)}
                    className={cls(
                      'px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider transition',
                      active && mode === 'live' && 'bg-scout-red/25 text-scout-red-bright',
                      active && mode === 'paper' && 'bg-sky-400/15 text-sky-200',
                      active && mode === 'off' && 'bg-white/10 text-white',
                      !active && (arming ? 'text-scout-red-bright' : 'text-krypt-muted hover:text-white'),
                    )}
                  >
                    {arming ? 'Real money — click again' : mode}
                  </button>
                )
              })}
            </div>
            <Cap
              label="Max per bet"
              prefix="$"
              value={bet.agent_max_usd}
              onCommit={(v) => void saveBetting({ agent_max_usd: v })}
            />
            <Cap
              label="Bets per day"
              value={bet.agent_max_bets_per_day}
              onCommit={(v) => void saveBetting({ agent_max_bets_per_day: v })}
            />
          </div>
          <p className="mt-2.5 max-w-3xl text-sm text-krypt-muted">{MODE_WORDS[bet.agent_mode]}</p>
          <p className="mt-1.5 max-w-3xl text-xs text-krypt-muted/80">
            Whatever the agent sends: one bet per game, never over these caps (a bigger request is refused, not
            trimmed), never above the price its own stated probability supports after fees, never more than two
            cents through the best offer, and your Auto-Bet stops halt it too. Every bet and every refusal is
            listed at the bottom of this page. Nothing here has been shown to make money — an agent&apos;s opinion
            is not an edge until its paper record says so.
          </p>
          {bet.agent_mode === 'live' && (
            <p className="mt-2 text-xs font-semibold text-amber-200">
              {connected.length > 0
                ? `Live on: ${connected.join(', ')}.`
                : 'No exchange account is connected yet, so live bets are refused. Connect one on the Auto-Bet page.'}
              {!enabled && ' Agent access is off above, so no agent can reach this at the moment.'}
            </p>
          )}
        </Card>
      )}

      {notice && (
        <div
          className={cls(
            'mb-6 rounded-xl border px-4 py-3 text-sm',
            notice.tone === 'ok'
              ? 'border-emerald-400/30 bg-emerald-500/10 text-emerald-200'
              : 'border-amber-400/30 bg-amber-500/10 text-amber-200',
          )}
        >
          {notice.text}
        </div>
      )}

      {/* ----------------------------------------------------------- clients */}
      <Section
        title="Connect an AI client"
        description={
          enabled
            ? 'Add puts one entry in that program’s own settings file (a backup is kept). It holds a path, never a token.'
            : 'You can add clients now; they will say the app is closed until agent access is switched on.'
        }
      >
        {!bridge ? (
          <Empty
            title="Desktop app required"
            message="Connecting a client edits files on this computer, which only the installed app can do."
          />
        ) : (
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            {clients.map((c) => {
              const isOpen = open === c.id
              const working = busy === `install:${c.id}` || busy === `uninstall:${c.id}`
              return (
                <Card key={c.id} padded={false} className="p-4">
                  <div className="flex items-center gap-2.5">
                    <span className="truncate text-sm font-semibold text-white">{c.label}</span>
                    {c.installed ? (
                      <Badge tone="success">
                        <Check size={10} /> ADDED
                      </Badge>
                    ) : c.detected ? (
                      <Badge>FOUND</Badge>
                    ) : (
                      c.id !== 'vscode' && <Badge>NOT FOUND</Badge>
                    )}
                    <div className="ml-auto flex items-center gap-1.5">
                      {c.canInstall && !c.installed && (
                        <PrimaryButton onClick={() => void act(c.id, 'install')} disabled={busy !== null}>
                          {working ? <Loader2 size={12} className="animate-spin" /> : <Plug size={12} />}
                          Add
                        </PrimaryButton>
                      )}
                      {c.canInstall && c.installed && (
                        <GhostButton onClick={() => void act(c.id, 'uninstall')} disabled={busy !== null} destructive>
                          {working ? <Loader2 size={12} className="animate-spin" /> : <Unplug size={12} />}
                          Remove
                        </GhostButton>
                      )}
                      <GhostButton onClick={() => setOpen(isOpen ? null : c.id)}>
                        {isOpen ? 'Hide setup' : c.canInstall ? 'Set up by hand' : 'Show setup'}
                      </GhostButton>
                    </div>
                  </div>
                  <p className="mt-2 text-xs text-krypt-muted">{c.note}</p>
                  {isOpen && (
                    <div className="mt-3">
                      <pre className="max-h-56 overflow-auto rounded-lg border border-white/[0.06] bg-black/40 p-3 font-mono text-[10.5px] leading-relaxed text-white/80">
                        {c.snippet}
                      </pre>
                      <div className="mt-2 flex items-center gap-2">
                        <GhostButton
                          onClick={() =>
                            void copyText(c.snippet).then((ok) => {
                              if (!ok) return
                              setCopied(c.id)
                              window.setTimeout(() => setCopied((cur) => (cur === c.id ? null : cur)), 1800)
                            })
                          }
                        >
                          {copied === c.id ? <Check size={12} /> : <Copy size={12} />}
                          {copied === c.id ? 'Copied' : 'Copy setup'}
                        </GhostButton>
                        {c.configPath && (
                          <GhostButton onClick={() => void bridge.openConfigFolder(c.id)}>
                            <FolderOpen size={12} />
                            Open its folder
                          </GhostButton>
                        )}
                        {c.configPath && (
                          <span className="truncate font-mono text-[10px] text-krypt-muted/60" title={c.configPath}>
                            {c.configPath}
                          </span>
                        )}
                      </div>
                    </div>
                  )}
                </Card>
              )
            })}
          </div>
        )}
      </Section>

      {/* ------------------------------------------------------------- tools */}
      <Section title="What an agent can do" description="Every tool reads the same data as the page it comes from.">
        <Card padded={false}>
          <div className="divide-y divide-white/[0.06]">
            {(status?.tools ?? []).map((t) => (
              <div key={t.name} className="flex items-start gap-3 px-4 py-2.5">
                <span className="mt-0.5 w-40 shrink-0 font-mono text-[11px] font-semibold text-white/90">{t.name}</span>
                <span className="min-w-0 flex-1 text-xs text-krypt-muted">{t.description}</span>
                {t.name === 'place_bet' ? (
                  <Badge tone={bet && bet.agent_mode !== 'off' ? 'warn' : undefined}>
                    {bet?.agent_mode === 'live' ? 'BETS · LIVE' : bet?.agent_mode === 'paper' ? 'BETS · PAPER' : 'BETS · OFF'}
                  </Badge>
                ) : t.writes ? (
                  <Badge tone="warn">WRITES YOUR BET LOG</Badge>
                ) : (
                  <Badge>READ</Badge>
                )}
              </div>
            ))}
          </div>
        </Card>
      </Section>

      {/* ---------------------------------------------------------- activity */}
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        <Section title="Who has connected" description="Since the app last started.">
          {(status?.clients ?? []).length === 0 ? (
            <Empty
              title="Nobody yet"
              message="Add a client above, restart it, then ask it to use krypt-scout."
            />
          ) : (
            <Card padded={false}>
              <div className="divide-y divide-white/[0.06]">
                {(status?.clients ?? []).map((c) => (
                  <div key={c.client} className="flex items-center gap-3 px-4 py-2.5 text-xs">
                    <span
                      className={cls('h-2 w-2 shrink-0 rounded-full', c.active ? 'bg-emerald-400' : 'bg-white/20')}
                      title={c.active ? 'Called recently' : 'Quiet for a while'}
                    />
                    <span className="truncate font-semibold text-white">{c.client}</span>
                    {c.version && <span className="font-mono text-[10px] text-krypt-muted/70">{c.version}</span>}
                    <span className="ml-auto font-mono text-[11px] text-krypt-muted">
                      {c.calls} call{c.calls === 1 ? '' : 's'}
                      {c.lastTool ? ` · last ${c.lastTool}` : ''} · {ago(c.lastSeen)}
                    </span>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </Section>

        <Section title="Changes and refusals" description="Reads are counted, not listed. Anything an agent changed, or was refused, is here.">
          {(status?.audit ?? []).length === 0 ? (
            <Empty title="Nothing to show" message="No agent has changed anything or been refused." />
          ) : (
            <Card padded={false}>
              <div className="max-h-80 divide-y divide-white/[0.06] overflow-auto">
                {(status?.audit ?? []).map((a, i) => (
                  <div key={`${a.at}-${i}`} className="px-4 py-2 text-xs">
                    <div className="flex items-center gap-2">
                      <span
                        className={cls(
                          'font-mono text-[10px] font-bold uppercase',
                          a.outcome === 'accepted'
                            ? 'text-emerald-300'
                            : a.outcome === 'error'
                              ? 'text-rose-300'
                              : 'text-amber-300',
                        )}
                      >
                        {a.outcome}
                      </span>
                      <span className="font-mono text-[11px] text-white/90">{a.tool}</span>
                      <span className="truncate text-krypt-muted">{a.client}</span>
                      <span className="ml-auto shrink-0 font-mono text-[10px] text-krypt-muted/70">{ago(a.at)}</span>
                    </div>
                    {a.detail && <div className="mt-0.5 truncate text-[11px] text-krypt-muted/80">{a.detail}</div>}
                  </div>
                ))}
              </div>
            </Card>
          )}
        </Section>
      </div>
    </Page>
  )
}

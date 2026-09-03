/**
 * SCOUT TERMINAL — the shareable "mission control" panel.
 *
 * Self-contained like the reference terminal: component + embedded CSS string
 * (scoped under .st-root) + pure-canvas draw loops. Everything on screen is
 * driven by the shared board state (App polls it) plus terminal-local timers
 * for ping / logs / animation only. Works fully in demo mode — the ambient
 * canvas, drifting chart and replayed wire keep motion constant even with the
 * backend down.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, Share2 } from 'lucide-react'
import type { BoardMap, GameCard, PerformanceReport, Sport } from '../types'
import { SPORTS, SPORT_TAG } from '../types'
import { byStartAsc, daysFromToday, fmtShortDate, startOfLocalDay } from '../cardview'
import { derivePicks, type Pick } from '../picks'
import { bookAbbrev, fmtAmerican, impliedProb } from '../oddsmath'
import { fetchPerformance } from '../api'
import { useModelMeta } from '../modelMeta'
import { apiBase } from '../backendBase'
import { useShareCard } from '../share/useShareCard'
import { useRoi } from '../share/useRoi'

// "KRYPT SCOUT" in ANSI-Shadow figlet letterforms — the terminal's marquee.
// Every row is exactly 96 columns; rendered tiny in a <pre> with a red glow.
const BANNER = String.raw`
██╗  ██╗ ██████╗  ██╗   ██╗ ██████╗  ████████╗   ███████╗  ██████╗  ██████╗  ██╗   ██╗ ████████╗
██║ ██╔╝ ██╔══██╗ ╚██╗ ██╔╝ ██╔══██╗ ╚══██╔══╝   ██╔════╝ ██╔════╝ ██╔═══██╗ ██║   ██║ ╚══██╔══╝
█████╔╝  ██████╔╝  ╚████╔╝  ██████╔╝    ██║      ███████╗ ██║      ██║   ██║ ██║   ██║    ██║
██╔═██╗  ██╔══██╗   ╚██╔╝   ██╔═══╝     ██║      ╚════██║ ██║      ██║   ██║ ██║   ██║    ██║
██║  ██╗ ██║  ██║    ██║    ██║         ██║      ███████║ ╚██████╗ ╚██████╔╝ ╚██████╔╝    ██║
╚═╝  ╚═╝ ╚═╝  ╚═╝    ╚═╝    ╚═╝         ╚═╝      ╚══════╝  ╚═════╝  ╚═════╝   ╚═════╝     ╚═╝`.slice(1)

// ------------------------------------------------------------- module state
// Module-level so a remount (tab away / back) shows last-known data instantly
// and cumulative counters / chart history survive navigation.
const SESSION_T0 = Date.now()
let scannedTotal = 0
const modelHist: number[] = [] // top pick model prob, 0..1
const marketHist: number[] = [] // top pick market implied prob, 0..1
let histGame: string | null = null
const prevTileMetric: Partial<Record<Sport, number>> = {}
let wireCache: WireLine[] = []
const seenLog = new Set<string>()

const HIST_CAP = 120
const WIRE_CAP = 80
const two = (n: number) => String(n).padStart(2, '0')
const clamp = (v: number, a: number, b: number) => (v < a ? a : v > b ? b : v)
const dpr = () => Math.min(2, window.devicePixelRatio || 1)
const utc = () => new Date().toISOString().slice(11, 19)

// Engine names + backtest accuracies come from src/modelMeta.ts (single
// source of truth; /api/models overrides at runtime via useModelMeta).

/** 3-char team mark for canvas labels ("Man City" → MAN, "BOS" → BOS). */
function abbr(name: string): string {
  const clean = name.replace(/[^A-Za-z0-9]/g, '')
  return (clean.length <= 4 ? clean : clean.slice(0, 3)).toUpperCase()
}

interface WireLine {
  key: string
  t: string
  tag: string
  msg: string
  color: string
}

interface NodeSpec {
  id: string
  sport: Sport
  label: string // ABB@ABB
  prob: number // model win prob of the pick side
  ev: number | null // +EV per $1 when a value pick
  value: boolean
}

interface FieldNode extends NodeSpec {
  x: number
  y: number
  ang: number
  r: number
}

/** A signal orb travelling through the slate field (4-phase life). */
interface Orb {
  x: number
  y: number
  tx: number
  ty: number
  ph: number
  r: number
  hot: boolean
  orbR?: number
  orbA?: number
  orbT?: number
  win?: boolean
  a?: number
}

function makeOrb(cv: HTMLCanvasElement | null, nodes: FieldNode[]): Orb {
  const w = cv?.clientWidth ?? 600
  const h = cv?.clientHeight ?? 400
  const edge = (Math.random() * 4) | 0
  let x = 0
  let y = 0
  if (edge === 0) {
    x = Math.random() * w
    y = -10
  } else if (edge === 1) {
    x = w + 10
    y = Math.random() * h
  } else if (edge === 2) {
    x = Math.random() * w
    y = h + 10
  } else {
    x = -10
    y = Math.random() * h
  }
  // Value nodes attract more traffic — that's where the story is.
  const hotPool = nodes.filter((n) => n.value)
  const useHot = hotPool.length > 0 && Math.random() < 0.55
  const pool = useHot ? hotPool : nodes
  const tgt = pool.length ? pool[(Math.random() * pool.length) | 0] : { x: w / 2, y: h / 2 }
  return { x, y, tx: tgt.x, ty: tgt.y, ph: 0, r: 1.7 + Math.random() * 1.6, hot: useHot }
}

interface Props {
  boards: BoardMap
  demo: boolean
  apiUp: boolean | null
}

export default function TerminalPage({ boards, demo, apiUp }: Props) {
  // ---- clock (declared early: the daily filter keys off the local date) ---
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])
  // Changes only at local midnight — keeps the daily filter memo stable.
  const dayKey = startOfLocalDay(now).getTime()

  // ---- derived, shared-state values --------------------------------------
  const allCards = useMemo(() => {
    const all: GameCard[] = []
    for (const s of SPORTS) all.push(...(boards[s] ?? []))
    return all
  }, [boards])

  // DAILY-FIRST working set: games whose LOCAL start date is today, plus
  // live spillover (started before today, not completed). Completed games
  // from prior days drop out. If the entire day is empty, fall back to the
  // soonest upcoming slate so the terminal never goes dead — flagged with a
  // NEXT SLATE note in the ticker.
  const { cards, aheadCount, nextSlateIso } = useMemo(() => {
    const midnight = new Date(dayKey)
    const today: GameCard[] = []
    const future: GameCard[] = []
    for (const c of allCards) {
      const d = daysFromToday(c.game.start_time, midnight)
      if (d === 0 || (d < 0 && !c.game.completed)) today.push(c)
      else if (d > 0 && !c.game.completed) future.push(c)
    }
    if (today.length > 0) {
      return { cards: today.sort(byStartAsc), aheadCount: future.length, nextSlateIso: null }
    }
    if (future.length === 0) return { cards: [], aheadCount: 0, nextSlateIso: null }
    future.sort(byStartAsc)
    const firstDay = daysFromToday(future[0].game.start_time, midnight)
    const slate = future.filter((c) => daysFromToday(c.game.start_time, midnight) === firstDay)
    return {
      cards: slate,
      aheadCount: future.length - slate.length,
      nextSlateIso: slate[0].game.start_time,
    }
  }, [allCards, dayKey])

  const picks = useMemo(() => derivePicks(cards), [cards])
  const valuePicks = useMemo(() => picks.filter((p) => p.kind === 'value'), [picks])
  const bestEv = valuePicks.length ? (valuePicks[0].ev ?? 0) : 0
  const avgEv = valuePicks.length
    ? valuePicks.reduce((a, p) => a + (p.ev ?? 0), 0) / valuePicks.length
    : 0
  const topPick: Pick | null = picks[0] ?? null
  const live = !demo && apiUp === true

  // ---- ROI feed (terminal-local: mount fetch + 5-min refresh; demo payload
  // in demo mode). null until /api/roi answers — surfaces keep their old face.
  const roi = useRoi(demo)
  const units30 = useMemo(() => {
    if (!roi || roi.overall.n_graded === 0) return null
    // Genuine trailing-30-day window: sum daily deltas inside it (the daily
    // series is all-time, so the last cum_units would overstate "30d").
    const cutoff = Date.now() - 30 * 86_400_000
    const daily = roi.daily
    if (!daily.length) return roi.overall.units
    return daily
      .filter((d) => new Date(d.date).getTime() >= cutoff)
      .reduce((sum, d) => sum + d.units, 0)
  }, [roi])

  // ---- share card (ticker icon-button → top picks PNG to clipboard) -------
  const share = useShareCard()

  // ---- model meta (names + backtest accs; /api/models over the fallbacks).
  // Ref mirror for the mount-once wire effect (refillQueue closes over it).
  const modelMeta = useModelMeta(demo)
  const modelMetaRef = useRef(modelMeta)
  modelMetaRef.current = modelMeta

  // Engine label per sport: real model name off the board when present.
  const engineName = (s: Sport): string =>
    boards[s]?.find((c) => c.prediction)?.prediction?.model ?? modelMeta[s].model

  // ---- uptime / scanned counter -------------------------------------------
  const [scanned, setScanned] = useState(scannedTotal)
  useEffect(() => {
    // Ambient scan ticker — the counter must visibly crawl between polls.
    const t2 = window.setInterval(() => {
      scannedTotal += 1 + ((Math.random() * 3) | 0)
      setScanned(scannedTotal)
    }, 4000)
    return () => clearInterval(t2)
  }, [])
  useEffect(() => {
    // Each board poll counts the whole tracked slate (today + ahead) as re-scanned.
    if (allCards.length) {
      scannedTotal += allCards.length
      setScanned(scannedTotal)
    }
  }, [boards, allCards.length])
  const upSecs = Math.max(0, ((now.getTime() - SESSION_T0) / 1000) | 0)
  const uptime = `${two((upSecs / 3600) | 0)}:${two(((upSecs / 60) % 60) | 0)}:${two(upSecs % 60)}`

  // ---- ticker tiles: per-sport TODAY count + best EV / top prob -----------
  const tiles = useMemo(
    () =>
      SPORTS.map((s) => {
        const sp = picks.filter((p) => p.sport === s)
        const val = sp.find((p) => p.kind === 'value')
        const metric = val ? (val.ev ?? 0) * 100 : sp.length ? sp[0].prob * 100 : null
        return {
          sport: s,
          count: cards.filter((c) => c.game.sport === s && !c.game.completed).length,
          metric,
          isEv: !!val,
        }
      }),
    [picks, cards],
  )
  const [dirs, setDirs] = useState<Partial<Record<Sport, number>>>({})
  useEffect(() => {
    const d: Partial<Record<Sport, number>> = {}
    for (const t of tiles) {
      const prev = prevTileMetric[t.sport]
      d[t.sport] =
        prev == null || t.metric == null || Math.abs(t.metric - prev) < 0.005
          ? 0
          : Math.sign(t.metric - prev)
      if (t.metric != null) prevTileMetric[t.sport] = t.metric
    }
    setDirs(d)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boards])

  // ---- REAL /api/health latency ping (2.5s) -------------------------------
  const [latency, setLatency] = useState<number | null>(null)
  const demoRef = useRef(demo)
  demoRef.current = demo
  useEffect(() => {
    let alive = true
    let busy = false
    async function ping() {
      if (busy) return
      busy = true
      try {
        const base = await apiBase()
        const ctl = new AbortController()
        const timer = setTimeout(() => ctl.abort(), 2000)
        const t0 = performance.now()
        try {
          const res = await fetch(base + '/api/health', { signal: ctl.signal })
          if (alive) setLatency(res.ok ? Math.max(1, Math.round(performance.now() - t0)) : null)
        } finally {
          clearTimeout(timer)
        }
      } catch {
        if (alive) setLatency(null)
      } finally {
        busy = false
      }
    }
    void ping()
    const t = window.setInterval(() => void ping(), 2500)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  // ---- GRADE BATTERY: /api/performance daily → last-10 W/L cells ----------
  const [perf, setPerf] = useState<PerformanceReport | null>(null)
  useEffect(() => {
    if (!live) return
    let alive = true
    const load = () => void fetchPerformance().then((p) => alive && p && setPerf(p))
    load()
    const t = window.setInterval(load, 60_000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [live])
  const gradeCells = useMemo<Array<boolean | null>>(() => {
    if (demo || !live) {
      // Fabricated but stable demo run: 7W / 3L.
      return [true, true, false, true, true, true, false, true, false, true]
    }
    const daily = perf?.daily ?? []
    const cells: boolean[] = []
    for (let i = daily.length - 1; i >= 0 && cells.length < 10; i--) {
      const d = daily[i]
      const wins = Math.round(d.accuracy * d.n)
      for (let k = 0; k < d.n && cells.length < 10; k++) cells.push(k >= d.n - wins)
    }
    cells.reverse()
    const pad: Array<boolean | null> = Array.from({ length: 10 - cells.length }, () => null)
    return [...pad, ...cells]
  }, [demo, live, perf])

  // ---- FEED HEALTH (source health — judged on the full tracked slate) -----
  const anyOdds = allCards.some((c) => c.odds.length > 0)
  const feeds: Array<[string, boolean]> = [
    ['espn.api', allCards.length > 0],
    ['odds.api', !demo && anyOdds],
    ['injuries.feed', live || demo],
    ['weather.feed', live || demo],
    ['store.sqlite', true],
  ]

  // ============================ hero canvas ================================
  const fieldRef = useRef<HTMLCanvasElement>(null)
  const orbsRef = useRef<Orb[]>([])
  const nodesRef = useRef<FieldNode[]>([])

  // Node specs: up to 14 live games, picks-first (value first via sort order).
  const nodeSpecs = useMemo<NodeSpec[]>(
    () =>
      picks.slice(0, 14).map((p) => ({
        id: p.gameId,
        sport: p.sport,
        label: `${abbr(p.card.game.away)}@${abbr(p.card.game.home)}`,
        prob: p.prob,
        ev: p.ev,
        value: p.kind === 'value',
      })),
    [picks],
  )
  const winRatio = picks.length ? clamp(0.3 + (valuePicks.length / picks.length) * 0.55, 0.3, 0.8) : 0.5
  const dataRef = useRef({ specs: nodeSpecs, spawnMs: 2600, winRatio })
  dataRef.current = {
    specs: nodeSpecs,
    spawnMs: clamp(2600 - picks.length * 110, 700, 2600),
    winRatio,
  }

  // Burst of orbs on every board poll — the terminal visibly "ingests".
  useEffect(() => {
    if (!nodeSpecs.length) return
    const n = Math.min(6, Math.max(2, (nodeSpecs.length / 3) | 0))
    for (let i = 0; i < n; i++) orbsRef.current.push(makeOrb(fieldRef.current, nodesRef.current))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boards])

  useEffect(() => {
    const cv = fieldRef.current
    if (!cv) return
    let raf = 0
    let hiddenTimer = 0
    let coreP = 0
    let lastKey = ''
    let lastSpawn = 0
    const orbs = orbsRef.current

    function layout(w: number, h: number) {
      const specs = dataRef.current.specs.length
        ? dataRef.current.specs
        : SPORTS.map((s, i) => ({
            id: 'ph' + i,
            sport: s,
            label: SPORT_TAG[s],
            prob: 0.5,
            ev: null,
            value: false,
          }))
      // Loose sport clusters: each sport present gets an angular sector.
      const sports = [...new Set(specs.map((s) => s.sport))]
      const bySport = new Map<Sport, NodeSpec[]>()
      for (const s of specs) {
        const arr = bySport.get(s.sport) ?? []
        arr.push(s)
        bySport.set(s.sport, arr)
      }
      const rx = w / 2 - 84
      const ry = h / 2 - 52
      const out: FieldNode[] = []
      sports.forEach((sp, si) => {
        const group = bySport.get(sp) ?? []
        const base = -Math.PI / 2 + (si / sports.length) * Math.PI * 2
        const spread = ((Math.PI * 2) / sports.length) * 0.66
        group.forEach((n, k) => {
          const ang =
            base + (group.length > 1 ? (k / (group.length - 1) - 0.5) * spread : 0)
          const jit = 0.78 + (((k * 37 + si * 13) % 5) / 5) * 0.22
          out.push({
            ...n,
            ang,
            x: w / 2 + Math.cos(ang) * rx * jit,
            y: h / 2 + Math.sin(ang) * ry * jit,
            r: n.value ? clamp(6 + (n.ev ?? 0) * 55, 6, 11) : 4.5 + Math.abs(n.prob - 0.5) * 9,
          })
        })
      })
      nodesRef.current = out
    }

    function frame(t: number) {
      if (document.hidden) {
        hiddenTimer = window.setTimeout(() => {
          raf = requestAnimationFrame(frame)
        }, 500)
        return
      }
      const cv = fieldRef.current
      if (!cv) {
        raf = requestAnimationFrame(frame)
        return
      }
      const w = cv.clientWidth
      const h = cv.clientHeight
      const d = dpr()
      const key = dataRef.current.specs.map((s) => s.id).join('|')
      if (cv.width !== w * d || cv.height !== h * d || key !== lastKey) {
        cv.width = w * d
        cv.height = h * d
        lastKey = key
        layout(w, h)
      }
      const c = cv.getContext('2d')!
      c.setTransform(d, 0, 0, d, 0, 0)
      c.clearRect(0, 0, w, h)
      // faint crimson grid
      c.strokeStyle = 'rgba(58,18,37,.30)'
      c.lineWidth = 1
      for (let x = 0; x < w; x += 34) {
        c.beginPath()
        c.moveTo(x, 0)
        c.lineTo(x, h)
        c.stroke()
      }
      for (let y = 0; y < h; y += 34) {
        c.beginPath()
        c.moveTo(0, y)
        c.lineTo(w, y)
        c.stroke()
      }
      const cx = w / 2
      const cy = h / 2
      coreP += 0.04
      const nodes = nodesRef.current

      // breathing web + dots travelling the links
      for (let i = 0; i < nodes.length; i++)
        for (let j = i + 1; j < nodes.length; j++) {
          const n1 = nodes[i]
          const n2 = nodes[j]
          if (Math.hypot(n1.x - n2.x, n1.y - n2.y) > Math.min(w, h) * 0.72) continue
          const al = 0.045 + 0.045 * Math.sin(coreP * 1.3 + i * 1.7 + j)
          c.beginPath()
          c.moveTo(n1.x, n1.y)
          c.lineTo(n2.x, n2.y)
          c.strokeStyle = `rgba(225,29,72,${al})`
          c.lineWidth = 1
          c.stroke()
          const tp = (t / 900 + (i * 0.13 + j * 0.07)) % 1
          c.beginPath()
          c.arc(n1.x + (n2.x - n1.x) * tp, n1.y + (n2.y - n1.y) * tp, 1.5, 0, 7)
          c.fillStyle = `rgba(251,113,133,${0.28 + al})`
          c.fill()
        }

      // marching-ants tethers to the core + travelling dot
      const labelBoxes: Array<{ x1: number; y1: number; x2: number; y2: number }> = []
      nodes.forEach((n, ni) => {
        c.beginPath()
        c.moveTo(cx, cy)
        c.lineTo(n.x, n.y)
        c.strokeStyle = `rgba(225,29,72,${0.10 + 0.06 * Math.sin(coreP + n.ang)})`
        c.lineWidth = 1
        c.setLineDash([2, 6])
        c.lineDashOffset = -t / 60
        c.stroke()
        c.setLineDash([])
        const tp = (t / 1400 + n.ang) % 1
        c.beginPath()
        c.arc(cx + (n.x - cx) * tp, cy + (n.y - cy) * tp, 1.6, 0, 7)
        c.fillStyle = 'rgba(251,113,133,.5)'
        c.fill()

        // node
        c.beginPath()
        c.arc(n.x, n.y, n.r, 0, 7)
        c.fillStyle = '#160a14'
        c.strokeStyle = '#E11D48'
        c.lineWidth = 1.5
        c.shadowColor = '#E11D48'
        c.shadowBlur = 7 + (n.value ? (n.ev ?? 0) * 140 : Math.abs(n.prob - 0.5) * 18)
        c.fill()
        c.stroke()
        c.shadowBlur = 0
        // value picks get the emerald ring pulse
        if (n.value) {
          const pr = n.r + 4.5 + Math.sin(coreP * 1.6 + n.ang * 3) * 2
          c.beginPath()
          c.arc(n.x, n.y, pr, 0, 7)
          c.strokeStyle = `rgba(52,211,153,${0.32 + 0.22 * Math.sin(coreP * 1.6 + n.ang * 3)})`
          c.lineWidth = 1.2
          c.stroke()
        }
        // Label pair (matchup + win%) with collision avoidance: prefer the
        // parity side, flip or push outward when dense clusters would stack
        // text on top of each other.
        c.font = '700 9px ui-monospace,monospace'
        const bw = Math.max(c.measureText(n.label).width, 26) + 6
        const cands =
          ni % 2 === 0
            ? [n.y - n.r - 26, n.y + n.r + 4, n.y - n.r - 46, n.y + n.r + 24]
            : [n.y + n.r + 4, n.y - n.r - 26, n.y + n.r + 24, n.y - n.r - 46]
        let by = cands[0]
        for (const y0 of cands) {
          const hit = labelBoxes.some(
            (r) => n.x - bw / 2 < r.x2 && n.x + bw / 2 > r.x1 && y0 < r.y2 && y0 + 22 > r.y1,
          )
          if (!hit) {
            by = y0
            break
          }
        }
        labelBoxes.push({ x1: n.x - bw / 2, y1: by, x2: n.x + bw / 2, y2: by + 22 })
        c.fillStyle = '#EDE7EB'
        c.textAlign = 'center'
        c.textBaseline = 'middle'
        c.fillText(n.label, n.x, by + 6)
        c.fillStyle = n.value ? 'rgba(52,211,153,.9)' : '#A89AA2'
        c.font = '8.5px ui-monospace,monospace'
        c.fillText((n.prob * 100).toFixed(0) + '%', n.x, by + 16)
      })

      // breathing radial core + Krypt-style Scout glyph
      const coreR = 27 + Math.sin(coreP) * 4
      const cg = c.createRadialGradient(cx, cy, 0, cx, cy, coreR)
      cg.addColorStop(0, 'rgba(251,113,133,.85)')
      cg.addColorStop(0.4, 'rgba(225,29,72,.45)')
      cg.addColorStop(1, 'rgba(225,29,72,0)')
      c.beginPath()
      c.arc(cx, cy, coreR, 0, 7)
      c.fillStyle = cg
      c.fill()
      c.fillStyle = '#EDE7EB'
      c.font = '700 15px ui-monospace,monospace'
      c.textAlign = 'center'
      c.textBaseline = 'middle'
      c.shadowColor = '#E11D48'
      c.shadowBlur = 16
      c.fillText('◈', cx, cy + 1)
      c.shadowBlur = 0

      // ambient orb spawner, rate ∝ live pick count
      if (t - lastSpawn > dataRef.current.spawnMs) {
        lastSpawn = t
        orbs.push(makeOrb(cv, nodes))
      }

      // orbs: fly to node → fly to core → orbit → eject green/red and fade
      for (let i = orbs.length - 1; i >= 0; i--) {
        const o = orbs[i]
        if (o.ph === 0) {
          o.x += (o.tx - o.x) * 0.045
          o.y += (o.ty - o.y) * 0.045
          if (Math.hypot(o.tx - o.x, o.ty - o.y) < 6) {
            o.ph = 1
            o.tx = cx
            o.ty = cy
          }
        } else if (o.ph === 1) {
          o.x += (o.tx - o.x) * 0.05
          o.y += (o.ty - o.y) * 0.05
          if (Math.hypot(cx - o.x, cy - o.y) < 10) {
            o.ph = 2
            o.orbR = 16 + Math.random() * 14
            o.orbA = Math.random() * 7
            o.orbT = 40 + Math.random() * 50
          }
        } else if (o.ph === 2) {
          o.orbA! += 0.09
          o.orbT! -= 1
          o.x = cx + Math.cos(o.orbA!) * o.orbR!
          o.y = cy + Math.sin(o.orbA!) * o.orbR!
          if (o.orbT! <= 0) {
            o.ph = 3
            // value found → eject up green; passed → eject down red
            o.win = Math.random() < dataRef.current.winRatio + (o.hot ? 0.12 : 0)
            o.tx = cx + (Math.random() - 0.5) * 60
            o.ty = o.win ? -20 : h + 20
          }
        } else {
          o.x += (o.tx - o.x) * 0.06
          o.y += (o.ty - o.y) * 0.06
          o.a = (o.a ?? 1) - 0.02
          if (o.a <= 0) {
            orbs.splice(i, 1)
            continue
          }
        }
        const col = o.ph >= 3 ? (o.win ? '#34D399' : '#FB7185') : o.hot ? '#FB7185' : '#E11D48'
        c.globalAlpha = o.a ?? 1
        c.beginPath()
        c.arc(o.x, o.y, o.r, 0, 7)
        c.fillStyle = col
        c.shadowColor = col
        c.shadowBlur = 10
        c.fill()
        c.shadowBlur = 0
        c.globalAlpha = 1
      }
      if (orbs.length > 60) orbs.splice(0, orbs.length - 60)
      raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)
    return () => {
      cancelAnimationFrame(raf)
      clearTimeout(hiddenTimer)
    }
  }, [])

  // ===================== ambient background canvas =========================
  // Full-page layer under the panel grid: a slowly drifting perspective grid,
  // ~60 rising ember motes and an occasional horizontal scan sweep. Throttled
  // to ~28fps; prefers-reduced-motion renders one static faint grid instead.
  const bgRef = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const cv = bgRef.current
    const host = cv?.parentElement
    if (!cv || !host) return
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    let raf = 0
    let timer = 0
    let stopped = false
    interface Ember {
      x: number // 0..1 of width
      y: number // px
      v: number // upward px/frame
      amp: number // sway amplitude px
      ph: number // phase
      a: number // base alpha
      rose: boolean
    }
    const embers: Ember[] = []
    const seedEmber = (h: number, anywhere: boolean): Ember => ({
      x: Math.random(),
      y: anywhere ? Math.random() * h : h + 4,
      v: 0.12 + Math.random() * 0.3,
      amp: 6 + Math.random() * 18,
      ph: Math.random() * 7,
      a: 0.05 + Math.random() * 0.2,
      rose: Math.random() < 0.4,
    })

    function draw(t: number) {
      const w = host!.clientWidth
      const h = Math.max(host!.clientHeight, host!.scrollHeight)
      const d = dpr()
      if (cv!.width !== w * d || cv!.height !== h * d) {
        cv!.width = w * d
        cv!.height = h * d
        cv!.style.width = w + 'px'
        cv!.style.height = h + 'px'
      }
      const c = cv!.getContext('2d')!
      c.setTransform(d, 0, 0, d, 0, 0)
      c.clearRect(0, 0, w, h)

      // --- drifting perspective floor grid (crimson, 6-8% opacity) ---------
      const vpx = w / 2
      const vpy = h * 0.34 // vanishing "horizon"
      const drift = reduced ? 0 : (t / 11000) % 1
      const ROWS = 13
      for (let i = 0; i < ROWS; i++) {
        const u = (i / ROWS + drift) % 1 // 0 at horizon → 1 at viewer
        const y = vpy + Math.pow(u, 2.6) * (h * 1.08 - vpy)
        if (y > h + 4) continue
        c.beginPath()
        c.moveTo(0, y)
        c.lineTo(w, y)
        c.strokeStyle = `rgba(225,29,72,${(0.02 + u * 0.06).toFixed(3)})`
        c.lineWidth = 1
        c.stroke()
      }
      // converging rails, static
      const RAILS = 11
      for (let j = -RAILS; j <= RAILS; j++) {
        const bx = vpx + (j / RAILS) * w * 0.95
        // start a bit below the horizon so the vanishing point stays soft
        const sx = vpx + (bx - vpx) * 0.06
        const sy = vpy + (h - vpy) * 0.06
        c.beginPath()
        c.moveTo(sx, sy)
        c.lineTo(bx, h)
        c.strokeStyle = 'rgba(225,29,72,.05)'
        c.lineWidth = 1
        c.stroke()
      }

      if (reduced) return // static faint grid only — no embers / sweep

      // --- rising ember motes ----------------------------------------------
      if (!embers.length) for (let i = 0; i < 60; i++) embers.push(seedEmber(h, true))
      for (const e of embers) {
        e.y -= e.v
        if (e.y < -6) {
          const ne = seedEmber(h, false)
          e.x = ne.x
          e.y = ne.y
          e.v = ne.v
          e.amp = ne.amp
          e.ph = ne.ph
          e.a = ne.a
          e.rose = ne.rose
        }
        const x = e.x * w + Math.sin(t / 2400 + e.ph) * e.amp
        const al = e.a * (0.55 + 0.45 * Math.sin(t / 900 + e.ph * 2))
        c.fillStyle = (e.rose ? 'rgba(251,113,133,' : 'rgba(225,29,72,') + al.toFixed(3) + ')'
        c.fillRect(x, e.y, 1.3, 1.3)
      }

      // --- occasional horizontal scan sweep (~every 12s) --------------------
      const per = 12000
      const p = (t % per) / per
      if (p < 0.16) {
        const q = p / 0.16
        const bw = w * 0.22
        const x = -bw + (w + 2 * bw) * q
        const g = c.createLinearGradient(x, 0, x + bw, 0)
        g.addColorStop(0, 'rgba(251,113,133,0)')
        g.addColorStop(0.5, 'rgba(251,113,133,.04)')
        g.addColorStop(1, 'rgba(251,113,133,0)')
        c.fillStyle = g
        c.fillRect(x, 0, bw, h)
      }
    }

    function loop(t: number) {
      if (stopped) return
      if (document.hidden) {
        timer = window.setTimeout(() => {
          raf = requestAnimationFrame(loop)
        }, 500)
        return
      }
      draw(t)
      if (reduced) return // one static frame is enough
      timer = window.setTimeout(() => {
        raf = requestAnimationFrame(loop)
      }, 36) // ~28fps — depth cue, not a light show
    }
    raf = requestAnimationFrame(loop)
    return () => {
      stopped = true
      cancelAnimationFrame(raf)
      clearTimeout(timer)
    }
  }, [])

  // ==================== gauge + model-vs-market chart ======================
  const gaugeRef = useRef<HTMLCanvasElement>(null)
  const chartRef = useRef<HTMLCanvasElement>(null)
  const gaugeV = useRef(0)
  const gaugeTarget = useRef(0)
  gaugeTarget.current = clamp(bestEv, 0, 0.1) / 0.1

  // Sample the top pick per poll; reset history when the top pick changes.
  const topRef = useRef<{ m: number; k: number } | null>(null)
  useEffect(() => {
    if (!topPick) return
    if (histGame !== topPick.gameId) {
      histGame = topPick.gameId
      modelHist.length = 0
      marketHist.length = 0
    }
    const m = topPick.prob
    const k =
      topPick.marketProb ?? (topPick.price != null ? impliedProb(topPick.price) : m - 0.02)
    topRef.current = { m, k }
    modelHist.push(m)
    marketHist.push(clamp(k, 0.02, 0.98))
    if (modelHist.length > HIST_CAP) {
      modelHist.shift()
      marketHist.shift()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boards, topPick?.gameId])

  // Gentle interpolated drift between poll samples — the line always crawls.
  useEffect(() => {
    const t = window.setInterval(() => {
      const tgt = topRef.current
      if (!tgt || !modelHist.length) return
      const lm = modelHist[modelHist.length - 1]
      const lk = marketHist[marketHist.length - 1]
      modelHist.push(clamp(lm + (tgt.m - lm) * 0.12 + (Math.random() - 0.5) * 0.0045, 0.02, 0.98))
      marketHist.push(clamp(lk + (tgt.k - lk) * 0.12 + (Math.random() - 0.5) * 0.003, 0.02, 0.98))
      if (modelHist.length > HIST_CAP) {
        modelHist.shift()
        marketHist.shift()
      }
    }, 700)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    let stopped = false
    let raf = 0
    function fit(cv: HTMLCanvasElement, h: number) {
      const w = cv.clientWidth
      const d = dpr()
      cv.width = w * d
      cv.height = h * d
      const c = cv.getContext('2d')!
      c.setTransform(d, 0, 0, d, 0, 0)
      return { c, w, h }
    }
    function draw() {
      if (document.hidden) {
        if (stopped) return
        raf = window.setTimeout(() => {
          if (!stopped) requestAnimationFrame(draw)
        }, 500) as unknown as number
        return
      }
      // EDGE GAUGE — eased sweep to best EV on a 0–10% scale
      const gc = gaugeRef.current
      if (gc) {
        gaugeV.current += (gaugeTarget.current - gaugeV.current) * 0.12
        const { c } = fit(gc, 88)
        const cx = 44
        const cy = 48
        const r = 34
        const a0 = Math.PI * 0.8
        const a1 = Math.PI * 2.2
        c.clearRect(0, 0, 88, 88)
        c.beginPath()
        c.arc(cx, cy, r, a0, a1)
        c.strokeStyle = '#2a0d1d'
        c.lineWidth = 8
        c.lineCap = 'round'
        c.stroke()
        const a = a0 + (a1 - a0) * clamp(gaugeV.current, 0.005, 1)
        const g = c.createLinearGradient(0, 0, 88, 0)
        g.addColorStop(0, '#E11D48')
        g.addColorStop(1, '#FB7185')
        c.beginPath()
        c.arc(cx, cy, r, a0, a)
        c.strokeStyle = g
        c.lineWidth = 8
        c.shadowColor = '#E11D48'
        c.shadowBlur = 12
        c.stroke()
        c.shadowBlur = 0
        c.beginPath()
        c.arc(cx + Math.cos(a) * r, cy + Math.sin(a) * r, 3.2, 0, 7)
        c.fillStyle = '#EDE7EB'
        c.fill()
      }
      // MODEL vs MARKET
      const mc = chartRef.current
      if (mc) {
        const { c, w, h } = fit(mc, 128)
        c.clearRect(0, 0, w, h)
        for (let i = 0; i <= 4; i++) {
          const y = 8 + (i / 4) * (h - 16)
          c.beginPath()
          c.moveTo(0, y)
          c.lineTo(w, y)
          c.strokeStyle = 'rgba(58,18,37,.45)'
          c.lineWidth = 1
          c.stroke()
        }
        const src = modelHist.length ? modelHist : [0.5, 0.5]
        const mk = marketHist.length ? marketHist : [0.5, 0.5]
        // Zoomed Y-window around both series — the model↔market gap IS the
        // story, so don't crush it against a full 0..1 axis.
        const lo = Math.max(0, Math.min(...src, ...mk) - 0.045)
        const hi = Math.min(1, Math.max(...src, ...mk) + 0.045)
        const rg = hi - lo || 1
        const X = (i: number, n: number) => (i / (n - 1 || 1)) * w
        const Y = (v: number) => 8 + (1 - (v - lo) / rg) * (h - 16)
        // market implied — dashed, ice (cool = market, warm = model)
        c.beginPath()
        mk.forEach((v, i) => (i ? c.lineTo(X(i, mk.length), Y(v)) : c.moveTo(X(i, mk.length), Y(v))))
        c.strokeStyle = 'rgba(125,211,252,.62)'
        c.lineWidth = 1.2
        c.setLineDash([3, 4])
        c.stroke()
        c.setLineDash([])
        // model prob — gradient area + solid crimson line + glowing endpoint
        c.beginPath()
        c.moveTo(0, h)
        src.forEach((v, i) => c.lineTo(X(i, src.length), Y(v)))
        c.lineTo(w, h)
        c.closePath()
        const g = c.createLinearGradient(0, 0, 0, h)
        g.addColorStop(0, 'rgba(225,29,72,.26)')
        g.addColorStop(1, 'rgba(225,29,72,0)')
        c.fillStyle = g
        c.fill()
        c.beginPath()
        src.forEach((v, i) => (i ? c.lineTo(X(i, src.length), Y(v)) : c.moveTo(X(i, src.length), Y(v))))
        c.strokeStyle = '#FB7185'
        c.lineWidth = 1.9
        c.shadowColor = '#E11D48'
        c.shadowBlur = 8
        c.stroke()
        c.shadowBlur = 0
        c.beginPath()
        c.arc(w - 2, Y(src[src.length - 1]), 3.2, 0, 7)
        c.fillStyle = '#EDE7EB'
        c.shadowColor = '#FB7185'
        c.shadowBlur = 12
        c.fill()
        c.shadowBlur = 0
        c.font = '8px ui-monospace,monospace'
        c.textAlign = 'left'
        c.fillStyle = 'rgba(251,113,133,.9)'
        c.fillText('— model', 6, 14)
        c.fillStyle = 'rgba(125,211,252,.8)'
        c.fillText('--- market implied', 6 + c.measureText('— model  ').width, 14)
      }
      if (stopped) return
      raf = window.setTimeout(() => {
        if (!stopped) requestAnimationFrame(draw)
      }, 70) as unknown as number // ~14fps is plenty for small charts
    }
    draw()
    return () => {
      stopped = true
      clearTimeout(raf)
    }
  }, [])

  // ============================ THE WIRE ===================================
  const [wire, setWire] = useState<WireLine[]>(wireCache)
  const wireSeq = useRef(0)
  const queueRef = useRef<Array<Omit<WireLine, 'key' | 't'>>>([])
  const picksRef = useRef(picks)
  picksRef.current = picks

  const pushWire = (lines: Array<Omit<WireLine, 'key' | 't'>>) => {
    if (!lines.length) return
    setWire((prev) => {
      const next = [
        ...prev,
        ...lines.map((l) => ({ ...l, key: 'w' + wireSeq.current++, t: utc() })),
      ].slice(-WIRE_CAP)
      wireCache = next
      return next
    })
  }

  // Real backend log lines (Electron only) — poll 3s, dedupe by line.
  useEffect(() => {
    if (!window.scout) return
    let alive = true
    const load = async () => {
      try {
        const logs = await window.scout!.backend.logs(60)
        if (!alive || !logs) return
        const fresh: Array<Omit<WireLine, 'key' | 't'>> = []
        for (const l of logs) {
          const k = `${l.ts}|${l.msg}`
          if (seenLog.has(k)) continue
          seenLog.add(k)
          const err = l.level === 'ERROR' || l.level === 'CRITICAL'
          fresh.push({
            tag: err ? 'FAULT' : l.level === 'WARN' ? 'WARN' : 'SYS',
            msg: l.msg.slice(0, 96),
            color: err ? 'var(--loss)' : l.level === 'WARN' ? 'var(--warn)' : 'var(--dim)',
          })
        }
        if (seenLog.size > 800) seenLog.clear()
        pushWire(fresh.slice(-8))
      } catch {
        /* backend gone — the synth drip keeps the wire alive */
      }
    }
    void load()
    const t = window.setInterval(() => void load(), 3000)
    return () => {
      alive = false
      clearInterval(t)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Synthesized event lines from app activity — refilled from the live picks
  // (demo replays the same script off the demo slate) so the wire never stalls.
  function refillQueue() {
    const q = queueRef.current
    const ps = picksRef.current
    if (!ps.length) {
      q.push({ tag: 'SCAN', msg: 'slate empty · rescanning feeds', color: 'var(--ice)' })
      return
    }
    const shuffled = [...ps].sort(() => Math.random() - 0.5).slice(0, 8)
    for (const p of shuffled) {
      const model = p.card.prediction?.model ?? modelMetaRef.current[p.sport].model
      q.push({
        tag: 'PRED',
        msg: `${p.refBook ?? model} ${abbr(p.team)} ${(p.prob * 100).toFixed(1)}%`,
        color: 'var(--tx)',
      })
      if (p.kind === 'value') {
        q.push({
          tag: 'EDGE',
          msg: `+${((p.ev ?? 0) * 100).toFixed(1)}% ${abbr(p.team)} ${fmtAmerican(p.price ?? 0)} ${bookAbbrev(p.book ?? '')}`,
          color: 'var(--win)',
        })
      }
    }
    const s = shuffled[0]
    const nb = s.card.odds.length
    q.push({ tag: 'ODDS', msg: `${SPORT_TAG[s.sport].toLowerCase()} refresh · ${nb} books quoted`, color: 'var(--ice)' })
    q.push({ tag: 'SCAN', msg: `${SPORTS.length}/${SPORTS.length} sports ok · ${ps.length} predictions on today's slate`, color: 'var(--ice)' })
    if (Math.random() < 0.5) {
      const w = Math.random() < 0.7
      q.push({
        tag: 'SETTLE',
        msg: `${SPORT_TAG[shuffled[Math.min(1, shuffled.length - 1)].sport].toLowerCase()} graded ${w ? 'W' : 'L'}`,
        color: w ? 'var(--win)' : 'var(--loss)',
      })
    }
    if (Math.random() < 0.3) q.push({ tag: 'DEVIG', msg: 'vig removed · kelly cap 0.25 applied', color: 'var(--dim)' })
    if (Math.random() < 0.12) q.push({ tag: 'KRYPT', msg: 'krypt.cc · free tools suite', color: 'var(--kr)' })
  }

  useEffect(() => {
    let alive = true
    let timer = 0
    const drip = () => {
      if (!alive) return
      if (queueRef.current.length < 3) refillQueue()
      const l = queueRef.current.shift()
      if (l) pushWire([l])
      // Fresh session: fill the pane fast so it never looks dead, then settle
      // into the ambient 0.9–2.6s cadence.
      const fast = wireCache.length < 14
      timer = window.setTimeout(drip, fast ? 220 + Math.random() * 260 : 900 + Math.random() * 1700)
    }
    timer = window.setTimeout(drip, 300)
    return () => {
      alive = false
      clearTimeout(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ---- decision trace ------------------------------------------------------
  const trace = picks.slice(0, 5)

  const latencyLabel = demo ? 'SIM' : latency != null ? `${latency}ms` : '—'

  return (
    <div className="st-root">
      <style>{ST_CSS}</style>
      <canvas ref={bgRef} className="st-bg" aria-hidden />
      <div className="st-app">
        {/* ============================ TICKER ============================ */}
        <div className="st-ticker">
          <div className="st-brand">
            <h1>
              <b>◈</b> SCOUT&nbsp;TERMINAL
            </h1>
            <div className="st-sub">MODEL DESK · TODAY&apos;S SLATE · MONEYLINE EDGE</div>
          </div>
          <div className={'st-live' + (demo ? ' demo' : live ? '' : ' off')}>
            <span className="st-dot" />
            {demo ? 'DEMO' : live ? 'LIVE' : 'OFFLINE'}
          </div>
          {nextSlateIso && (
            <div
              className="st-live nextslate"
              title="Nothing on today's local slate — showing the soonest upcoming slate instead"
            >
              NEXT SLATE · {fmtShortDate(nextSlateIso)}
            </div>
          )}
          <div className="st-tiles">
            {tiles.map((t) => {
              const dir = dirs[t.sport] ?? 0
              return (
                <div className={'st-tile' + (t.count === 0 ? ' dim' : '')} key={t.sport}>
                  <div className="s">
                    {SPORT_TAG[t.sport]} <span className="n st-num">{t.count}</span>
                  </div>
                  <div className={'v st-num' + (t.isEv ? ' ev' : '')}>
                    {t.metric == null ? '—' : t.isEv ? `+${t.metric.toFixed(1)}%` : `${t.metric.toFixed(0)}%`}
                    <span className={'arr ' + (dir > 0 ? 'up' : dir < 0 ? 'dn' : '')}>
                      {dir > 0 ? '▲' : dir < 0 ? '▼' : '·'}
                    </span>
                  </div>
                  <div className="k">{t.isEv ? 'BEST EV' : t.count === 0 ? 'NONE TODAY' : 'TOP PROB'}</div>
                </div>
              )
            })}
          </div>
          <div className="st-clock">
            <div className="t st-num">{now.toISOString().slice(11, 19)} UTC</div>
            <div className="u">
              SESSION <span className="st-num">{uptime}</span>
            </div>
            <div className="u" title="Upcoming games tracked beyond today's slate">
              TRACKED AHEAD <span className="st-num">{aheadCount}</span>
            </div>
          </div>
          <button
            type="button"
            className={
              'st-share' +
              (share.status === 'copied' || share.status === 'saved' ? ' ok' : '')
            }
            onClick={() => void share.share({ picks, roi, date: new Date(), demo })}
            disabled={share.busy || picks.length === 0}
            title={
              share.status === 'idle'
                ? 'Share top picks — renders a PNG card and copies it to the clipboard'
                : share.statusLabel
            }
            aria-label="Share top picks"
          >
            {share.status === 'copied' || share.status === 'saved' ? (
              <Check size={13} />
            ) : (
              <Share2 size={13} />
            )}
          </button>
        </div>

        {/* ============================ BANNER ============================ */}
        <div className="st-banner" aria-hidden>
          <pre>{BANNER}</pre>
          <div className="st-banner-tag">
            moneyline intelligence · {SPORTS.length} sports
            <br />
            model → devig → edge
          </div>
        </div>

        {/* ============================= LEFT ============================= */}
        <div className="st-col st-left">
          <div className="st-panel">
            <div className="st-lbl">
              <b>◈</b> MODEL RACK <span className="tag">elo.replay</span>
            </div>
            <div className="st-rack">
              {SPORTS.map((s, i) => {
                const n = cards.filter((c) => c.game.sport === s && !c.game.completed).length
                const on = n > 0
                return (
                  <div className="st-eng" key={s}>
                    <span className={'d' + (on ? ' on' : '')} style={{ animationDelay: `${i * 0.21}s` }} />
                    <span className="nm">{engineName(s)}</span>
                    <span className="ac st-num">{modelMeta[s].acc.toFixed(1)}%</span>
                    <span className="gc st-num">{n}g</span>
                  </div>
                )
              })}
            </div>
          </div>

          <div className="st-panel">
            <div className="st-lbl">
              <b>◈</b> EDGE GAUGE <span className="tag">devig → kelly</span>
            </div>
            <div className="st-gauge-wrap">
              <canvas ref={gaugeRef} width={150} height={150} style={{ width: 88, height: 88 }} />
              <div className="st-gauge-meta">
                <div className="g1 st-num">{valuePicks.length ? `+${(bestEv * 100).toFixed(1)}%` : '—'}</div>
                <div className="g2">BEST EV ON BOARD · 0–10% SCALE</div>
                <div className="g3">
                  {topPick && valuePicks.length
                    ? `${valuePicks[0].team} ${fmtAmerican(valuePicks[0].price ?? 0)} @ ${bookAbbrev(valuePicks[0].book ?? '')}`
                    : 'no priced edge — model picks only'}
                </div>
              </div>
            </div>
          </div>

          <div className="st-panel">
            <div className="st-lbl">
              <b>◈</b> GRADE BATTERY{' '}
              <span className="tag">
                {units30 != null ? (
                  <span
                    className="st-num"
                    style={{ color: units30 >= 0 ? 'var(--win)' : 'var(--loss)' }}
                    title="Net units at flat 1u stakes, last 30 days of graded value picks"
                  >
                    {units30 >= 0 ? '+' : ''}
                    {units30.toFixed(1)}u / 30d
                  </span>
                ) : (
                  'last 10 settled'
                )}
              </span>
            </div>
            <div className="st-cells">
              {gradeCells.map((c, i) => (
                <div key={i} className={'st-cell' + (c === null ? '' : c ? ' w' : ' l')} />
              ))}
            </div>
          </div>

          <div className="st-panel">
            <div className="st-lbl">
              <b>◈</b> FEED HEALTH <span className="tag">sources</span>
            </div>
            <div className="st-feeds">
              {feeds.map(([name, ok]) => (
                <div className="st-feed-row" key={name}>
                  <span className="fn">{name}</span>
                  <span className={'bar' + (ok ? ' on' : '')}>
                    <i style={{ width: ok ? '100%' : '18%' }} />
                  </span>
                  <span className={'st ' + (ok ? 'on' : 'off')}>{ok ? 'LIVE' : '—'}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* ============================ CENTER ============================ */}
        <div className="st-col st-center">
          <div className="st-panel st-field">
            <canvas ref={fieldRef} />
            <div className="st-title st-lbl">
              <b>◈</b> THE SLATE FIELD — LIVE GAME GRAPH
            </div>
            <div className="st-ov a">
              <div className="k">GAMES SCANNED</div>
              <div className="v st-num">{scanned.toLocaleString()}</div>
            </div>
            <div className="st-ov b">
              <div className="k">PICKS LIVE</div>
              <div className="v st-num">{picks.length}</div>
            </div>
            <div className="st-ov c">
              <div className="k">{units30 != null ? 'UNITS 30D' : 'AVG EV'}</div>
              <div
                className="v st-num"
                style={{ color: units30 != null && units30 < 0 ? 'var(--loss)' : 'var(--win)' }}
              >
                {units30 != null
                  ? `${units30 >= 0 ? '+' : ''}${units30.toFixed(1)}u`
                  : valuePicks.length
                    ? `+${(avgEv * 100).toFixed(1)}%`
                    : '—'}
              </div>
            </div>
            <div className="st-ov d">
              <div className="k">API</div>
              <div className="v st-num">{latencyLabel}</div>
            </div>
            <div className="st-legend">
              <span>
                <i style={{ background: 'var(--pri)' }} />
                GAME
              </span>
              <span>
                <i style={{ background: 'var(--win)' }} />
                VALUE
              </span>
              <span>
                <i style={{ background: 'var(--pri2)' }} />
                SIGNAL
              </span>
            </div>
          </div>
          <div className="st-panel">
            <div className="st-chart-head">
              <div className="st-lbl">
                <b>◈</b> MODEL vs MARKET —{' '}
                <span style={{ color: 'var(--pri2)' }}>
                  {topPick ? `${abbr(topPick.team)} ${SPORT_TAG[topPick.sport]}` : 'NO PICK'}
                </span>
                <span className="tag">prob.history</span>
              </div>
              <div className="st-now st-num">
                {topPick ? (topPick.prob * 100).toFixed(1) + '%' : '—'}
              </div>
            </div>
            <canvas ref={chartRef} height={128} />
          </div>
        </div>

        {/* ============================ RIGHT ============================= */}
        <div className="st-col st-right">
          <div className="st-panel">
            <div className="st-lbl">
              <b>◈</b> TOP PICKS <span className="tag">edge → devig → kelly</span>
            </div>
            <table className="st-table">
              <thead>
                <tr>
                  <th>TIER</th>
                  <th>MATCHUP</th>
                  <th>SIDE</th>
                  <th>PRICE</th>
                  <th>BOOK</th>
                  <th>EV</th>
                </tr>
              </thead>
              <tbody>
                {picks.length === 0 && (
                  <tr>
                    <td colSpan={6} className="empty">
                      nothing on today&apos;s slate
                    </td>
                  </tr>
                )}
                {picks.slice(0, 7).map((p) => (
                  <tr key={p.gameId}>
                    <td>
                      <span className={'st-tier ' + p.tier.toLowerCase()}>{p.tier[0]}</span>
                    </td>
                    <td className="mu">
                      {abbr(p.card.game.away)}@{abbr(p.card.game.home)}
                    </td>
                    <td>
                      <span className="sym">{abbr(p.team)}</span>
                    </td>
                    <td>{p.price != null ? fmtAmerican(p.price) : fmtAmerican(p.fairPrice)}</td>
                    <td className="bk">{p.book ? bookAbbrev(p.book) : 'FAIR'}</td>
                    <td className={p.ev != null ? 'g-win' : ''}>
                      {p.ev != null ? `+${(p.ev * 100).toFixed(1)}%` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="st-panel st-term">
            <div className="st-lbl">
              <b>◈</b> THE WIRE <span className="tag">logs · boards.poll</span>
            </div>
            <div className="st-log">
              <div className="st-loglines">
                {wire.slice(-24).map((l) => (
                  <div className="ln" key={l.key}>
                    <span className="t">{l.t}</span>{' '}
                    <span className="tg" style={{ color: l.color }}>
                      [{l.tag.toUpperCase().slice(0, 6).padEnd(6)}]
                    </span>{' '}
                    <span className="m">{l.msg}</span>
                  </div>
                ))}
                {wire.length === 0 && <div className="ln dimline">wire warming up…</div>}
              </div>
              <div className="st-prompt">
                <span className="u">scout@wire</span>
                <span className="d">:~$ </span>
                <span className="cur">▮</span>
              </div>
            </div>
          </div>

          <div className="st-panel">
            <div className="st-lbl">
              <b>◈</b> DECISION TRACE <span className="tag">model → gate → call</span>
            </div>
            <div className="st-tracev">
              {trace.map((p) => (
                <div className="st-tchip" key={p.gameId}>
                  <b>{abbr(p.team)}</b>
                  <span className="arw">▸</span>
                  {p.refBook ?? p.card.prediction?.model ?? modelMeta[p.sport].model}
                  <span className="arw">▸</span>
                  <span className="st-num">{(p.prob * 100).toFixed(1)}%</span>
                  <span className="arw">▸</span>
                  {p.kind === 'value' ? (
                    <span className="g-win st-num">TAKE {fmtAmerican(p.price ?? 0)}</span>
                  ) : (
                    <span className="pass">PASS</span>
                  )}
                </div>
              ))}
              {trace.length === 0 && <div className="st-tchip dimline">no picks yet — engines warming up</div>}
            </div>
          </div>
        </div>

        {/* ============================ STRIP ============================= */}
        <div className="st-strip">
          <span className="seq">
            model <i>→</i> devig <i>→</i> edge <i>→</i> kelly
          </span>
          <span className="dash">—</span>
          <span className="tail">the gap is the edge.</span>
        </div>
      </div>
    </div>
  )
}

const ST_CSS = `
.st-root{--ground:#070409;--panel:#1a0c18;--panel2:#130711;--line:#4a1a30;--line2:#5d2240;
  --pri:#E11D48;--pri2:#FB7185;--win:#34D399;--loss:#FB7185;--warn:#FBBF24;--kr:#C084FC;--ice:#7DD3FC;
  --tx:#EDE7EB;--mut:#A89AA2;--dim:#8A7A83;--dim2:#6E5F68;
  --mono:"JetBrains Mono",ui-monospace,"Cascadia Code",Menlo,Consolas,monospace;
  position:relative;height:100%;width:100%;overflow:auto;padding:12px;font-family:var(--mono);color:var(--tx);
  background:radial-gradient(1200px 600px at 50% -8%,rgba(225,29,72,.10),transparent 60%),linear-gradient(180deg,#070409,#050307)}
.st-bg{position:absolute;top:0;left:0;z-index:0;pointer-events:none}
.st-app{position:relative;z-index:1}
.st-app{max-width:1520px;margin:0 auto;display:grid;gap:10px;grid-template-columns:262px minmax(0,1fr) 352px;
  grid-template-areas:"ticker ticker ticker" "banner banner banner" "left center right" "strip strip strip";grid-template-rows:auto auto auto auto;align-items:start}
@media (max-width:1180px){.st-app{grid-template-columns:1fr;grid-template-areas:"ticker" "banner" "center" "left" "right" "strip";grid-template-rows:auto}}
.st-num{font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.st-panel{position:relative;background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);
  border-radius:9px;padding:11px 12px;overflow:hidden;box-shadow:inset 0 1px 0 rgba(251,113,133,.05),0 8px 26px rgba(0,0,0,.5)}
.st-lbl{font-size:9.5px;letter-spacing:.22em;text-transform:uppercase;color:var(--dim);display:flex;align-items:center;gap:7px}
.st-lbl b{color:var(--pri2);font-weight:600}
.st-lbl .tag{margin-left:auto;color:var(--dim2);letter-spacing:.14em;text-transform:none}
.st-ticker{grid-area:ticker;display:flex;align-items:center;gap:16px;min-width:0;background:linear-gradient(180deg,#1a0a16,#0e0510);
  border:1px solid var(--line);border-radius:9px;padding:9px 14px}
/* Narrow widths (single-column layout): the 9-tile strip takes its own
   full-width row under the brand/status/clock line instead of crushing it. */
@media (max-width:1180px){
  .st-ticker{flex-wrap:wrap;row-gap:8px}
  .st-ticker .st-tiles{order:10;flex:1 1 100%;margin-left:0}
  .st-ticker .st-clock{margin-left:auto}
}
.st-brand h1{font-size:15px;letter-spacing:.30em;font-weight:700;color:var(--tx);margin:0}
.st-brand h1 b{color:var(--pri2)}
.st-brand .st-sub{font-size:8.5px;letter-spacing:.26em;color:var(--dim);margin-top:2px}
.st-live{display:flex;align-items:center;gap:7px;font-size:10px;letter-spacing:.2em;color:var(--pri2);padding:4px 10px;
  border:1px solid rgba(251,113,133,.3);border-radius:20px;background:rgba(225,29,72,.08);min-width:max-content}
.st-live.demo{color:var(--warn);border-color:rgba(251,191,36,.3);background:rgba(251,191,36,.06)}
.st-live.off{color:var(--dim);border-color:var(--line);background:transparent}
.st-live.nextslate{color:var(--warn);border-color:rgba(251,191,36,.3);background:rgba(251,191,36,.06)}
.st-live .st-dot{width:7px;height:7px;border-radius:50%;background:currentColor;box-shadow:0 0 10px currentColor;animation:stblink 1.5s infinite}
@keyframes stblink{0%,100%{opacity:1}50%{opacity:.25}}
/* KRYPT SCOUT marquee — glowing-red ANSI-shadow figlet between ticker and grid.
   Breathes over 4s; a 1-frame flicker dip every 7s. Both cut under reduced motion. */
.st-banner{grid-area:banner;position:relative;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(180deg,#160812,#0a0409);border:1px solid var(--line);border-radius:9px;
  padding:8px 14px;overflow:hidden;user-select:none}
.st-banner::before{content:'';position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(58% 130% at 50% 50%,rgba(225,29,72,.11),transparent 72%)}
.st-banner pre{position:relative;margin:0;font-family:var(--mono);font-size:clamp(6.5px,0.9vw,9.5px);
  line-height:1.15;color:var(--pri2);
  text-shadow:0 0 6px rgba(225,29,72,.85),0 0 18px rgba(225,29,72,.55);
  animation:stbanner-breathe 4s ease-in-out infinite,stbanner-flick 7s steps(1,end) infinite}
@keyframes stbanner-breathe{
  0%,100%{text-shadow:0 0 6px rgba(225,29,72,.75),0 0 16px rgba(225,29,72,.45)}
  50%{text-shadow:0 0 7px rgba(225,29,72,.95),0 0 22px rgba(225,29,72,.65),0 0 42px rgba(225,29,72,.28)}}
@keyframes stbanner-flick{0%,93%,94%,100%{opacity:1}93.5%{opacity:.82}}
.st-banner-tag{position:absolute;right:16px;bottom:8px;text-align:right;font-size:8px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--dim);line-height:1.7}
@media (max-width:1180px){.st-banner-tag{display:none}}
/* 9 sports: tiles keep a readable floor width and the strip scrolls
   horizontally (scrollbar hidden) instead of crushing at narrow widths. */
.st-tiles{display:flex;gap:8px;flex:1;min-width:0;overflow-x:auto;overflow-y:hidden;margin-left:4px;
  scrollbar-width:none}
.st-tiles::-webkit-scrollbar{display:none}
.st-tile{flex:1 0 auto;min-width:64px;border-left:1px solid var(--line);padding-left:8px}
.st-tile .s{font-size:9px;letter-spacing:.14em;color:var(--pri2)}
.st-tile .s .n{color:var(--tx);font-size:11px;font-weight:700}
.st-tile .v{font-size:13px;font-weight:600;color:var(--tx);white-space:nowrap}
.st-tile .v.ev{color:var(--win)}
.st-tile .v .arr{font-size:8.5px;margin-left:4px;color:var(--dim2)}
.st-tile .v .arr.up{color:var(--win)}.st-tile .v .arr.dn{color:var(--loss)}
.st-tile .k{font-size:7.5px;letter-spacing:.16em;color:var(--dim2);margin-top:1px}
.st-tile.dim .s,.st-tile.dim .s .n{color:var(--dim2)}
.st-tile.dim .v{color:var(--dim2)}
.st-clock{text-align:right;min-width:max-content}
.st-share{display:flex;align-items:center;justify-content:center;width:30px;height:30px;flex:none;border-radius:7px;
  border:1px solid var(--line);background:rgba(225,29,72,.06);color:var(--pri2);cursor:pointer;transition:border-color .2s,color .2s,box-shadow .2s}
.st-share:hover:not(:disabled){border-color:var(--pri);color:var(--tx);box-shadow:0 0 12px rgba(225,29,72,.35)}
.st-share:disabled{opacity:.4;cursor:not-allowed}
.st-share.ok{color:var(--win);border-color:rgba(52,211,153,.45);background:rgba(52,211,153,.08)}
.st-clock .t{font-size:13px;font-weight:600;color:var(--tx)}
.st-clock .u{font-size:8.5px;letter-spacing:.18em;color:var(--dim)}
.st-col{display:flex;flex-direction:column;gap:10px;min-width:0}
.st-left{grid-area:left}.st-center{grid-area:center}.st-right{grid-area:right}
.st-center .st-panel.st-field{height:clamp(300px,42vh,460px)}
.st-root canvas{display:block;width:100%}
.st-rack{display:flex;flex-direction:column;gap:6px;margin-top:9px}
.st-eng{display:flex;align-items:center;gap:8px;font-size:11px}
.st-eng .d{width:6px;height:6px;border-radius:50%;background:var(--dim2);flex:none}
.st-eng .d.on{background:var(--pri2);box-shadow:0 0 8px var(--pri);animation:stpulse 2.1s infinite}
@keyframes stpulse{0%,100%{opacity:1}50%{opacity:.35}}
.st-eng .nm{color:var(--mut);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.st-eng .ac{color:var(--tx);font-weight:600;width:44px;text-align:right}
.st-eng .gc{color:var(--dim2);width:26px;text-align:right}
.st-gauge-wrap{display:flex;align-items:center;gap:12px;margin-top:6px}
.st-gauge-wrap canvas{width:88px!important;flex:none}
.st-gauge-meta .g1{font-size:25px;font-weight:700;letter-spacing:-.02em;color:var(--win)}
.st-gauge-meta .g2{font-size:8px;letter-spacing:.14em;color:var(--dim);margin-top:2px}
.st-gauge-meta .g3{font-size:9px;color:var(--mut);margin-top:6px;line-height:1.5}
.st-cells{display:grid;grid-template-columns:repeat(10,1fr);gap:5px;margin-top:9px}
.st-cell{aspect-ratio:1;border-radius:3px;background:var(--dim2);transition:background .3s}
.st-cell.w{background:var(--win);box-shadow:0 0 8px rgba(52,211,153,.55)}
.st-cell.l{background:var(--loss);box-shadow:0 0 8px rgba(251,113,133,.5)}
.st-feeds{display:flex;flex-direction:column;gap:5px;margin-top:9px}
.st-feed-row{display:flex;align-items:center;gap:8px;font-size:11px}
.st-feed-row .fn{color:var(--tx);width:86px}
.st-feed-row .bar{flex:1;height:4px;background:var(--ground);border-radius:3px;overflow:hidden}
.st-feed-row .bar i{display:block;height:100%;background:var(--dim2);transition:width .6s,background .3s}
.st-feed-row .bar.on i{background:linear-gradient(90deg,#0e9f6e,var(--win))}
.st-feed-row .st{width:32px;text-align:right;font-size:9px;letter-spacing:.1em}
.st-feed-row .st.on{color:var(--win)}.st-feed-row .st.off{color:var(--dim2)}
.st-field{position:relative}
.st-field canvas{position:absolute;inset:0;width:100%!important;height:100%!important}
.st-ov{position:absolute;z-index:3;pointer-events:none}
.st-ov .k{font-size:8.5px;letter-spacing:.18em;color:var(--dim)}
.st-ov .v{font-size:22px;font-weight:700;letter-spacing:-.02em;line-height:1;color:var(--tx);text-shadow:0 0 18px rgba(225,29,72,.55)}
.st-ov.a{top:12px;left:14px}.st-ov.b{top:12px;right:14px;text-align:right}
.st-ov.c{bottom:12px;left:14px}.st-ov.d{bottom:12px;right:14px;text-align:right}
.st-field .st-title{position:absolute;top:11px;left:50%;transform:translateX(-50%);z-index:3;white-space:nowrap}
.st-legend{position:absolute;bottom:11px;left:50%;transform:translateX(-50%);z-index:3;display:flex;gap:14px;font-size:8.5px;letter-spacing:.12em;color:var(--dim)}
.st-legend i{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle}
.st-chart-head{display:flex;justify-content:space-between;align-items:baseline}
.st-chart-head .st-now{font-size:12px;color:var(--tx);font-weight:600}
.st-chart-head .st-lbl{min-width:0;overflow:hidden}
.st-table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;margin-top:2px}
.st-table th{font-size:8.5px;letter-spacing:.12em;color:var(--dim);text-align:right;padding:7px 5px 6px;font-weight:500;border-bottom:1px solid var(--line)}
.st-table th:nth-child(-n+3),.st-table td:nth-child(-n+3){text-align:left}
.st-table td{font-size:11px;padding:5px;border-bottom:1px solid rgba(74,26,48,.5);color:var(--tx);text-align:right;white-space:nowrap}
.st-table td .sym{color:var(--tx);font-weight:700}
.st-table td.mu{color:var(--tx);font-size:10px}
.st-table td.bk{color:var(--ice)}
.st-table td.empty{color:var(--dim2);text-align:center;padding:14px 0}
.st-tier{display:inline-block;width:14px;height:14px;line-height:14px;text-align:center;border-radius:4px;font-size:8.5px;font-weight:700}
.st-tier.strong{background:rgba(52,211,153,.16);color:var(--win)}
.st-tier.lean{background:rgba(251,191,36,.13);color:var(--warn)}
.st-tier.model{background:rgba(237,231,235,.07);color:var(--mut)}
.g-win{color:var(--win)}
.st-term{display:flex;flex-direction:column}
/* CRT log well — Krypt Zeus storm-ops treatment: true-black phosphor pane,
   scanlines + vignette overlay, glowing padded tags, blinking prompt. */
.st-log{height:272px;margin-top:8px;position:relative;overflow:hidden;display:flex;flex-direction:column;
  background:linear-gradient(180deg,rgba(6,2,5,.94),rgba(2,1,2,.97));
  border:1px solid rgba(225,29,72,.16);border-radius:7px;padding:8px 10px 6px;
  box-shadow:inset 0 0 70px rgba(225,29,72,.055),inset 0 1px 0 rgba(255,255,255,.03)}
.st-log::after{content:'';position:absolute;inset:0;pointer-events:none;border-radius:7px;
  background:repeating-linear-gradient(0deg,rgba(255,255,255,.024) 0 1px,transparent 1px 3px),
  radial-gradient(130% 90% at 50% 0%,transparent 62%,rgba(0,0,0,.35) 100%)}
.st-loglines{flex:1;min-height:0;overflow:hidden;display:flex;flex-direction:column;justify-content:flex-end;
  font-size:11.5px;line-height:1.5;-webkit-mask-image:linear-gradient(180deg,transparent,#000 26px)}
.st-log .ln{flex:none;white-space:pre;overflow:hidden;text-overflow:ellipsis}
.st-log .t{color:#5c4a54}
.st-log .tg{text-shadow:0 0 7px}
.st-log .m{color:rgba(245,240,243,.85)}
.st-log .dimline{color:var(--dim2)}
.st-prompt{font-size:11.5px;line-height:1.6;flex:none}
.st-prompt .u{color:var(--win);text-shadow:0 0 8px rgba(52,211,153,.4)}
.st-prompt .d{color:#5c4a54}
@keyframes stcursor{0%,49%{opacity:1}50%,100%{opacity:0}}
.st-prompt .cur{color:var(--pri2);animation:stcursor 1.1s steps(1) infinite;text-shadow:0 0 10px rgba(251,113,133,.6)}
.st-tracev{display:flex;flex-direction:column;gap:5px;margin-top:8px}
.st-tchip{display:flex;align-items:center;gap:7px;font-size:11px;color:var(--mut);border:1px solid var(--line);border-radius:6px;
  padding:4px 9px;white-space:nowrap;overflow:hidden;background:var(--ground)}
.st-tchip b{color:var(--tx);font-weight:700}
.st-tchip .st-num{color:var(--tx)}
.st-tchip .g-win{color:var(--win)}
.st-tchip .arw{color:var(--dim2)}
.st-tchip .pass{color:var(--dim)}
.st-tchip.dimline{color:var(--dim2)}
.st-strip{grid-area:strip;display:flex;align-items:center;justify-content:center;gap:10px;font-size:9.5px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--dim);padding:8px 12px;border:1px solid rgba(225,29,72,.18);border-radius:9px;
  background:linear-gradient(90deg,rgba(225,29,72,.08),transparent 40%,transparent 60%,rgba(225,29,72,.08))}
.st-strip .seq{color:var(--mut)}
.st-strip .seq i{color:var(--pri2);font-style:normal;margin:0 4px}
.st-strip .dash{color:var(--dim2)}
.st-strip .tail{color:var(--pri2);text-shadow:0 0 14px rgba(225,29,72,.5)}
@media (prefers-reduced-motion:reduce){.st-live .st-dot,.st-eng .d.on,.st-prompt .cur,.st-banner pre{animation:none}}
`

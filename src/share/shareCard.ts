/**
 * Share-card generator — a pure canvas renderer that turns the current picks
 * into a 1200×{dynamic}px branded PNG in the terminal's visual language:
 * near-black crimson-tinted ground, subtle grid + glow, Chakra Petch display
 * type, JetBrains Mono numbers, emerald value accents, ice book marks.
 *
 * No React, no fetches — callers hand in picks (already filtered/sorted) and
 * an optional RoiSummary; the QR code comes from `qrcode-generator` (zero-dep
 * MIT) and degrades to a text-only footer if generation ever throws.
 */
import qrcode from 'qrcode-generator'
import type { Pick } from '../picks'
import type { RoiSummary } from '../api'
import { SPORT_TAG } from '../types'
import { fmtAmerican } from '../oddsmath'
import { bookShort } from '../config/referrals'

export const SHARE_URL = 'https://krypt.cc/tools/scout'
export const SHARE_URL_LABEL = 'krypt.cc/tools/scout'

/** Card geometry — widths fixed, height grows with the pick count. */
const W = 1200
const PAD = 64
const HEADER_H = 216
const ROW_H = 78
const FOOTER_H = 196
export const MAX_SHARE_PICKS = 8

// Palette — mirrors the terminal tokens (tailwind.config.js + TerminalPage CSS).
const INK = '#EDE7EB'
const MUTED = '#A89AA2'
const DIM = '#8A7A83'
const CRIMSON = '#E11D48'
const CRIMSON_BRIGHT = '#FB7185'
const EMERALD = '#34D399'
const ICE = '#7DD3FC'
const AMBER = '#FBBF24'

const SANS = '"Chakra Petch", Inter, system-ui, sans-serif'
const MONO = '"JetBrains Mono", ui-monospace, Consolas, monospace'

export interface ShareCardOptions {
  picks: Pick[]
  roi?: RoiSummary | null
  date: Date
  demo: boolean
}

/** Await webfonts (Chakra Petch / JetBrains Mono) so canvas text renders in
 *  brand type, not the fallback stack. Never rejects — a font that fails to
 *  load just falls back, which is exactly what CSS would do. */
export async function ensureShareFonts(): Promise<void> {
  try {
    if (typeof document === 'undefined' || !document.fonts) return
    await Promise.allSettled([
      document.fonts.load(`700 52px ${SANS}`),
      document.fonts.load(`600 26px ${SANS}`),
      document.fonts.load(`600 24px ${MONO}`),
      document.fonts.load(`400 14px ${MONO}`),
      document.fonts.ready,
    ])
  } catch {
    /* canvas falls back to system faces — still legible */
  }
}

function roundRect(
  c: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  c.beginPath()
  c.moveTo(x + r, y)
  c.arcTo(x + w, y, x + w, y + h, r)
  c.arcTo(x + w, y + h, x, y + h, r)
  c.arcTo(x, y + h, x, y, r)
  c.arcTo(x, y, x + w, y, r)
  c.closePath()
}

/** Small pill chip; returns its width so callers can flow content after it. */
function chip(
  c: CanvasRenderingContext2D,
  x: number,
  y: number,
  text: string,
  fg: string,
  bg: string,
  border: string,
): number {
  c.font = `700 13px ${SANS}`
  const tw = c.measureText(text).width
  const w = tw + 26
  const h = 28
  roundRect(c, x, y, w, h, 6)
  c.fillStyle = bg
  c.fill()
  c.strokeStyle = border
  c.lineWidth = 1
  c.stroke()
  c.fillStyle = fg
  c.textAlign = 'center'
  c.textBaseline = 'middle'
  c.fillText(text, x + w / 2, y + h / 2 + 1)
  return w
}

const TIER_STYLE: Record<Pick['tier'], { fg: string; bg: string; border: string }> = {
  STRONG: { fg: EMERALD, bg: 'rgba(52,211,153,0.12)', border: 'rgba(52,211,153,0.38)' },
  LEAN: { fg: 'rgba(52,211,153,0.72)', bg: 'rgba(52,211,153,0.05)', border: 'rgba(52,211,153,0.2)' },
  MODEL: { fg: MUTED, bg: 'rgba(255,255,255,0.05)', border: 'rgba(255,255,255,0.14)' },
}

/** Matchup context line: "LAL @ BOS · NBA" (league tag when the sport has one). */
function matchupLine(p: Pick): string {
  const g = p.card.game
  const tag = g.league || SPORT_TAG[p.sport]
  return `${g.away} @ ${g.home} · ${tag}`
}

/** Draw the QR (dark modules on a light plate — scannable off a dark card).
 *  Returns false when generation failed so the caller can skip the plate. */
function drawQr(c: CanvasRenderingContext2D, x: number, y: number, size: number): boolean {
  try {
    const qr = qrcode(0, 'M')
    qr.addData(SHARE_URL)
    qr.make()
    const n = qr.getModuleCount()
    const quiet = 10
    const cell = (size - quiet * 2) / n
    roundRect(c, x, y, size, size, 10)
    c.fillStyle = '#F5F2F4'
    c.fill()
    c.fillStyle = '#120711'
    for (let r = 0; r < n; r++) {
      for (let col = 0; col < n; col++) {
        if (!qr.isDark(r, col)) continue
        // Overdraw by a hair so adjacent modules fuse without seams.
        c.fillRect(x + quiet + col * cell, y + quiet + r * cell, cell + 0.35, cell + 0.35)
      }
    }
    return true
  } catch {
    return false
  }
}

const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']

/**
 * Render the branded picks card. Pure and synchronous — call
 * `await ensureShareFonts()` first so the brand faces are available.
 */
export function renderShareCard(opts: ShareCardOptions): HTMLCanvasElement {
  const picks = opts.picks.slice(0, MAX_SHARE_PICKS)
  const rows = Math.max(picks.length, 1) // empty slate still renders one placeholder row
  const H = HEADER_H + rows * ROW_H + FOOTER_H

  const cv = document.createElement('canvas')
  cv.width = W
  cv.height = H
  const c = cv.getContext('2d')!

  // ---- ground: near-black with a crimson cast + top glow -------------------
  const bg = c.createLinearGradient(0, 0, 0, H)
  bg.addColorStop(0, '#0C0510')
  bg.addColorStop(0.5, '#090409')
  bg.addColorStop(1, '#070408')
  c.fillStyle = bg
  c.fillRect(0, 0, W, H)

  // subtle grid — the terminal texture
  c.strokeStyle = 'rgba(225,29,72,0.05)'
  c.lineWidth = 1
  for (let x = 40; x < W; x += 40) {
    c.beginPath()
    c.moveTo(x + 0.5, 0)
    c.lineTo(x + 0.5, H)
    c.stroke()
  }
  for (let y = 40; y < H; y += 40) {
    c.beginPath()
    c.moveTo(0, y + 0.5)
    c.lineTo(W, y + 0.5)
    c.stroke()
  }

  // radial glow behind the header
  const glow = c.createRadialGradient(W / 2, -80, 0, W / 2, -80, 620)
  glow.addColorStop(0, 'rgba(225,29,72,0.16)')
  glow.addColorStop(1, 'rgba(225,29,72,0)')
  c.fillStyle = glow
  c.fillRect(0, 0, W, 480)

  // hairline frame
  c.strokeStyle = 'rgba(225,29,72,0.28)'
  c.lineWidth = 2
  c.strokeRect(1, 1, W - 2, H - 2)

  // ---- header ---------------------------------------------------------------
  c.textBaseline = 'alphabetic'
  c.textAlign = 'left'
  c.font = `700 30px ${SANS}`
  c.fillStyle = CRIMSON_BRIGHT
  c.shadowColor = CRIMSON
  c.shadowBlur = 18
  c.fillText('◆', PAD, 84)
  c.shadowBlur = 0
  c.fillStyle = INK
  c.fillText('KRYPT SCOUT', PAD + 44, 84)

  const d = opts.date
  const dateLabel = `${MONTHS[d.getMonth()]} ${String(d.getDate()).padStart(2, '0')} · ${d.getFullYear()}`
  c.font = `500 20px ${MONO}`
  c.fillStyle = MUTED
  c.textAlign = 'right'
  c.fillText(dateLabel, W - PAD, 82)

  c.textAlign = 'left'
  c.font = `700 52px ${SANS}`
  c.fillStyle = INK
  c.shadowColor = 'rgba(225,29,72,0.55)'
  c.shadowBlur = 26
  c.fillText("TONIGHT'S PICKS", PAD, 158)
  c.shadowBlur = 0
  const titleW = c.measureText("TONIGHT'S PICKS").width

  if (opts.demo) {
    chip(
      c,
      PAD + titleW + 22,
      128,
      'SAMPLE',
      AMBER,
      'rgba(251,191,36,0.10)',
      'rgba(251,191,36,0.45)',
    )
  }

  // header divider
  c.strokeStyle = 'rgba(225,29,72,0.3)'
  c.lineWidth = 1.5
  c.beginPath()
  c.moveTo(PAD, HEADER_H - 26)
  c.lineTo(W - PAD, HEADER_H - 26)
  c.stroke()

  // column captions
  c.font = `600 12px ${SANS}`
  c.fillStyle = DIM
  c.textAlign = 'left'
  c.textBaseline = 'alphabetic'
  c.fillText('TIER', PAD, HEADER_H - 6)
  c.fillText('THE CALL', PAD + 128, HEADER_H - 6)
  c.textAlign = 'right'
  c.fillText('PRICE', 928, HEADER_H - 6)
  c.fillText('BOOK', 1018, HEADER_H - 6)
  c.fillText('EV', W - PAD, HEADER_H - 6)

  // ---- pick rows ------------------------------------------------------------
  let y = HEADER_H + 8
  if (picks.length === 0) {
    c.font = `500 20px ${MONO}`
    c.fillStyle = DIM
    c.textAlign = 'left'
    c.textBaseline = 'middle'
    c.fillText('no picks on the board — engines warming up', PAD, y + ROW_H / 2)
    y += ROW_H
  }
  for (const p of picks) {
    const midY = y + ROW_H / 2
    // tier chip
    const st = TIER_STYLE[p.tier]
    chip(c, PAD, midY - 14, p.tier, st.fg, st.bg, st.border)

    // TAKE {team}
    c.textBaseline = 'alphabetic'
    c.textAlign = 'left'
    c.font = `700 15px ${SANS}`
    c.fillStyle = CRIMSON_BRIGHT
    c.fillText('TAKE', PAD + 128, midY - 6)
    const takeW = c.measureText('TAKE').width
    c.font = `700 25px ${SANS}`
    c.fillStyle = INK
    // clamp long names (soccer) so they never collide with the price column
    let team = p.team.toUpperCase()
    while (c.measureText(team).width > 470 && team.length > 4) team = team.slice(0, -2)
    c.fillText(team, PAD + 128 + takeW + 12, midY)
    // matchup context under the call
    c.font = `400 13.5px ${MONO}`
    c.fillStyle = MUTED
    c.fillText(matchupLine(p), PAD + 128, midY + 22)

    // price (mono): emerald book price for value picks, fair line for model picks
    const value = p.kind === 'value'
    c.textAlign = 'right'
    c.font = `600 26px ${MONO}`
    c.fillStyle = value ? EMERALD : 'rgba(237,231,235,0.82)'
    c.fillText(fmtAmerican(value ? (p.price as number) : p.fairPrice), 928, midY + 2)

    // book short (ice) / FAIR
    c.font = `600 16px ${MONO}`
    c.fillStyle = value ? ICE : DIM
    c.fillText(value ? bookShort(p.book as string) : 'FAIR', 1018, midY + 2)

    // EV% — emerald, only when there is priced value
    c.font = `600 19px ${MONO}`
    if (value && p.ev != null) {
      c.fillStyle = EMERALD
      c.fillText(`+${(p.ev * 100).toFixed(1)}%`, W - PAD, midY + 2)
    } else {
      c.fillStyle = DIM
      c.fillText('—', W - PAD, midY + 2)
    }

    // row separator
    c.strokeStyle = 'rgba(255,255,255,0.05)'
    c.lineWidth = 1
    c.beginPath()
    c.moveTo(PAD, y + ROW_H - 0.5)
    c.lineTo(W - PAD, y + ROW_H - 0.5)
    c.stroke()
    y += ROW_H
  }

  // ---- footer ---------------------------------------------------------------
  const footTop = y + 8
  c.strokeStyle = 'rgba(225,29,72,0.3)'
  c.lineWidth = 1.5
  c.beginPath()
  c.moveTo(PAD, footTop)
  c.lineTo(W - PAD, footTop)
  c.stroke()

  const qrSize = 128
  const qrX = W - PAD - qrSize
  const qrY = footTop + 24
  const qrDrawn = drawQr(c, qrX, qrY, qrSize)

  // units record line (when the ROI feed is present)
  let ly = footTop + 52
  c.textAlign = 'left'
  c.textBaseline = 'alphabetic'
  const roi = opts.roi
  if (roi && roi.overall.n_graded > 0) {
    const strong = roi.overall.by_tier.STRONG
    const u = strong?.units ?? roi.overall.units
    const label = strong?.units != null ? 'STRONG PICKS' : 'VALUE PICKS'
    const sign = u >= 0 ? '+' : ''
    c.font = `700 24px ${MONO}`
    c.fillStyle = u >= 0 ? EMERALD : CRIMSON_BRIGHT
    c.fillText(`${label} ${sign}${u.toFixed(1)}u ON RECORD`, PAD, ly)
    c.font = `400 13px ${MONO}`
    c.fillStyle = DIM
    c.fillText('flat 1u stakes · graded automatically', PAD, ly + 24)
    ly += 58
  }

  c.font = `700 26px ${SANS}`
  c.fillStyle = CRIMSON_BRIGHT
  c.shadowColor = CRIMSON
  c.shadowBlur = 14
  c.fillText(SHARE_URL_LABEL, PAD, ly + 6)
  c.shadowBlur = 0
  if (!qrDrawn) {
    // text-only fallback when QR generation is unavailable
    c.font = `400 14px ${MONO}`
    c.fillStyle = MUTED
    c.textAlign = 'right'
    c.fillText(SHARE_URL, W - PAD, footTop + 56)
    c.textAlign = 'left'
  }

  c.font = `400 13px ${MONO}`
  c.fillStyle = DIM
  c.fillText('model output · not betting advice · 21+', PAD, H - 30)

  return cv
}

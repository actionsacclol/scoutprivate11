/**
 * useShareCard — generate the branded picks card, copy it to the clipboard as
 * a PNG, and fall back to a file download when the clipboard is unavailable
 * (older Chromium, permission denied, non-secure context). Exposes a tiny
 * status machine for toastless inline button feedback.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { ensureShareFonts, renderShareCard, type ShareCardOptions } from './shareCard'

export type ShareStatus = 'idle' | 'working' | 'copied' | 'saved' | 'error'

/** Button-ready label for each status ('' when idle). */
export const SHARE_STATUS_LABEL: Record<ShareStatus, string> = {
  idle: '',
  working: 'RENDERING…',
  copied: 'COPIED TO CLIPBOARD',
  saved: 'SAVED AS PNG',
  error: 'SHARE FAILED',
}

const RESET_MS = 2600

function canvasToPngBlob(cv: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    cv.toBlob((b) => (b ? resolve(b) : reject(new Error('toBlob returned null'))), 'image/png')
  })
}

async function copyPng(blob: Blob): Promise<boolean> {
  try {
    if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write) return false
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
    return true
  } catch {
    return false
  }
}

function downloadPng(blob: Blob, date: Date): void {
  const ymd = `${date.getFullYear()}${String(date.getMonth() + 1).padStart(2, '0')}${String(date.getDate()).padStart(2, '0')}`
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `krypt-scout-picks-${ymd}.png`
  document.body.appendChild(a)
  a.click()
  a.remove()
  // Give the browser a beat to start the download before revoking.
  window.setTimeout(() => URL.revokeObjectURL(url), 4000)
}

export interface UseShareCard {
  status: ShareStatus
  /** Human label for the current status ('' when idle). */
  statusLabel: string
  busy: boolean
  /** Render the card and copy it (download fallback). Resolves when settled. */
  share: (opts: ShareCardOptions) => Promise<void>
  /** Render the card and download it directly, skipping the clipboard. */
  download: (opts: ShareCardOptions) => Promise<void>
}

export function useShareCard(): UseShareCard {
  const [status, setStatus] = useState<ShareStatus>('idle')
  const timer = useRef<number | null>(null)
  const busyRef = useRef(false)

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current)
    },
    [],
  )

  const settle = useCallback((s: ShareStatus) => {
    setStatus(s)
    if (timer.current !== null) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setStatus('idle'), RESET_MS)
  }, [])

  const run = useCallback(
    async (opts: ShareCardOptions, mode: 'copy' | 'download') => {
      if (busyRef.current) return
      busyRef.current = true
      setStatus('working')
      try {
        await ensureShareFonts()
        const cv = renderShareCard(opts)
        const blob = await canvasToPngBlob(cv)
        if (mode === 'copy' && (await copyPng(blob))) {
          settle('copied')
        } else {
          downloadPng(blob, opts.date)
          settle('saved')
        }
      } catch {
        settle('error')
      } finally {
        busyRef.current = false
      }
    },
    [settle],
  )

  return {
    status,
    statusLabel: SHARE_STATUS_LABEL[status],
    busy: status === 'working',
    share: useCallback((opts: ShareCardOptions) => run(opts, 'copy'), [run]),
    download: useCallback((opts: ShareCardOptions) => run(opts, 'download'), [run]),
  }
}

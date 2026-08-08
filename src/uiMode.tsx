/**
 * SIMPLE / PRO presentation mode — the progressive-disclosure switch.
 *
 * SIMPLE (the default): picks-first navigation, plain-English cards, no
 * quant jargon (EV / Kelly / fair lines hidden). PRO: the full terminal.
 * Persisted in localStorage under `scout-ui-mode`.
 */
import { createContext, useContext } from 'react'
import { cls } from './utils/format'

export type UiMode = 'simple' | 'pro'

export const UI_MODE_KEY = 'scout-ui-mode'

export function loadUiMode(): UiMode {
  try {
    return localStorage.getItem(UI_MODE_KEY) === 'pro' ? 'pro' : 'simple'
  } catch {
    return 'simple'
  }
}

export function saveUiMode(mode: UiMode): void {
  try {
    localStorage.setItem(UI_MODE_KEY, mode)
  } catch {
    /* storage unavailable — mode still works for the session */
  }
}

export const UiModeContext = createContext<UiMode>('simple')

export function useUiMode(): UiMode {
  return useContext(UiModeContext)
}

const MODE_HELP: Record<UiMode, string> = {
  simple: 'Simple view — just the picks, in plain English',
  pro: 'Pro view — every page, every number',
}

/** Segmented SIMPLE | PRO control — compact in the TitleBar, larger in Settings. */
export function ModeSwitch({
  mode,
  onMode,
  size = 'sm',
}: {
  mode: UiMode
  onMode: (m: UiMode) => void
  size?: 'sm' | 'md'
}) {
  return (
    <div
      className={cls(
        'flex items-center overflow-hidden rounded-lg border border-white/10 bg-white/[0.03]',
        size === 'md' && 'rounded-xl',
      )}
      role="group"
      aria-label="Interface mode"
    >
      {(['simple', 'pro'] as UiMode[]).map((m) => {
        const active = mode === m
        return (
          <button
            key={m}
            type="button"
            onClick={() => onMode(m)}
            title={MODE_HELP[m]}
            aria-pressed={active}
            className={cls(
              'font-bold uppercase tracking-widest transition',
              size === 'sm' ? 'px-2 py-[3px] text-[9px]' : 'px-3.5 py-1.5 text-[11px]',
              active
                ? 'bg-white/10 text-white shadow-[inset_0_0_0_1px_rgba(225,29,72,0.45)]'
                : 'text-krypt-muted hover:bg-white/5 hover:text-white',
            )}
          >
            {m}
          </button>
        )
      })}
    </div>
  )
}

import type { Sport } from '../types'
import { SPORTS, SPORT_TAG } from '../types'
import { cls } from '../utils/format'

/** The all-sports pill strip (driven by SPORTS) — active pill gets the
 *  scout-red glow ring.
 *  Shared by the BOARDS strip and the INTEL sport selector. */
export default function SportPills({
  active,
  counts,
  onSelect,
}: {
  active: Sport
  counts?: Partial<Record<Sport, number>>
  onSelect: (s: Sport) => void
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {SPORTS.map((s) => {
        const isActive = s === active
        const count = counts?.[s]
        return (
          <button
            key={s}
            onClick={() => onSelect(s)}
            className={cls(
              'flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11px] font-bold tracking-widest transition',
              isActive
                ? 'border-scout-red/50 bg-white/10 text-white shadow-scout-glow ring-1 ring-scout-red/40'
                : 'border-white/10 bg-white/[0.03] text-krypt-muted hover:border-white/20 hover:bg-white/5 hover:text-white',
            )}
          >
            {SPORT_TAG[s]}
            {count !== undefined && (
              <span
                className={cls(
                  'min-w-[16px] rounded px-1 text-center font-mono text-[9px] font-semibold leading-4',
                  isActive
                    ? 'bg-scout-red/25 text-scout-red-bright'
                    : count > 0
                      ? 'bg-white/5 text-krypt-muted'
                      : 'bg-white/[0.03] text-krypt-muted/40',
                )}
              >
                {count}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}

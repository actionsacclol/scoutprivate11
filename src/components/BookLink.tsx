import type { KeyboardEvent, MouseEvent, ReactNode } from 'react'
import { ArrowRight, ExternalLink } from 'lucide-react'
import { bookName, openBook } from '../config/referrals'
import { cls } from '../utils/format'

function stopKeys(e: KeyboardEvent) {
  // Don't let Enter/Space bubble to card/row containers that toggle drawers.
  if (e.key === 'Enter' || e.key === ' ') e.stopPropagation()
}

/**
 * Generic clickable sportsbook wrapper — every book chip/badge/cell in the app
 * goes through this so clicks open the book (referral link when configured)
 * without toggling the surrounding card, row or drawer.
 */
export default function BookLink({
  book,
  className,
  children,
  title,
}: {
  book: string
  className?: string
  children: ReactNode
  title?: string
}) {
  const onClick = (e: MouseEvent) => {
    e.stopPropagation()
    openBook(book)
  }
  return (
    <button
      type="button"
      title={title ?? `Opens ${bookName(book)}`}
      onClick={onClick}
      onKeyDown={stopKeys}
      className={cls('group/book cursor-pointer text-left', className)}
    >
      {children}
      <ExternalLink
        size={8}
        aria-hidden
        className="ml-0.5 inline-block shrink-0 align-baseline opacity-0 transition-opacity group-hover/book:opacity-70"
      />
    </button>
  )
}

/** Gradient BET NOW call-to-action — opens the pick's book. */
export function BetNowButton({ book, className }: { book: string; className?: string }) {
  return (
    <button
      type="button"
      title={`Opens ${bookName(book)}`}
      onClick={(e) => {
        e.stopPropagation()
        openBook(book)
      }}
      onKeyDown={stopKeys}
      className={cls(
        'inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-lg bg-krypt-gradient px-2.5 py-1 text-[10px] font-bold uppercase tracking-widest text-white shadow-krypt-glow transition',
        'hover:brightness-110 active:scale-[0.98]',
        className,
      )}
    >
      BET NOW
      <ArrowRight size={10} />
    </button>
  )
}

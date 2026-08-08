import { ExternalLink, Gift, Heart } from 'lucide-react'
import { REFERRALS, bookName, openBook, type BookConfig } from '../config/referrals'
import { Page, Section } from './common'
import BookLink from './BookLink'
import { cls } from '../utils/format'

/** Two-letter mono avatar tile for a book. */
function BookAvatar({ book, large }: { book: BookConfig; large?: boolean }) {
  return (
    <span
      className={cls(
        'flex shrink-0 items-center justify-center rounded-xl border border-white/10 bg-white/5 font-mono font-bold text-white/90',
        large ? 'h-12 w-12 text-base' : 'h-9 w-9 text-xs',
      )}
      aria-hidden
    >
      {book.short.slice(0, 2)}
    </span>
  )
}

function ClaimButton({ book, large }: { book: BookConfig; large?: boolean }) {
  return (
    <button
      type="button"
      onClick={() => openBook(book.key)}
      title={`Opens ${bookName(book.key)}`}
      className={cls(
        'inline-flex items-center justify-center gap-2 rounded-xl bg-krypt-gradient font-semibold text-white shadow-krypt-glow transition hover:brightness-110 active:scale-[0.98]',
        large ? 'px-4 py-2.5 text-sm' : 'px-3 py-2 text-xs',
      )}
    >
      <Gift size={large ? 14 : 12} />
      CLAIM BONUS
      <ExternalLink size={large ? 12 : 10} className="opacity-70" />
    </button>
  )
}

/** One bonus card. Featured books get the scout-red spine + glow and a larger layout. */
function BonusCard({ book }: { book: BookConfig }) {
  const featured = !!book.featured
  return (
    <div
      className={cls(
        'relative flex flex-col overflow-hidden rounded-2xl border bg-krypt-panel/80 shadow-krypt-card transition',
        featured
          ? 'border-scout-red/30 p-5 shadow-scout-glow hover:border-scout-red/50'
          : 'border-white/10 p-4 hover:border-white/20',
      )}
    >
      {featured && (
        <span className="absolute bottom-4 left-0 top-4 w-[2px] rounded-r-full bg-scout-red/70" />
      )}
      <div className={cls('flex items-center gap-3', featured && 'pl-1.5')}>
        <BookAvatar book={book} large={featured} />
        <div className="min-w-0">
          <div className={cls('truncate font-bold text-white', featured ? 'text-base' : 'text-sm')}>
            {book.name}
          </div>
          <div className="font-mono text-[10px] uppercase tracking-wider text-krypt-muted/60">
            {book.short}
          </div>
        </div>
        {featured && (
          <span className="ml-auto rounded border border-scout-red/30 bg-scout-red/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest text-scout-red-bright">
            FEATURED
          </span>
        )}
      </div>

      <div className={cls('mt-3 flex-1', featured && 'pl-1.5')}>
        <div
          className={cls(
            'font-bold leading-snug text-white',
            featured ? 'text-xl' : 'text-base',
          )}
        >
          {book.bonusHeadline ?? 'Sign-up offer available'}
        </div>
        {book.bonusDetail && (
          <div className="mt-1.5 text-xs leading-relaxed text-krypt-muted">{book.bonusDetail}</div>
        )}
      </div>

      <div className={cls('mt-4', featured && 'pl-1.5')}>
        <ClaimButton book={book} large={featured} />
      </div>
    </div>
  )
}

/** BONUSES — sportsbook sign-up offers via the app's referral links. */
export default function BonusesPage() {
  const withOffer = REFERRALS.filter((b) => b.referralUrl || b.bonusHeadline)
  const featured = withOffer.filter((b) => b.featured)
  const standard = withOffer.filter((b) => !b.featured)
  const more = REFERRALS.filter((b) => !b.referralUrl && !b.bonusHeadline)

  return (
    <Page
      title="Bonuses"
      subtitle="Sign-up offers from the sportsbooks Scout tracks — claim them straight from here."
    >
      {/* Support banner */}
      <div className="border-krypt-gradient relative mb-6 flex items-center gap-3 rounded-2xl bg-krypt-panel/80 px-4 py-3 shadow-krypt-card">
        <Heart size={15} className="shrink-0 text-scout-red-bright" />
        <p className="text-sm text-white/90">
          These are referral links — <span className="font-semibold">Krypt Scout</span> may earn a
          commission when you sign up through them. That&apos;s what keeps it free forever, no ads.
        </p>
      </div>

      {withOffer.length === 0 ? (
        <Section>
          <div className="rounded-2xl border border-dashed border-white/10 bg-black/20 px-6 py-12 text-center">
            <div className="text-base font-semibold text-white">No offers configured yet</div>
            <div className="mt-2 text-sm text-krypt-muted">
              Offers appear here once referral links are added in{' '}
              <span className="font-mono text-[12px] text-white/70">src/config/referrals.ts</span>.
            </div>
          </div>
        </Section>
      ) : (
        <>
          {featured.length > 0 && (
            <Section>
              <div className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(340px,1fr))]">
                {featured.map((b) => (
                  <BonusCard key={b.key} book={b} />
                ))}
              </div>
            </Section>
          )}
          {standard.length > 0 && (
            <Section title={featured.length > 0 ? 'More offers' : undefined}>
              <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(260px,1fr))]">
                {standard.map((b) => (
                  <BonusCard key={b.key} book={b} />
                ))}
              </div>
            </Section>
          )}
        </>
      )}

      {/* Every other book Scout knows — still clickable, just no offer copy. */}
      {more.length > 0 && (
        <Section
          title="More books"
          description="No offer listed right now — links open each book's site."
        >
          <div className="flex flex-wrap gap-1.5">
            {more.map((b) => (
              <BookLink
                key={b.key}
                book={b.key}
                className="rounded-lg border border-white/5 bg-white/[0.02] px-2.5 py-1.5 text-[11px] font-semibold text-krypt-muted/70 transition hover:border-white/20 hover:bg-white/5 hover:text-white"
              >
                {b.name}
              </BookLink>
            ))}
          </div>
        </Section>
      )}

      {/* Disclaimer */}
      <footer className="mt-2 border-t border-white/5 pt-4 pb-2">
        <p className="text-[11px] leading-relaxed text-krypt-muted/60">
          21+ (18+/19+ in some regions) and physically present where the book is licensed. Offers
          are made by the sportsbooks, change often, and come with terms and conditions — always
          read them on the book&apos;s site. Nothing in this app is betting advice. If gambling
          stops being fun, stop: in the US call or text 1-800-GAMBLER.
        </p>
      </footer>
    </Page>
  )
}

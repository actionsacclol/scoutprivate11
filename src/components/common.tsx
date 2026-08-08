import type { ReactNode } from 'react';
import { cls } from '../utils/format';

export function Page({
  title,
  subtitle,
  actions,
  children,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col h-full">
      <div className="px-8 pt-7 pb-5 flex items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight text-white">{title}</h1>
          {subtitle && <p className="mt-1 text-sm text-krypt-muted">{subtitle}</p>}
        </div>
        {actions && <div className="flex items-center gap-2">{actions}</div>}
      </div>
      <div className="flex-1 overflow-auto px-8 pb-8">{children}</div>
    </div>
  );
}

export function Section({
  title,
  description,
  actions,
  children,
}: {
  title?: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="mb-8">
      {(title || actions) && (
        <div className="flex items-end justify-between gap-2 mb-3">
          <div>
            {title && (
              <h2 className="text-xs font-semibold uppercase tracking-[0.18em] text-krypt-muted">
                {title}
              </h2>
            )}
            {description && <p className="text-xs text-krypt-muted/80 mt-1">{description}</p>}
          </div>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function Card({
  className,
  children,
  hoverable,
  padded = true,
}: {
  className?: string;
  children: ReactNode;
  hoverable?: boolean;
  padded?: boolean;
}) {
  return (
    <div
      className={cls(
        'rounded-2xl border border-white/10 bg-krypt-panel/80 backdrop-blur-sm shadow-krypt-card',
        padded && 'p-5',
        hoverable && 'transition hover:border-white/20 hover:shadow-krypt-glow',
        className,
      )}
    >
      {children}
    </div>
  );
}

export function PrimaryButton({
  onClick,
  disabled,
  children,
  className,
  type = 'button',
}: {
  onClick?: () => void;
  disabled?: boolean;
  children: ReactNode;
  className?: string;
  type?: 'button' | 'submit';
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={cls(
        'inline-flex items-center justify-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold text-white shadow-krypt-glow bg-krypt-gradient transition',
        'hover:brightness-110 active:scale-[0.98]',
        'disabled:cursor-not-allowed disabled:opacity-50 disabled:shadow-none disabled:brightness-75',
        className,
      )}
    >
      {children}
    </button>
  );
}

export function GhostButton({
  onClick,
  disabled,
  children,
  className,
  destructive,
}: {
  onClick?: () => void;
  disabled?: boolean;
  children: ReactNode;
  className?: string;
  destructive?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={cls(
        'inline-flex items-center justify-center gap-2 rounded-xl border px-4 py-2.5 text-sm font-semibold transition',
        destructive
          ? 'border-rose-500/40 bg-rose-500/10 text-rose-200 hover:bg-rose-500/20'
          : 'border-white/10 bg-white/5 text-white/90 hover:bg-white/10 hover:border-white/20',
        'disabled:cursor-not-allowed disabled:opacity-50',
        className,
      )}
    >
      {children}
    </button>
  );
}

export function IconButton({
  onClick,
  title,
  children,
  active,
  destructive,
  disabled,
}: {
  onClick?: () => void;
  title?: string;
  children: ReactNode;
  active?: boolean;
  destructive?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      disabled={disabled}
      className={cls(
        'h-9 w-9 rounded-lg border flex items-center justify-center transition',
        active
          ? 'border-krypt-purple/50 bg-krypt-purple/15 text-white shadow-krypt-glow'
          : destructive
            ? 'border-rose-500/30 bg-rose-500/10 text-rose-200 hover:bg-rose-500/20 hover:text-rose-100'
            : 'border-white/10 bg-white/5 text-krypt-muted hover:bg-white/10 hover:text-white hover:border-white/20',
        'disabled:cursor-not-allowed disabled:opacity-40',
      )}
    >
      {children}
    </button>
  );
}

/** Compact strip-header button (REFRESH / DEMO…) — BOARDS + PICKS top strips. */
export function StripBtn({
  onClick,
  active,
  disabled,
  title,
  children,
}: {
  onClick: () => void;
  active?: boolean;
  disabled?: boolean;
  title?: string;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={cls(
        'flex items-center gap-1.5 rounded-lg border px-2 py-1 text-[10px] font-semibold tracking-wider transition',
        active
          ? 'border-scout-red/40 bg-scout-red/10 text-scout-red-bright'
          : 'border-white/10 bg-white/[0.03] text-krypt-muted hover:border-white/20 hover:bg-white/5 hover:text-white',
        'disabled:cursor-not-allowed disabled:opacity-40',
      )}
    >
      {children}
    </button>
  );
}

export function Empty({
  title,
  message,
  action,
}: {
  title: string;
  message?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-16 px-6 rounded-2xl border border-dashed border-white/10 bg-black/20">
      <div className="text-base font-semibold text-white">{title}</div>
      {message && <div className="text-sm text-krypt-muted mt-2 max-w-md">{message}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function Badge({
  children,
  tone = 'neutral',
}: {
  children: ReactNode;
  tone?: 'neutral' | 'success' | 'warn' | 'danger' | 'gradient';
}) {
  const styles: Record<string, string> = {
    neutral: 'border-white/10 bg-white/5 text-krypt-muted',
    success: 'border-emerald-400/30 bg-emerald-500/10 text-emerald-300',
    warn:    'border-amber-400/30 bg-amber-500/10 text-amber-300',
    danger:  'border-rose-400/30 bg-rose-500/10 text-rose-300',
    gradient:'border-purple-400/40 bg-krypt-purple/10 text-purple-200',
  };
  return (
    <span className={cls('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider', styles[tone])}>
      {children}
    </span>
  );
}

/** Section eyebrow with the scout-red accent tick — terminal section headers. */
export function Eyebrow({ children, sub }: { children: ReactNode; sub?: ReactNode }) {
  return (
    <div className="mb-2 flex items-baseline gap-2">
      <span className="relative -top-px inline-block h-2.5 w-0.5 shrink-0 self-center rounded-full bg-scout-red shadow-scout-glow" />
      <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-white/80">
        {children}
      </span>
      {sub && (
        <span className="truncate text-[10px] uppercase tracking-wider text-krypt-muted/70">
          {sub}
        </span>
      )}
    </div>
  );
}

/** Big-mono-number stat tile for summary rows. */
export function StatTile({
  label,
  value,
  sub,
  tone = 'neutral',
  help,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: 'neutral' | 'success' | 'danger' | 'accent';
  /** Plain-English tooltip explaining the stat abbreviation. */
  help?: string;
}) {
  const valueTone: Record<string, string> = {
    neutral: 'text-white',
    success: 'text-emerald-300',
    danger: 'text-rose-400',
    accent: 'text-scout-red-bright',
  };
  return (
    <div
      title={help}
      className="rounded-2xl border border-white/10 bg-krypt-panel/80 px-4 py-3 shadow-krypt-card"
    >
      <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
        {label}
      </div>
      <div className={cls('mt-1 font-mono text-2xl font-bold leading-none tracking-tight', valueTone[tone])}>
        {value}
      </div>
      {sub && <div className="mt-1.5 text-[10px] uppercase tracking-wider text-krypt-muted/70">{sub}</div>}
    </div>
  );
}

/** Compact on/off switch. */
export function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: () => void;
  label?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={onChange}
      className={cls(
        'relative h-5 w-9 shrink-0 rounded-full border transition',
        checked
          ? 'border-scout-red/60 bg-scout-red/30 shadow-scout-glow'
          : 'border-white/15 bg-white/5',
      )}
    >
      <span
        className={cls(
          'absolute top-0.5 h-3.5 w-3.5 rounded-full transition-all',
          checked ? 'left-[18px] bg-scout-red-bright' : 'left-0.5 bg-white/40',
        )}
      />
    </button>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="inline-flex items-center rounded-md border border-white/15 bg-white/5 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-white/80 shadow-[inset_0_-1px_0_rgba(255,255,255,0.08)]">
      {children}
    </kbd>
  );
}

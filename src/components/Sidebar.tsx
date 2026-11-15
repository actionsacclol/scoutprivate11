import { useState } from 'react';
import {
  BarChart3,
  Bot,
  ChevronDown,
  Gauge,
  CalendarDays,
  Cpu,
  FlaskConical,
  Gift,
  Info,
  Layers,
  NotebookPen,
  LayoutGrid,
  MessageCircle,
  Radar,
  Settings,
  SquareTerminal,
  Target,
  TrendingUp,
  Webhook,
  Zap,
} from 'lucide-react';
import { cls, openExternal } from '../utils/format';
import { useUiMode } from '../uiMode';

export type PageId =
  | 'dashboard'
  | 'today'
  | 'boards'
  | 'terminal'
  | 'picks'
  | 'parlay'
  | 'tracker'
  | 'autobet'
  | 'bonuses'
  | 'discord'
  | 'value'
  | 'perf'
  | 'backtest'
  | 'models'
  | 'intel'
  | 'agents'
  | 'settings'
  | 'about';

interface NavItem {
  id: PageId;
  label: string;
  icon: typeof LayoutGrid;
}

type GroupId = 'main' | 'betting' | 'research' | 'connect';

interface NavGroup {
  id: GroupId;
  /** Section heading; the first group has none. */
  label?: string;
  /** Folded away in SIMPLE until the user opens it. Never hidden: one click
   *  on the heading shows it, and the choice sticks. */
  foldedInSimple?: boolean;
  items: NavItem[];
}

// Eighteen pages in one flat list was the first thing a new user met. They
// are grouped by what you came to do, and SIMPLE folds the two groups a
// newcomer does not need on day one. Every page is still one click away in
// both modes; PRO opens everything.
const GROUPS: NavGroup[] = [
  {
    id: 'main',
    items: [
      { id: 'dashboard', label: 'DASHBOARD', icon: Gauge },
      { id: 'today', label: 'TODAY', icon: CalendarDays },
      { id: 'picks', label: 'PICKS', icon: Target },
      { id: 'boards', label: 'BOARDS', icon: LayoutGrid },
    ],
  },
  {
    id: 'betting',
    label: 'Betting',
    items: [
      { id: 'tracker', label: 'TRACKER', icon: NotebookPen },
      { id: 'parlay', label: 'PARLAY', icon: Layers },
      { id: 'autobet', label: 'AUTO-BET', icon: Zap },
      { id: 'bonuses', label: 'BONUSES', icon: Gift },
    ],
  },
  {
    id: 'research',
    label: 'Research',
    foldedInSimple: true,
    items: [
      { id: 'value', label: 'VALUE', icon: TrendingUp },
      { id: 'terminal', label: 'TERMINAL', icon: SquareTerminal },
      { id: 'intel', label: 'INTEL', icon: Radar },
      { id: 'perf', label: 'PERFORMANCE', icon: BarChart3 },
      { id: 'backtest', label: 'BACKTEST', icon: FlaskConical },
      { id: 'models', label: 'MODELS', icon: Cpu },
    ],
  },
  {
    id: 'connect',
    label: 'Connect',
    foldedInSimple: true,
    items: [
      { id: 'discord', label: 'DISCORD', icon: Webhook },
      { id: 'agents', label: 'AI AGENTS', icon: Bot },
    ],
  },
];

/** Pinned to the bottom, out of the working list. */
const FOOT: NavItem[] = [
  { id: 'settings', label: 'SETTINGS', icon: Settings },
  { id: 'about', label: 'ABOUT', icon: Info },
];

const FOLD_KEY = 'scout-nav-folds';

/** The user's own open/closed choices per group; a group they never touched
 *  follows the mode. */
function loadFolds(): Partial<Record<GroupId, boolean>> {
  try {
    const raw = JSON.parse(localStorage.getItem(FOLD_KEY) ?? '{}');
    return raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  } catch {
    return {};
  }
}

function NavButton({
  item: { id, label, icon: Icon },
  active,
  onNav,
}: {
  item: NavItem;
  active: boolean;
  onNav: (page: PageId) => void;
}) {
  return (
    <button
      data-tour={id}
      onClick={() => onNav(id)}
      className={cls(
        'relative flex w-full items-center gap-2.5 rounded-lg px-3 py-[7px] text-left text-xs font-semibold tracking-wider transition',
        active ? 'bg-white/10 text-white' : 'text-krypt-muted hover:bg-white/5 hover:text-white',
      )}
    >
      {active && (
        <span className="absolute bottom-1.5 left-0 top-1.5 w-0.5 rounded-r bg-scout-red shadow-scout-glow" />
      )}
      <Icon size={15} className={active ? 'text-scout-red-bright' : undefined} />
      {label}
    </button>
  );
}

export default function Sidebar({
  page,
  onNav,
}: {
  page: PageId;
  onNav: (page: PageId) => void;
}) {
  const mode = useUiMode();
  const [folds, setFolds] = useState(loadFolds);

  const folded = (g: NavGroup): boolean => {
    // The group holding the open page is always shown — nobody (including the
    // tour, which navigates before it spotlights a tab) lands on a page whose
    // tab they cannot see.
    if (g.items.some((it) => it.id === page)) return false;
    return folds[g.id] ?? (mode === 'simple' && !!g.foldedInSimple);
  };

  const toggle = (g: NavGroup) => {
    const next = { ...folds, [g.id]: !folded(g) };
    setFolds(next);
    try {
      localStorage.setItem(FOLD_KEY, JSON.stringify(next));
    } catch {
      /* storage unavailable — the fold still works for the session */
    }
  };

  return (
    <aside className="flex w-[200px] shrink-0 flex-col border-r border-white/10 bg-krypt-panel/60">
      <nav className="min-h-0 flex-1 overflow-y-auto px-2 pt-3">
        {GROUPS.map((g) => {
          const isFolded = folded(g);
          const holdsPage = g.items.some((it) => it.id === page);
          return (
            <div key={g.id} className={g.label ? 'mt-3' : undefined}>
              {g.label && (
                <button
                  onClick={() => toggle(g)}
                  disabled={holdsPage}
                  aria-expanded={!isFolded}
                  title={isFolded ? `Show ${g.label.toLowerCase()} pages` : undefined}
                  className={cls(
                    'mb-0.5 flex w-full items-center justify-between rounded-md px-3 py-1 text-left text-[9px] font-bold uppercase tracking-[0.22em] transition',
                    holdsPage
                      ? 'cursor-default text-krypt-muted/50'
                      : 'text-krypt-muted/50 hover:bg-white/5 hover:text-white',
                  )}
                >
                  <span>
                    {g.label}
                    {isFolded && <span className="ml-1.5 font-mono tracking-normal opacity-70">{g.items.length}</span>}
                  </span>
                  {!holdsPage && (
                    <ChevronDown size={11} className={cls('transition-transform', isFolded && '-rotate-90')} />
                  )}
                </button>
              )}
              {!isFolded && (
                <div className="space-y-0.5">
                  {g.items.map((item) => (
                    <NavButton key={item.id} item={item} active={page === item.id} onNav={onNav} />
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </nav>
      <div className="space-y-0.5 border-t border-white/10 px-2 py-2">
        {FOOT.map((item) => (
          <NavButton key={item.id} item={item} active={page === item.id} onNav={onNav} />
        ))}
      </div>
      <div className="space-y-2 border-t border-white/10 p-3">
        <div className="flex gap-2">
          <button
            onClick={() => openExternal('https://krypt.cc')}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-2 py-1.5 text-[10px] font-semibold text-white/90 transition hover:border-white/20 hover:bg-white/10"
          >
            krypt.cc
          </button>
          <button
            onClick={() => openExternal('https://discord.gg/muzFKR657F')}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-2 py-1.5 text-[10px] font-semibold text-white/90 transition hover:border-white/20 hover:bg-white/10"
          >
            <MessageCircle size={11} />
            Discord
          </button>
        </div>
        {/* Scout is the suite's licensed tool, so the suite's usual "Free &
            open source · no telemetry" footer line does not belong here. */}
        <p className="text-center text-[10px] leading-relaxed text-krypt-muted/70">Krypt Premium · no ads</p>
      </div>
    </aside>
  );
}

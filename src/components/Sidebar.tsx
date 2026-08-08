import {
  BarChart3,
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
  | 'settings'
  | 'about';

interface NavItem {
  id: PageId;
  label: string;
  icon: typeof LayoutGrid;
}

// One nav for both UI modes — every tab is always visible. SIMPLE/PRO still
// changes how individual pages render (card density, drawers), just not which
// tabs exist. TODAY leads the boards cluster: the cross-sport "what's on" view.
const NAV: NavItem[] = [
  { id: 'dashboard', label: 'DASHBOARD', icon: Gauge },
  { id: 'today', label: 'TODAY', icon: CalendarDays },
  { id: 'boards', label: 'BOARDS', icon: LayoutGrid },
  { id: 'terminal', label: 'TERMINAL', icon: SquareTerminal },
  { id: 'picks', label: 'PICKS', icon: Target },
  { id: 'parlay', label: 'PARLAY', icon: Layers },
  { id: 'tracker', label: 'TRACKER', icon: NotebookPen },
  { id: 'autobet', label: 'AUTO-BET', icon: Zap },
  { id: 'bonuses', label: 'BONUSES', icon: Gift },
  { id: 'discord', label: 'DISCORD', icon: Webhook },
  { id: 'value', label: 'VALUE', icon: TrendingUp },
  { id: 'perf', label: 'PERFORMANCE', icon: BarChart3 },
  { id: 'backtest', label: 'BACKTEST', icon: FlaskConical },
  { id: 'models', label: 'MODELS', icon: Cpu },
  { id: 'intel', label: 'INTEL', icon: Radar },
  { id: 'settings', label: 'SETTINGS', icon: Settings },
  { id: 'about', label: 'ABOUT', icon: Info },
];

export default function Sidebar({
  page,
  onNav,
}: {
  page: PageId;
  onNav: (page: PageId) => void;
}) {
  const nav = NAV;
  return (
    <aside className="flex w-[200px] shrink-0 flex-col border-r border-white/10 bg-krypt-panel/60">
      <nav className="flex-1 space-y-0.5 px-2 pt-3">
        {nav.map(({ id, label, icon: Icon }) => {
          const active = page === id;
          return (
            <button
              key={id}
              data-tour={id}
              onClick={() => onNav(id)}
              className={cls(
                'relative flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-xs font-semibold tracking-wider transition',
                active
                  ? 'bg-white/10 text-white'
                  : 'text-krypt-muted hover:bg-white/5 hover:text-white',
              )}
            >
              {active && (
                <span className="absolute bottom-1.5 left-0 top-1.5 w-0.5 rounded-r bg-scout-red shadow-scout-glow" />
              )}
              <Icon size={15} className={active ? 'text-scout-red-bright' : undefined} />
              {label}
            </button>
          );
        })}
      </nav>
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
        <p className="text-center text-[10px] leading-relaxed text-krypt-muted/70">
          Free &amp; open source
          <br />
          no ads, no telemetry
        </p>
      </div>
    </aside>
  );
}

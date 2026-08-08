import { HelpCircle, Minus, Square, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { BackendInfo } from '@shared/types';
import { resetTour } from '../prefs';
import { ModeSwitch, type UiMode } from '../uiMode';

const statusColor: Record<string, string> = {
  running: 'bg-scout-red-bright shadow-[0_0_8px_rgba(244,63,94,0.85)]',
  starting: 'bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.8)] animate-pulse-slow',
  restarting: 'bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.8)] animate-pulse-slow',
  crashed: 'bg-red-500 shadow-[0_0_8px_rgba(239,68,68,0.8)]',
  stopped: 'bg-white/20',
};

export default function TitleBar({
  backend,
  mode,
  onMode,
}: {
  backend: BackendInfo | null;
  mode: UiMode;
  onMode: (m: UiMode) => void;
}) {
  const [maximized, setMaximized] = useState(false);

  useEffect(() => {
    if (!window.scout) return;
    void window.scout.window.isMaximized().then(setMaximized);
    return window.scout.window.onMaximizeChange(setMaximized);
  }, []);

  // Under plain vite there is no supervisor — show a neutral dot.
  const status = window.scout ? backend?.status ?? 'stopped' : 'running';

  return (
    <div
      className="flex h-9 shrink-0 select-none items-center justify-between border-b border-white/10 bg-krypt-panel/80 pl-3"
      style={{ WebkitAppRegion: 'drag' } as React.CSSProperties}
    >
      <div className="flex items-center gap-2">
        <img
          src="./krypt.png"
          alt=""
          className="h-[18px] w-[18px]"
          style={{ filter: 'drop-shadow(0 0 6px rgba(225,29,72,0.75))' }}
          onError={(e) => {
            (e.target as HTMLImageElement).style.display = 'none';
          }}
        />
        <span className="text-xs font-bold tracking-[0.28em]">
          <span className="text-krypt-gradient">KRYPT</span> SCOUT
        </span>
        <span className="ml-3 flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-krypt-muted">
          <span className={`h-1.5 w-1.5 rounded-full ${statusColor[status] ?? 'bg-white/20'}`} />
          {status}
        </span>
        <span className="ml-3" style={{ WebkitAppRegion: 'no-drag' } as React.CSSProperties}>
          <ModeSwitch mode={mode} onMode={onMode} size="sm" />
        </span>
      </div>
      <div
        className="flex h-full"
        style={{ WebkitAppRegion: 'no-drag' } as React.CSSProperties}
      >
        {/* Help = replay the tab tour (TourGuide re-arms via prefs.toured). */}
        <button
          className="flex h-full w-11 items-center justify-center text-krypt-muted transition-colors hover:bg-white/5 hover:text-white"
          title="Lost? Replay the quick tour of the tabs"
          onClick={resetTour}
        >
          <HelpCircle size={14} />
        </button>
        {window.scout && (
          <>
          <button
            className="flex h-full w-11 items-center justify-center text-krypt-muted transition-colors hover:bg-white/5 hover:text-white"
            onClick={() => window.scout?.window.minimize()}
          >
            <Minus size={14} />
          </button>
          <button
            className="flex h-full w-11 items-center justify-center text-krypt-muted transition-colors hover:bg-white/5 hover:text-white"
            onClick={() => window.scout?.window.maximize()}
          >
            <Square size={maximized ? 11 : 12} />
          </button>
          <button
            className="flex h-full w-11 items-center justify-center text-krypt-muted transition-colors hover:bg-scout-red/80 hover:text-white"
            onClick={() => window.scout?.window.close()}
          >
            <X size={15} />
          </button>
          </>
        )}
      </div>
    </div>
  );
}

/** The KRYPT SCOUT ANSI-shadow marquee — glowing red, breathing, with a rare
 * subtle flicker. Shared by the DASHBOARD; the TERMINAL keeps its own copy
 * inside its scoped stylesheet. Every row is exactly 96 columns.
 */
const BANNER = String.raw`
██╗  ██╗ ██████╗  ██╗   ██╗ ██████╗  ████████╗   ███████╗  ██████╗  ██████╗  ██╗   ██╗ ████████╗
██║ ██╔╝ ██╔══██╗ ╚██╗ ██╔╝ ██╔══██╗ ╚══██╔══╝   ██╔════╝ ██╔════╝ ██╔═══██╗ ██║   ██║ ╚══██╔══╝
█████╔╝  ██████╔╝  ╚████╔╝  ██████╔╝    ██║      ███████╗ ██║      ██║   ██║ ██║   ██║    ██║
██╔═██╗  ██╔══██╗   ╚██╔╝   ██╔═══╝     ██║      ╚════██║ ██║      ██║   ██║ ██║   ██║    ██║
██║  ██╗ ██║  ██║    ██║    ██║         ██║      ███████║ ╚██████╗ ╚██████╔╝ ╚██████╔╝    ██║
╚═╝  ╚═╝ ╚═╝  ╚═╝    ╚═╝    ╚═╝         ╚═╝      ╚══════╝  ╚═════╝  ╚═════╝   ╚═════╝     ╚═╝`.slice(1)

const CSS = `
@keyframes kb-breathe {
  0%, 100% { text-shadow: 0 0 6px rgba(225,29,72,.85), 0 0 18px rgba(225,29,72,.5); }
  50% { text-shadow: 0 0 8px rgba(225,29,72,.95), 0 0 26px rgba(225,29,72,.7); }
}
@keyframes kb-flick {
  0%, 96.4%, 100% { opacity: 1; }
  96.6%, 96.9% { opacity: .82; }
}
.kb-banner {
  color: #FB7185;
  text-shadow: 0 0 6px rgba(225,29,72,.85), 0 0 18px rgba(225,29,72,.55);
  animation: kb-breathe 4s ease-in-out infinite, kb-flick 7s steps(1, end) infinite;
}
@media (prefers-reduced-motion: reduce) { .kb-banner { animation: none; } }
`

export default function AsciiBanner({ className = '' }: { className?: string }) {
  return (
    <div className={`relative flex justify-center overflow-hidden ${className}`}>
      <style>{CSS}</style>
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            'radial-gradient(60% 120% at 50% 50%, rgba(225,29,72,.10), transparent 70%)',
        }}
      />
      <pre
        aria-label="KRYPT SCOUT"
        className="kb-banner select-none font-mono"
        style={{ fontSize: 'clamp(6px, 0.85vw, 9px)', lineHeight: 1.15 }}
      >
        {BANNER}
      </pre>
    </div>
  )
}

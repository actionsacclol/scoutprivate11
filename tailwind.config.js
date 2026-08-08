/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Krypt brand tokens — see guidelines.md §3. Verbatim from the suite.
        'krypt-black': '#000000',
        'krypt-void': '#0A0A0F',
        'krypt-surface': '#141419',
        'krypt-panel': '#11111A',
        'krypt-muted': '#A1A1AA',
        'krypt-indigo': '#6366F1',
        'krypt-purple': '#A855F7',
        'krypt-pink': '#EC4899',
        // Scout accent namespace — the tool's own signal color.
        'scout-red': '#E11D48',
        'scout-red-bright': '#F43F5E',
        'scout-red-dim': 'rgba(225,29,72,0.15)',
      },
      fontFamily: {
        sans: ['"Chakra Petch"', 'Inter', 'system-ui', 'sans-serif'],
        pixel: ['"Press Start 2P"', 'monospace'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      boxShadow: {
        'krypt-glow': '0 0 24px rgba(168, 85, 247, 0.35)',
        'krypt-card': '0 8px 30px rgba(0, 0, 0, 0.45)',
        'scout-glow': '0 0 20px rgba(225, 29, 72, 0.35)',
      },
      backgroundImage: {
        'krypt-gradient':
          'linear-gradient(90deg, #6366F1 0%, #A855F7 50%, #EC4899 100%)',
        'krypt-radial':
          'radial-gradient(700px circle at 18% 0%, rgba(168,85,247,0.18), transparent 60%)',
        'scout-scanlines':
          'repeating-linear-gradient(to bottom, rgba(255,255,255,0.02) 0, rgba(255,255,255,0.02) 1px, transparent 1px, transparent 3px)',
      },
      animation: {
        'gradient-x': 'gradient-x 8s ease infinite',
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'fade-in': 'fade-in 0.25s ease-out both',
        'pop-in': 'pop-in 0.18s cubic-bezier(0.2, 0.9, 0.3, 1.2) both',
      },
      keyframes: {
        'gradient-x': {
          '0%, 100%': { backgroundPosition: '0% 50%' },
          '50%': { backgroundPosition: '100% 50%' },
        },
        'fade-in': {
          from: { opacity: 0, transform: 'translateY(4px)' },
          to: { opacity: 1, transform: 'translateY(0)' },
        },
        'pop-in': {
          from: { opacity: 0, transform: 'scale(0.95)' },
          to: { opacity: 1, transform: 'scale(1)' },
        },
      },
    },
  },
  plugins: [],
};

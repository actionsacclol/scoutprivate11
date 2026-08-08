import { useState } from 'react'
import { KeyRound } from 'lucide-react'
import { activateLicense, type LicenseStatus } from '../api'

/** Full-screen activation wall shown when the app is not licensed. The whole
 *  app is gated (backend enforces it too — see api.main._license_gate), so this
 *  is the only thing an unlicensed user can interact with. */
export default function LicenseGate({
  status,
  onActivated,
}: {
  status: LicenseStatus | null
  onActivated: (s: LicenseStatus) => void
}) {
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const activate = async () => {
    if (!key.trim()) return
    setBusy(true)
    setError(null)
    try {
      onActivated(await activateLicense(key.trim()))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Activation failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-0 flex-1 items-center justify-center bg-krypt-radial p-6">
      <div className="w-full max-w-md rounded-2xl border border-white/10 bg-krypt-void/60 p-8 shadow-2xl backdrop-blur">
        <div className="mb-6 flex items-center gap-3">
          <div className="grid h-10 w-10 place-items-center rounded-xl bg-scout-red/15 text-scout-red-bright">
            <KeyRound size={20} />
          </div>
          <div>
            <h1 className="text-lg font-bold text-white">Krypt Scout</h1>
            <p className="text-xs text-krypt-muted">Activate to unlock</p>
          </div>
        </div>

        <p className="mb-5 text-sm text-krypt-muted">
          This is licensed software. Enter your key to activate it on this machine. Generate a key on the{' '}
          <span className="text-white">Krypt dashboard → Utility → Krypt Premium</span> (premium members only).
        </p>

        <label className="mb-1.5 block text-[11px] font-medium uppercase tracking-wider text-krypt-muted">
          License key
        </label>
        <input
          value={key}
          onChange={(e) => setKey(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void activate()
          }}
          placeholder="KRYPT-XXXX-XXXX-XXXX-XXXX"
          spellCheck={false}
          autoFocus
          className="w-full rounded-xl border border-white/10 bg-black/40 px-3 py-2.5 font-mono text-sm text-white placeholder:font-sans placeholder:text-krypt-muted/40 focus:border-scout-red/40 focus:outline-none focus:ring-1 focus:ring-scout-red/30"
        />

        {error && <p className="mt-2 text-xs text-rose-300">{error}</p>}

        <button
          onClick={() => void activate()}
          disabled={busy || !key.trim()}
          className="mt-4 w-full rounded-xl bg-gradient-to-r from-scout-red to-scout-red-bright px-4 py-2.5 text-sm font-semibold text-white shadow-lg transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? 'Activating…' : 'Activate'}
        </button>

        <p className="mt-4 text-center text-[11px] text-krypt-muted/70">
          {status?.reason === 'expired_or_invalid'
            ? 'Your license lapsed or was deactivated. Re-activate to continue.'
            : 'Your money on Polymarket is unaffected — the app only tracks it.'}
        </p>
      </div>
    </div>
  )
}

// Idempotent: makes sure the Python venv exists and the backend deps are
// installed. Used by `predev` so first-time `npm run dev` Just Works.
import { ensureVenv } from './python-utils.mjs';

try {
  ensureVenv();
  console.log('>> Python backend ready.');
} catch (e) {
  console.error('!! Python setup failed:', e.message);
  // In CI a failed install is a failed build. Exiting 0 here hid a broken
  // `pip install` on the Windows runner: this step went green and the next
  // one died with "No module named pytest", nowhere near the real error.
  if (process.env.CI) process.exit(1);
  console.error(
    'You can still run the app in renderer-only mode; the scouting backend\n' +
    '   will not start until Python is available.',
  );
  // Non-fatal — let `npm run dev` proceed; the UI will surface the error.
  process.exit(0);
}

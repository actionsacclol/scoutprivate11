import RPC from 'discord-rpc';

/**
 * Discord Rich Presence — best-effort, never blocking.
 *
 * Per guidelines.md §8 every Krypt tool advertises in the user's
 * profile while running.
 *
 * Don't ever throw out of here. If Discord isn't running, RPC just
 * fails silently — that's fine.
 */

const CLIENT_ID = '1495323918234423406';

const DEFAULT_BUTTONS: Array<{ label: string; url: string }> = [
  { label: 'Free Tools', url: 'https://krypt.cc/tools' },
  { label: 'Krypt.cc', url: 'https://discord.gg/muzFKR657F' },
];

let client: RPC.Client | null = null;
let connected = false;
let desired = false;
let last: { state: string; details: string } | null = null;
let startedAt = Date.now();

export async function startDiscordRpc(): Promise<void> {
  desired = true;
  if (connected) return;
  try {
    client = new RPC.Client({ transport: 'ipc' });
    client.on('ready', () => {
      connected = true;
      startedAt = Date.now();
      pushActivity();
    });
    client.on('disconnected', () => {
      connected = false;
    });
    await client.login({ clientId: CLIENT_ID });
  } catch (err) {
    // Discord may simply not be running. Not an error.
    // eslint-disable-next-line no-console
    console.warn('[discord] login skipped:', (err as Error)?.message);
    connected = false;
  }
}

export function stopDiscordRpc(): void {
  desired = false;
  try {
    client?.destroy();
  } catch {
    /* ignore */
  }
  client = null;
  connected = false;
  last = null;
}

export function setActivity(details: string, state: string): void {
  last = { details, state };
  pushActivity();
}

function pushActivity(): void {
  if (!connected || !client || !desired) return;
  const payload = last ?? { details: 'Scouting the boards', state: 'krypt.cc/tools/scout' };
  client
    .setActivity({
      details: payload.details,
      state: payload.state,
      largeImageKey: 'krypt',
      largeImageText: 'Krypt Scout',
      startTimestamp: startedAt,
      instance: false,
      buttons: DEFAULT_BUTTONS,
    })
    .catch(() => {
      /* best-effort */
    });
}

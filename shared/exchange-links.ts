// The publisher's sign-up (referral) links for the three exchanges — the one
// place to change them. Used by the app (src/config/referrals.ts: BONUSES,
// onboarding, every "sign up" / "open" button) and by the Discord post
// (electron/system/discord-picks.ts), so the two can never disagree.

export const EXCHANGE_SIGNUP = {
  polymarket: 'https://polymarket.com?via=yuhgo',
  kalshi: 'https://kalshi.com/t/c62uf1c4',
  polymarket_us: 'https://polymarket.us/join/yuhgo',
} as const;

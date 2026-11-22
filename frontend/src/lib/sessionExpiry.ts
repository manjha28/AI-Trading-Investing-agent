export type SessionBanner = { level: 'expired' | 'soon'; text: string } | null;

/** A warning when the Dhan session is expired or has less than 60 minutes left; nothing when it is fine or unknown (unknown is not "fine": the caller shows "expiry unknown"). */
export function sessionBanner(expiresAt: string | null | undefined, now: number = Date.now(), warnMinutes = 60): SessionBanner {
  if (!expiresAt) return null;
  const t = new Date(expiresAt).getTime();
  if (Number.isNaN(t)) return null;
  const minutes = Math.floor((t - now) / 60000);
  if (minutes <= 0) return { level: 'expired', text: 'Your Dhan session has expired. Reconnect Dhan to get live data, history and holdings.' };
  if (minutes < warnMinutes) return { level: 'soon', text: `Your Dhan session expires in ${minutes} minute${minutes === 1 ? '' : 's'}. Reconnect Dhan soon to avoid a gap in data.` };
  return null;
}

export interface ExpiringSession {
  expiresAt: string;
}

export function isSessionExpired(session: ExpiringSession, now: Date): boolean {
  return new Date(session.expiresAt).getTime() <= now.getTime();
}

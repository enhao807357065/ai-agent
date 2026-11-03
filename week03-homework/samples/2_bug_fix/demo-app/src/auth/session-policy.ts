export interface ExpiringSession {
  expiresAt: string;
}

export function isSessionExpired(session: ExpiringSession, now: Date): boolean {
  return now.getTime() >= new Date(session.expiresAt).getTime();
}

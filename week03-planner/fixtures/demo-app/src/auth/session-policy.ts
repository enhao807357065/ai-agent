export interface ExpiringSession {
  expiresAt: string;
}

export function isSessionExpired(session: ExpiringSession, now: Date): boolean {
  const expiresAt = Date.parse(session.expiresAt);
  return now.getTime() >= expiresAt;
}

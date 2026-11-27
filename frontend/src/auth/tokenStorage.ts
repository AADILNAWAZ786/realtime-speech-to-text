/**
 * Access-token persistence. MVP choice (agreed in the architecture plan): localStorage.
 * Trade-off: readable by any script on the page, so XSS would expose it; a later upgrade is
 * an httpOnly cookie. The token is stored with its expiry and dropped once expired.
 */

const KEY = "stt.auth";

// Fallback when localStorage is unavailable (e.g. disabled storage): lasts for this tab.
let memoryValue: string | null = null;

interface StoredToken {
  token: string;
  expiresAt: number; // epoch ms
}

function read(): StoredToken | null {
  try {
    let raw: string | null;
    try {
      raw = window.localStorage.getItem(KEY);
    } catch {
      raw = memoryValue;
    }
    raw ??= memoryValue;
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredToken>;
    if (typeof parsed.token !== "string" || typeof parsed.expiresAt !== "number") return null;
    return { token: parsed.token, expiresAt: parsed.expiresAt };
  } catch {
    return null; // corrupt value
  }
}

export const tokenStorage = {
  get(now: number = Date.now()): string | null {
    const stored = read();
    if (!stored) return null;
    if (stored.expiresAt <= now) {
      tokenStorage.clear();
      return null;
    }
    return stored.token;
  },
  set(token: string, expiresInSeconds: number, now: number = Date.now()): void {
    const value = JSON.stringify({ token, expiresAt: now + expiresInSeconds * 1000 });
    memoryValue = null;
    try {
      window.localStorage.setItem(KEY, value);
    } catch {
      memoryValue = value; // storage unavailable: keep the session for this tab only
    }
  },
  clear(): void {
    memoryValue = null;
    try {
      window.localStorage.removeItem(KEY);
    } catch {
      // ignore
    }
  },
  key: KEY,
};

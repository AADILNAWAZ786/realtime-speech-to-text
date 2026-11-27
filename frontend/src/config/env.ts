export class ConfigError extends Error {
  override name = "ConfigError";
}

/** Validate the API base URL from VITE_API_BASE_URL (never hardcoded). */
export function parseApiBaseUrl(value: string | undefined): string {
  const raw = value?.trim();
  if (!raw) {
    throw new ConfigError(
      "VITE_API_BASE_URL is not set. Copy frontend/.env.example to frontend/.env.",
    );
  }
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new ConfigError(`VITE_API_BASE_URL is not a valid URL: "${raw}"`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new ConfigError("VITE_API_BASE_URL must start with http:// or https://");
  }
  return url.toString().replace(/\/+$/, "");
}

export function loadConfig(env: ImportMetaEnv = import.meta.env): { apiBaseUrl: string } {
  return { apiBaseUrl: parseApiBaseUrl(env.VITE_API_BASE_URL) };
}

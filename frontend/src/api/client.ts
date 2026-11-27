import type { ApiErrorBody, ApiValidationIssue } from "./types";

type QueryValue = string | number | boolean | null | undefined;

export interface RequestOptions {
  body?: unknown;
  query?: Record<string, QueryValue>;
  /** Attach the Bearer token (default true). */
  auth?: boolean;
  signal?: AbortSignal;
}

const STATUS_MESSAGES: Record<number, string> = {
  400: "The request could not be processed.",
  401: "Please sign in to continue.",
  403: "You do not have access to this resource.",
  404: "Not found.",
  409: "This conflicts with existing data.",
  422: "Please check the highlighted fields.",
  429: "Too many attempts. Please wait and try again.",
  500: "Something went wrong on the server. Please try again.",
  502: "The server is unreachable right now. Please try again.",
  503: "The service is temporarily unavailable. Please try again.",
  504: "The server took too long to respond. Please try again.",
};

/** A failed API call, normalised from the backend's {detail, code, errors} format. */
export class ApiError extends Error {
  override name = "ApiError";
  readonly status: number;
  readonly code: string;
  /** Field name -> first validation message (from 422 responses). */
  readonly fieldErrors: Readonly<Record<string, string>>;
  readonly retryAfterSeconds: number | null;
  readonly requestId: string | null;

  constructor(init: {
    status: number;
    code: string;
    message: string;
    fieldErrors?: Record<string, string>;
    retryAfterSeconds?: number | null;
    requestId?: string | null;
  }) {
    super(init.message);
    this.status = init.status;
    this.code = init.code;
    this.fieldErrors = init.fieldErrors ?? {};
    this.retryAfterSeconds = init.retryAfterSeconds ?? null;
    this.requestId = init.requestId ?? null;
  }

  get isNetworkError(): boolean {
    return this.status === 0;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }
}

function humanise(message: string): string {
  return message.replace(/^Value error, /, "");
}

/** Map pydantic issues to {field: message}; loc is e.g. ["body", "email"]. */
export function toFieldErrors(issues: ApiValidationIssue[] | undefined): Record<string, string> {
  const result: Record<string, string> = {};
  for (const issue of issues ?? []) {
    const field = [...issue.loc].reverse().find((part) => typeof part === "string");
    const key = field && field !== "body" && field !== "query" ? field : "_form";
    result[key] ??= humanise(issue.msg);
  }
  return result;
}

function isErrorBody(value: unknown): value is ApiErrorBody {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { detail?: unknown }).detail === "string" &&
    typeof (value as { code?: unknown }).code === "string"
  );
}

export interface ApiClientOptions {
  baseUrl: string;
  getToken: () => string | null;
  /** Called when an authenticated request is rejected with 401 (expired/revoked token). */
  onUnauthorized?: () => void;
  fetchImpl?: typeof fetch;
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly getToken: () => string | null;
  private readonly onUnauthorized: (() => void) | undefined;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ApiClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.getToken = options.getToken;
    this.onUnauthorized = options.onUnauthorized;
    this.fetchImpl = options.fetchImpl ?? ((...args) => fetch(...args));
  }

  url(path: string, query?: Record<string, QueryValue>): string {
    const url = new URL(`${this.baseUrl}${path.startsWith("/") ? path : `/${path}`}`);
    for (const [key, value] of Object.entries(query ?? {})) {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
    return url.toString();
  }

  async request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    const token = options.auth === false ? null : this.getToken();
    if (token) headers.Authorization = `Bearer ${token}`;

    let response: Response;
    try {
      response = await this.fetchImpl(this.url(path, options.query), {
        method,
        headers,
        body: options.body === undefined ? null : JSON.stringify(options.body),
        signal: options.signal ?? null,
      });
    } catch (error) {
      // Cancelled by the caller (e.g. TanStack Query on unmount): not a network failure.
      if (options.signal?.aborted || (error as { name?: unknown } | null)?.name === "AbortError") {
        throw error;
      }
      throw new ApiError({
        status: 0,
        code: "network_error",
        message: "Cannot reach the server. Check your connection and try again.",
      });
    }

    if (response.status === 204) return undefined as T;

    const payload: unknown = await response.json().catch(() => null);
    if (response.ok) return payload as T;

    if (response.status === 401 && token) this.onUnauthorized?.();

    const body = isErrorBody(payload) ? payload : null;
    const retryAfter = Number(response.headers.get("Retry-After"));
    throw new ApiError({
      status: response.status,
      code: body?.code ?? `http_${response.status}`,
      message:
        body?.detail ??
        STATUS_MESSAGES[response.status] ??
        `Request failed (HTTP ${response.status}).`,
      fieldErrors: toFieldErrors(body?.errors),
      retryAfterSeconds: Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
      requestId: response.headers.get("X-Request-ID"),
    });
  }

  get<T>(path: string, options?: Omit<RequestOptions, "body">): Promise<T> {
    return this.request<T>("GET", path, options);
  }

  post<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>("POST", path, { ...options, body });
  }

  patch<T>(path: string, body: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>("PATCH", path, { ...options, body });
  }

  delete(path: string, options?: RequestOptions): Promise<void> {
    return this.request<undefined>("DELETE", path, options);
  }
}

/** User-facing message for any error thrown by a request or mutation. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429 && error.retryAfterSeconds) {
      return `${error.message} Try again in ${error.retryAfterSeconds} s.`;
    }
    return error.message;
  }
  return "Something went wrong. Please try again.";
}

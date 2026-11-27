import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError, errorMessage, toFieldErrors } from "./client";

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function client(response: Response | { reject: Error }, token: string | null = "tok") {
  const fetchImpl = vi.fn<typeof fetch>(() =>
    response instanceof Response ? Promise.resolve(response) : Promise.reject(response.reject),
  );
  const onUnauthorized = vi.fn();
  const api = new ApiClient({
    baseUrl: "http://api.test/api/v1/",
    getToken: () => token,
    onUnauthorized,
    fetchImpl,
  });
  return { api, fetchImpl, onUnauthorized };
}

async function caught(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof ApiError) return error;
    throw error;
  }
  throw new Error("expected the request to fail");
}

describe("ApiClient", () => {
  it("builds URLs from the base URL and skips empty query values", () => {
    const { api } = client(jsonResponse(200, {}));
    expect(api.url("/transcriptions", { page: 2, q: "", status: undefined, source: null })).toBe(
      "http://api.test/api/v1/transcriptions?page=2",
    );
    expect(api.url("auth/me")).toBe("http://api.test/api/v1/auth/me");
  });

  it("sends JSON with the bearer token and parses the response", async () => {
    const { api, fetchImpl } = client(jsonResponse(201, { id: "1" }));
    await expect(api.post("/transcriptions", { text: "hi" })).resolves.toEqual({ id: "1" });
    const [, init] = fetchImpl.mock.calls[0]!;
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe('{"text":"hi"}');
    expect(init?.headers).toMatchObject({
      Authorization: "Bearer tok",
      "Content-Type": "application/json",
    });
  });

  it("omits the token for unauthenticated calls", async () => {
    const { api, fetchImpl } = client(jsonResponse(200, {}));
    await api.post("/auth/login", { email: "a@b.co", password: "x" }, { auth: false });
    expect(fetchImpl.mock.calls[0]![1]?.headers).not.toHaveProperty("Authorization");
  });

  it("returns undefined for 204", async () => {
    const { api } = client(new Response(null, { status: 204 }));
    await expect(api.delete("/transcriptions/1")).resolves.toBeUndefined();
  });

  it("turns the backend error body into an ApiError", async () => {
    const { api } = client(
      jsonResponse(
        404,
        { detail: "Transcription not found", code: "transcription_not_found" },
        {
          "X-Request-ID": "req-1",
        },
      ),
    );
    const error = await caught(api.get("/transcriptions/x"));
    expect(error).toMatchObject({
      status: 404,
      code: "transcription_not_found",
      message: "Transcription not found",
      requestId: "req-1",
    });
  });

  it("maps 422 issues to field errors", async () => {
    const { api } = client(
      jsonResponse(422, {
        detail: "Request validation failed",
        code: "validation_error",
        errors: [
          { loc: ["body", "password"], msg: "String should have at least 8 characters", type: "x" },
          { loc: ["body"], msg: "Value error, Provide at least one of: title, text", type: "x" },
        ],
      }),
    );
    const error = await caught(api.patch("/transcriptions/1", {}));
    expect(error.fieldErrors).toEqual({
      password: "String should have at least 8 characters",
      _form: "Provide at least one of: title, text",
    });
  });

  it("falls back to a status message for non-JSON errors", async () => {
    const { api } = client(new Response("<html>Bad gateway</html>", { status: 502 }));
    const error = await caught(api.get("/auth/me"));
    expect(error.code).toBe("http_502");
    expect(error.message).toMatch(/server/i);
  });

  it("reports network failures as status 0", async () => {
    const { api } = client({ reject: new TypeError("Failed to fetch") });
    const error = await caught(api.get("/auth/me"));
    expect(error.isNetworkError).toBe(true);
    expect(error.message).toMatch(/cannot reach the server/i);
  });

  it("rethrows aborts unchanged", async () => {
    const { api } = client({ reject: new DOMException("aborted", "AbortError") });
    await expect(api.get("/auth/me")).rejects.toMatchObject({ name: "AbortError" });
  });

  it("notifies on 401 only when a token was sent", async () => {
    const body = { detail: "Invalid or expired token", code: "invalid_token" };
    const withToken = client(jsonResponse(401, body));
    expect((await caught(withToken.api.get("/auth/me"))).isUnauthorized).toBe(true);
    expect(withToken.onUnauthorized).toHaveBeenCalledOnce();

    const login = client(jsonResponse(401, { detail: "Incorrect", code: "invalid_credentials" }));
    await caught(login.api.post("/auth/login", {}, { auth: false }));
    expect(login.onUnauthorized).not.toHaveBeenCalled();
  });

  it("reads Retry-After for rate limiting", async () => {
    const { api } = client(
      jsonResponse(
        429,
        { detail: "Too many requests", code: "rate_limited" },
        { "Retry-After": "30" },
      ),
    );
    const error = await caught(api.post("/auth/login", {}, { auth: false }));
    expect(error.retryAfterSeconds).toBe(30);
    expect(errorMessage(error)).toBe("Too many requests Try again in 30 s.");
  });
});

describe("toFieldErrors / errorMessage", () => {
  it("keeps the first message per field", () => {
    expect(
      toFieldErrors([
        { loc: ["body", "email"], msg: "first", type: "x" },
        { loc: ["body", "email"], msg: "second", type: "x" },
      ]),
    ).toEqual({ email: "first" });
    expect(toFieldErrors(undefined)).toEqual({});
  });

  it("has a generic message for unknown errors", () => {
    expect(errorMessage(new Error("boom"))).toBe("Something went wrong. Please try again.");
  });
});

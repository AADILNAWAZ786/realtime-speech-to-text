/**
 * In-memory stand-in for the FastAPI backend, used ONLY by the test suite (MSW).
 * It follows the real contracts: paths, status codes, error body {detail, code, errors?},
 * owner scoping, pagination maths and the list/detail response shapes.
 */
import { http, HttpResponse, type JsonBodyType } from "msw";

import type { Segment, Transcription, TranscriptionSummary, User } from "../api/types";

export const API = "http://api.test/api/v1";

interface StoredUser extends User {
  password: string;
}

type StoredTranscription = Omit<Transcription, "segments"> & {
  owner_id: string;
  segments: Segment[];
};

function error(status: number, detail: string, code: string, errors?: unknown) {
  return HttpResponse.json({ detail, code, ...(errors ? { errors } : {}) }, { status });
}

function validation(field: string, msg: string) {
  return error(422, "Request validation failed", "validation_error", [
    { loc: ["body", field], msg, type: "value_error" },
  ]);
}

let counter = 0;
const uuid = () => `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`;

export class FakeBackend {
  users = new Map<string, StoredUser>();
  tokens = new Map<string, string>(); // token -> user id
  transcriptions: StoredTranscription[] = [];
  /** Requests seen, for assertions ("METHOD /path?query"). */
  requests: string[] = [];
  /** JSON bodies of POST/PATCH requests, in order. */
  bodies: unknown[] = [];
  /** When set, every request fails like an unreachable/broken server. */
  failWith: "network" | 500 | null = null;
  /** Limit `failWith` to request lines starting with this ("GET /transcriptions"). */
  failOnly: string | null = null;
  private clock = Date.parse("2026-11-20T10:00:00Z");

  private tick(): string {
    this.clock += 60_000;
    return new Date(this.clock).toISOString();
  }

  addUser(email: string, password: string, displayName: string | null = null): StoredUser {
    const user: StoredUser = {
      id: uuid(),
      email: email.toLowerCase(),
      display_name: displayName,
      created_at: this.tick(),
      password,
    };
    this.users.set(user.email, user);
    return user;
  }

  /** Issue a token for a user, as POST /auth/login would. */
  issueToken(user: StoredUser): string {
    const token = `token-${uuid()}`;
    this.tokens.set(token, user.id);
    return token;
  }

  addTranscription(
    owner: StoredUser,
    data: Partial<StoredTranscription> & { title: string; text: string },
  ): StoredTranscription {
    const now = this.tick();
    const record: StoredTranscription = {
      id: uuid(),
      owner_id: owner.id,
      speech_mode: "auto",
      script_preference: "as_recognised",
      source: "manual",
      status: "completed",
      detected_language: null,
      duration_ms: null,
      is_edited: false,
      decode_strategy: null,
      model_name: null,
      created_at: now,
      updated_at: now,
      segments: [],
      ...data,
    };
    this.transcriptions.push(record);
    return record;
  }

  private userFor(request: Request): StoredUser | Response {
    const header = request.headers.get("Authorization");
    if (!header?.startsWith("Bearer ")) return error(401, "Not authenticated", "not_authenticated");
    const userId = this.tokens.get(header.slice("Bearer ".length));
    const user = [...this.users.values()].find((u) => u.id === userId);
    return user ?? error(401, "Invalid or expired token", "invalid_token");
  }

  private static publicUser({ password: _password, ...user }: StoredUser): User {
    return user;
  }

  private static detail(t: StoredTranscription, withSegments: boolean): Transcription {
    const { owner_id: _owner, segments, ...rest } = t;
    return { ...rest, segments: withSegments ? segments : null };
  }

  private static summary(t: StoredTranscription): TranscriptionSummary {
    const { owner_id: _o, segments: _s, text, decode_strategy: _d, model_name: _m, ...rest } = t;
    return { ...rest, preview: text.slice(0, 200) };
  }

  handlers() {
    const guard = (request: Request) => {
      const url = new URL(request.url);
      const line = `${request.method} ${url.pathname.replace("/api/v1", "")}${url.search}`;
      this.requests.push(line);
      if (this.failOnly && !line.startsWith(this.failOnly)) return null;
      if (this.failWith === "network") return HttpResponse.error();
      if (this.failWith === 500) return error(500, "Internal server error", "internal_error");
      return null;
    };
    const owned = (user: StoredUser, id: string) =>
      this.transcriptions.find((t) => t.id === id && t.owner_id === user.id);
    const json = (body: JsonBodyType, status = 200) => HttpResponse.json(body, { status });

    return [
      http.post(`${API}/auth/register`, async ({ request }) => {
        const failed = guard(request);
        if (failed) return failed;
        const body = (await request.json()) as Record<string, unknown>;
        this.bodies.push(body);
        // Mirrors the API: display_name is optional; null, "" and whitespace mean "none";
        // a non-string or more than 100 characters is a 422.
        if (
          "display_name" in body &&
          body.display_name !== null &&
          (typeof body.display_name !== "string" || body.display_name.length > 100)
        )
          return validation("display_name", "String should have at most 100 characters");
        const email = typeof body.email === "string" ? body.email.toLowerCase() : "";
        const password = typeof body.password === "string" ? body.password : "";
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))
          return validation("email", "value is not a valid email address");
        if (password.length < 8)
          return validation("password", "String should have at least 8 characters");
        if (this.users.has(email))
          return error(
            409,
            "An account with this email already exists",
            "email_already_registered",
          );
        const displayName =
          typeof body.display_name === "string" ? body.display_name.trim() || null : null;
        return json(FakeBackend.publicUser(this.addUser(email, password, displayName)), 201);
      }),

      http.post(`${API}/auth/login`, async ({ request }) => {
        const failed = guard(request);
        if (failed) return failed;
        const body = (await request.json()) as { email: string; password: string };
        const user = this.users.get(body.email.toLowerCase());
        if (user?.password !== body.password)
          return error(401, "Incorrect email or password", "invalid_credentials");
        return json({
          access_token: this.issueToken(user),
          token_type: "bearer",
          expires_in: 3600,
        });
      }),

      http.get(`${API}/auth/me`, ({ request }) => {
        const failed = guard(request);
        if (failed) return failed;
        const user = this.userFor(request);
        return user instanceof Response ? user : json(FakeBackend.publicUser(user));
      }),

      http.get(`${API}/transcriptions`, ({ request }) => {
        const failed = guard(request);
        if (failed) return failed;
        const user = this.userFor(request);
        if (user instanceof Response) return user;
        const params = new URL(request.url).searchParams;
        const page = Number(params.get("page") ?? 1);
        const pageSize = Number(params.get("page_size") ?? 20);
        const q = params.get("q")?.toLowerCase();
        const items = this.transcriptions
          .filter((t) => t.owner_id === user.id)
          .filter(
            (t) => !q || t.title.toLowerCase().includes(q) || t.text.toLowerCase().includes(q),
          )
          .filter((t) => !params.get("speech_mode") || t.speech_mode === params.get("speech_mode"))
          .filter((t) => !params.get("status") || t.status === params.get("status"))
          .filter((t) => !params.get("source") || t.source === params.get("source"))
          .sort((a, b) => b.created_at.localeCompare(a.created_at));
        const total = items.length;
        return json({
          items: items
            .slice((page - 1) * pageSize, page * pageSize)
            .map((t) => FakeBackend.summary(t)),
          total,
          page,
          page_size: pageSize,
          pages: total ? Math.ceil(total / pageSize) : 0,
        });
      }),

      http.post(`${API}/transcriptions`, async ({ request }) => {
        const failed = guard(request);
        if (failed) return failed;
        const user = this.userFor(request);
        if (user instanceof Response) return user;
        const body = (await request.json()) as Record<string, string | undefined>;
        this.bodies.push(body);
        if (!body.text?.trim()) return validation("text", "Value error, must not be blank");
        const record = this.addTranscription(user, {
          title: body.title?.trim() ?? "Transcription - 20 Nov 2026, 10:00 UTC",
          text: body.text.trim(),
          ...(body.speech_mode
            ? { speech_mode: body.speech_mode as Transcription["speech_mode"] }
            : {}),
          ...(body.script_preference
            ? { script_preference: body.script_preference as Transcription["script_preference"] }
            : {}),
        });
        return json(FakeBackend.detail(record, false), 201);
      }),

      http.get(`${API}/transcriptions/:id`, ({ request, params }) => {
        const failed = guard(request);
        if (failed) return failed;
        const user = this.userFor(request);
        if (user instanceof Response) return user;
        const record = owned(user, String(params.id));
        if (!record) return error(404, "Transcription not found", "transcription_not_found");
        const withSegments = new URL(request.url).searchParams.get("include_segments") === "true";
        return json(FakeBackend.detail(record, withSegments));
      }),

      http.patch(`${API}/transcriptions/:id`, async ({ request, params }) => {
        const failed = guard(request);
        if (failed) return failed;
        const user = this.userFor(request);
        if (user instanceof Response) return user;
        const record = owned(user, String(params.id));
        if (!record) return error(404, "Transcription not found", "transcription_not_found");
        const body = (await request.json()) as Record<string, unknown>;
        this.bodies.push(body);
        const keys = Object.keys(body);
        if (
          keys.length === 0 ||
          keys.some((k) => !["title", "text"].includes(k) || body[k] === null)
        )
          return validation("body", "Value error, Provide at least one of: title, text");
        if (typeof body.title === "string") record.title = body.title.trim();
        if (typeof body.text === "string" && body.text.trim() !== record.text) {
          record.text = body.text.trim();
          record.is_edited = true;
        }
        record.updated_at = this.tick();
        return json(FakeBackend.detail(record, false));
      }),

      http.delete(`${API}/transcriptions/:id`, ({ request, params }) => {
        const failed = guard(request);
        if (failed) return failed;
        const user = this.userFor(request);
        if (user instanceof Response) return user;
        const index = this.transcriptions.findIndex(
          (t) => t.id === String(params.id) && t.owner_id === user.id,
        );
        if (index === -1) return error(404, "Transcription not found", "transcription_not_found");
        this.transcriptions.splice(index, 1);
        return new HttpResponse(null, { status: 204 });
      }),
    ];
  }
}

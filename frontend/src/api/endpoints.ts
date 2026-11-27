import type { ApiClient } from "./client";
import type {
  LoginRequest,
  RegisterRequest,
  TokenResponse,
  Transcription,
  TranscriptionCreate,
  TranscriptionListParams,
  TranscriptionPage,
  TranscriptionUpdate,
  User,
} from "./types";

/** Endpoints under VITE_API_BASE_URL (/api/v1). Paths match backend/app/api/v1/*.py. */
export function createEndpoints(client: ApiClient) {
  return {
    auth: {
      register: (data: RegisterRequest) =>
        client.post<User>("/auth/register", data, { auth: false }),
      login: (data: LoginRequest) =>
        client.post<TokenResponse>("/auth/login", data, { auth: false }),
      me: (signal?: AbortSignal) => client.get<User>("/auth/me", signal ? { signal } : {}),
    },
    transcriptions: {
      list: (params: TranscriptionListParams, signal?: AbortSignal) =>
        client.get<TranscriptionPage>("/transcriptions", {
          query: { ...params },
          ...(signal ? { signal } : {}),
        }),
      get: (id: string, options: { includeSegments?: boolean; signal?: AbortSignal } = {}) =>
        client.get<Transcription>(`/transcriptions/${encodeURIComponent(id)}`, {
          query: { include_segments: options.includeSegments ? true : undefined },
          ...(options.signal ? { signal: options.signal } : {}),
        }),
      create: (data: TranscriptionCreate) => client.post<Transcription>("/transcriptions", data),
      update: (id: string, data: TranscriptionUpdate) =>
        client.patch<Transcription>(`/transcriptions/${encodeURIComponent(id)}`, data),
      remove: (id: string) => client.delete(`/transcriptions/${encodeURIComponent(id)}`),
    },
  };
}

export type Endpoints = ReturnType<typeof createEndpoints>;

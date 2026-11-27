/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Backend REST API base URL including /api/v1, e.g. http://localhost:8000/api/v1 */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

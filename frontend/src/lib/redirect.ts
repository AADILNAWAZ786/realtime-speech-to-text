/** Only same-app paths, never an absolute URL (open-redirect guard). */
export function safeRedirect(from: unknown): string {
  return typeof from === "string" && from.startsWith("/") && !from.startsWith("//") ? from : "/";
}

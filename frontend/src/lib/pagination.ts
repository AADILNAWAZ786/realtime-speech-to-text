/** Page numbers to show, with null for a gap: [1, null, 4, 5, 6, null, 10]. */
export function pageWindow(page: number, pages: number): (number | null)[] {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);
  const result: (number | null)[] = [1];
  const start = Math.max(2, page - 1);
  const end = Math.min(pages - 1, page + 1);
  if (start > 2) result.push(null);
  for (let p = start; p <= end; p++) result.push(p);
  if (end < pages - 1) result.push(null);
  result.push(pages);
  return result;
}

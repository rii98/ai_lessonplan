export const plural = (n: number, word: string, many = `${word}s`) => `${n} ${n === 1 ? word : many}`;

export function truncate(s: string | null | undefined, n: number): string {
  const t = String(s ?? "").trim();
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
}

const rtf = typeof Intl !== "undefined" ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }) : null;

/** "3 minutes ago", "yesterday" — falls back to a date past a week. */
export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const sec = Math.round((t - now) / 1000);
  const abs = Math.abs(sec);
  if (!rtf) return new Date(t).toLocaleString();
  if (abs < 45) return "just now";
  if (abs < 3600) return rtf.format(Math.round(sec / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(sec / 3600), "hour");
  if (abs < 7 * 86400) return rtf.format(Math.round(sec / 86400), "day");
  return new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export const dateTime = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";

export const humanize = (s: string) => s.replace(/_/g, " ");

export const formatScore = (n: number | null | undefined) =>
  n == null ? "—" : Number.isInteger(n) ? String(n) : n.toFixed(1);

/** "A > **B** > C" → "A › B › C" */
export const breadcrumb = (s: string | null | undefined) =>
  String(s ?? "").replace(/\*\*/g, "").replace(/\s*>\s*/g, " › ").trim();

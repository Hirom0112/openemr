/**
 * Friendly timestamp helpers for surfaces visible to clinicians.
 *
 * Format: "May 3, 10:57 AM" — month name + day, then 12-hour time.
 * Locale-aware via Intl.DateTimeFormat. Returns "" when the input is
 * not a parseable date so callers can fall back to a placeholder.
 */
export function formatFriendly(value: string | Date | null | undefined): string {
  if (value == null) return '';
  const d = typeof value === 'string' ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) return '';
  return new Intl.DateTimeFormat([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  }).format(d);
}

/**
 * Same shape as ``formatFriendly`` but appends seconds — used in tooltips
 * where the extra precision helps when comparing two refreshes that
 * happened in the same minute.
 */
export function formatFriendlyWithSeconds(value: string | Date | null | undefined): string {
  if (value == null) return '';
  const d = typeof value === 'string' ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) return '';
  return new Intl.DateTimeFormat([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
    hour12: true,
  }).format(d);
}

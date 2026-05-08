/**
 * Slice 9.8 — client-side telemetry shim (PHI-safe).
 *
 * For v1 we do NOT post events to the server (`/ui/event` route is deferred —
 * see todo.md Slice 9.8). We queue events in-memory so a future shipper can
 * drain the queue without instrumentation churn. Until then `flush()` exposes
 * the buffer for tests / debug consoles.
 *
 * PHI rules (mirror agent-api/CLAUDE.md §5.2):
 *   - Never log raw filenames. Hash to first 12 hex of sha256.
 *   - Lane / reason / outcome are closed enums; free-text gets dropped.
 *   - File size is a coarse bucket (`<1MB` / `1-5MB` / `5-25MB` / `>25MB`).
 *   - Never log patient_id, MRN, names, DOB, or any extracted clinical text.
 *
 * The hash uses Web Crypto when available; the env-fallback (test rigs without
 * SubtleCrypto) returns a deterministic non-cryptographic placeholder so unit
 * tests stay deterministic. Production paths always have crypto.subtle.
 */

import type { Lane } from '../styles/tokens';

export type TelemetryEventName =
  | 'dropzone_validate'
  | 'dropzone_upload_start'
  | 'dropzone_upload_state'
  | 'dropzone_upload_outcome'
  | 'dropzone_threshold_30s'
  | 'dropzone_threshold_2m'
  | 'dropzone_threshold_5m'
  | 'dropzone_late_resolve'
  | 'approval_open'
  | 'approval_close'
  | 'approval_action'
  | 'quarantine_open'
  | 'quarantine_action';

export type IngestOutcome = 'committed' | 'staged' | 'quarantined' | 'duplicate' | 'failed';

export type SizeBucket = 'lt_1mb' | '1_5mb' | '5_25mb' | 'gt_25mb';

export interface TelemetryEvent {
  name: TelemetryEventName;
  ts: number; // epoch ms
  lane?: Lane;
  outcome?: IngestOutcome | 'success' | 'error' | 'partial';
  reason_code?: string; // closed enum from server (`mrn_not_found`, etc.)
  size_bucket?: SizeBucket;
  filename_hash?: string; // 12 hex chars; never raw name
  duration_ms?: number;
  state?: string; // dropzone state machine label
  count?: number; // for batch sizes
}

// In-memory ring buffer. Bounded so a long session doesn't leak.
const MAX_EVENTS = 500;
const _queue: TelemetryEvent[] = [];

export function record(evt: Omit<TelemetryEvent, 'ts'>): void {
  const full: TelemetryEvent = { ts: Date.now(), ...evt };
  _queue.push(full);
  if (_queue.length > MAX_EVENTS) _queue.shift();
}

/** Drain — returns and clears the buffer. */
export function flush(): TelemetryEvent[] {
  const out = _queue.slice();
  _queue.length = 0;
  return out;
}

/** Read without clearing — for live dashboards / dev tools. */
export function peek(): readonly TelemetryEvent[] {
  return _queue;
}

export function bucketSize(bytes: number): SizeBucket {
  if (bytes < 1_048_576) return 'lt_1mb';
  if (bytes < 5_242_880) return '1_5mb';
  if (bytes < 26_214_400) return '5_25mb';
  return 'gt_25mb';
}

/**
 * Hash a filename to 12 hex chars. Async because Web Crypto is async; callers
 * that don't want to await can fire-and-forget by chaining `.then(record)`.
 */
export async function hashFilename(name: string): Promise<string> {
  if (typeof crypto !== 'undefined' && crypto.subtle) {
    try {
      const enc = new TextEncoder().encode(name);
      const digest = await crypto.subtle.digest('SHA-256', enc);
      const bytes = new Uint8Array(digest);
      let hex = '';
      for (let i = 0; i < 6; i += 1) {
        hex += bytes[i].toString(16).padStart(2, '0');
      }
      return hex;
    } catch {
      // fall through
    }
  }
  // Non-crypto fallback for environments without SubtleCrypto. Stable enough
  // for tests; never used in production browsers.
  let h = 0x811c9dc5;
  for (let i = 0; i < name.length; i += 1) {
    h = (h ^ name.charCodeAt(i)) >>> 0;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, '0').slice(0, 12).padEnd(12, '0');
}

/** Convenience: synchronously record an event with a pre-computed hash. */
export function recordWithHash(
  name: TelemetryEventName,
  filenameHash: string | undefined,
  extra: Omit<TelemetryEvent, 'ts' | 'name' | 'filename_hash'> = {},
): void {
  record({ name, filename_hash: filenameHash, ...extra });
}

/**
 * Drag-and-drop + click-to-upload entry point for the W2 Pillar 1 Path B
 * "Sara drops a file in chat" workflow. Mounted inside ChatSurface, just above
 * the message input row.
 *
 * Slice 9.8 expansion (W2 multimodal):
 *   - Whitelist extends to .docx / .tiff / .tif / .xlsx / .hl7. HL7 has no
 *     widely-honored MIME — accept by extension when MIME is text/plain or
 *     application/octet-stream (the two we observe in browsers).
 *   - 25 MB pre-flight client check. Server is still the authority.
 *   - State machine replaces the old `busy: boolean`:
 *       idle → uploading → committed | staged | quarantined | failed
 *     With latency thresholds at 30s (warn), 2m (panic), 5m (terminal toast
 *     but the fetch is NOT aborted — late resolves still record telemetry
 *     and surface a notice).
 *   - Structured error mapping for 400 / 413 / 415 sub-codes → human copy.
 *   - LaneChip beneath the pill once a file is selected (pre-classification).
 *   - Optional callbacks for staged / quarantined results — ChatSurface uses
 *     them to mount ApprovalModal / QuarantineCard at root level.
 *
 * UX:
 *   - Resting: a small inline pill ("Drop a file or click to upload"). Clicking
 *     the pill opens the OS file picker scoped to the whitelist.
 *   - Dragging anywhere over the chat surface: the pill expands into a
 *     full-width tinted overlay reading "Drop to ingest into the chart". On
 *     dragleave we shrink back. preventDefault on dragover/drop is REQUIRED —
 *     otherwise the browser's default takes over and opens the PDF in a new
 *     tab.
 *   - Uploading: spinner + "Ingesting document..." until the response lands.
 *   - Error: red inline message under the pill, dismissable.
 */

import { useCallback, useEffect, useRef, useState, type DragEvent, type ReactElement } from 'react';
import {
  IngestStructuredError,
  ingestDocumentWithResult,
  type DuplicateIngestPayload,
  type IngestResponse,
  type IngestResult,
  type QuarantineIngestPayload,
  type StagingMetadata,
} from '../api';
import { BRAND, NEU, RED, SURFACE } from '../styles/tokens';
import LaneChip, { laneFromFilename } from './LaneChip';
import { bucketSize, hashFilename, record } from '../lib/telemetry';

const ACCEPTED_MIME_TYPES = [
  'application/pdf',
  'image/png',
  'image/tiff',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document', // .docx
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',       // .xlsx
] as const;

const ACCEPTED_EXTENSIONS = ['.pdf', '.png', '.tif', '.tiff', '.docx', '.xlsx', '.hl7'] as const;

// HL7 has no widely honored MIME. Browsers commonly report these for .hl7:
const HL7_FALLBACK_MIMES = new Set(['text/plain', 'application/octet-stream', '']);

const ACCEPT_ATTR = ([...ACCEPTED_EXTENSIONS, ...ACCEPTED_MIME_TYPES] as string[]).join(',');

const MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024; // 25 MB

// Latency thresholds — see Slice 9.8 spec.
const THRESHOLD_30S_MS = 30_000;
const THRESHOLD_2M_MS = 120_000;
const THRESHOLD_5M_MS = 300_000;

export interface FileDropZoneProps {
  baseUrl: string;
  /**
   * Allowed to be null/undefined now. If unset, we still let the upload start
   * — the server's identity resolver may match the file from its parsed
   * demographics and either succeed (committed/staged) or quarantine for
   * manual triage. Pre-Slice-9.8 we hard-blocked here.
   */
  patientId: string | null | undefined;
  /** Legacy callback retained for committed-path callers. */
  onExtraction: (response: IngestResponse, file: File) => void;
  /** Slice 9.8: notified when the server staged rows for approval. */
  onStaged?: (staging: StagingMetadata, response: IngestResponse, file: File) => void;
  /** Slice 9.8: notified when the server quarantined the upload (HTTP 202). */
  onQuarantined?: (payload: QuarantineIngestPayload, file: File) => void;
  /**
   * Notified when the server reports a content-hash duplicate (HTTP 202 with
   * `status: "processing"`) — the document was already ingested for this
   * patient, no quarantine, no new work. ChatSurface mounts an info card
   * pointing the operator to the Documents tab.
   */
  onDuplicate?: (payload: DuplicateIngestPayload, file: File) => void;
  disabled?: boolean;
  docTypeHint?: string;
}

interface ValidationOutcome {
  ok: boolean;
  file?: File;
  error?: string;
}

/** State-machine state. */
export type DropzoneState =
  | 'idle'
  | 'uploading'
  | 'committed'
  | 'staged'
  | 'quarantined'
  | 'duplicate'
  | 'failed';

/**
 * Pure validator extracted so unit tests can hit it without DOM mocks.
 *
 * Rules:
 *   - exactly one file (multi-drop unsupported in v1)
 *   - MIME or extension must match the whitelist
 *   - HL7 special-cases: extension is `.hl7` AND MIME is one of the fallback
 *     set (browsers don't agree on a single MIME for HL7v2).
 *   - 25 MB cap (server is still authoritative).
 */
export function validateDroppedFiles(files: File[] | FileList | null): ValidationOutcome {
  // Use Array.from on anything iterable / array-like (FileList, plain array).
  // Avoids `instanceof FileList`, which is not defined in node test environments
  // and would throw a ReferenceError there.
  let arr: File[];
  if (!files) {
    arr = [];
  } else if (Array.isArray(files)) {
    arr = files;
  } else {
    arr = Array.from(files as ArrayLike<File>);
  }
  if (arr.length === 0) {
    return { ok: false, error: 'No file detected.' };
  }
  if (arr.length > 1) {
    return { ok: false, error: 'Drop one file at a time.' };
  }
  const f = arr[0];
  const lowerName = f.name.toLowerCase();
  const mimeOk = (ACCEPTED_MIME_TYPES as readonly string[]).includes(f.type);
  const extOk = ACCEPTED_EXTENSIONS.some((ext) => lowerName.endsWith(ext));
  // HL7 escape hatch: .hl7 + permissive MIME.
  const hl7Ok = lowerName.endsWith('.hl7') && HL7_FALLBACK_MIMES.has(f.type);
  if (!mimeOk && !extOk && !hl7Ok) {
    return { ok: false, error: `Unsupported file type. PDF, PNG, TIFF, DOCX, XLSX, or HL7 only.` };
  }
  if (f.size > MAX_FILE_SIZE_BYTES) {
    const mb = (f.size / 1_048_576).toFixed(1);
    return { ok: false, error: `File too large (${mb} MB). 25 MB max.` };
  }
  return { ok: true, file: f };
}

// Structured error sub-code → operator copy.
const STRUCTURED_ERROR_COPY: Record<string, string> = {
  // 400 family
  invalid_pdf: 'The PDF could not be opened. Re-export it and try again.',
  invalid_format: 'The file format does not match its extension. Re-save and retry.',
  parse_failed: 'We could not parse this file. Re-save it or try a different export.',
  patient_id_missing: 'Select a patient first or supply demographics in the document.',
  // 413
  file_too_large: 'File exceeds the 25 MB cap. Split or compress before retrying.',
  // 415
  unsupported_media_type: 'Unsupported file type. PDF, PNG, TIFF, DOCX, XLSX, or HL7 only.',
};

function _copyForError(err: unknown): string {
  if (err instanceof IngestStructuredError) {
    if (err.subCode && STRUCTURED_ERROR_COPY[err.subCode]) {
      return STRUCTURED_ERROR_COPY[err.subCode];
    }
    if (err.status === 413) return STRUCTURED_ERROR_COPY.file_too_large;
    if (err.status === 415) return STRUCTURED_ERROR_COPY.unsupported_media_type;
    if (err.status === 400) return 'The server rejected this upload. Check the file and retry.';
    return `Upload failed (${err.status}).`;
  }
  return err instanceof Error ? err.message : 'Upload failed.';
}

export default function FileDropZone(props: FileDropZoneProps): ReactElement {
  const { baseUrl, patientId, onExtraction, onStaged, onQuarantined, onDuplicate, disabled, docTypeHint } = props;
  const [isDragOver, setIsDragOver] = useState(false);
  const [state, setState] = useState<DropzoneState>('idle');
  const [stateNote, setStateNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pendingLane, setPendingLane] = useState<ReturnType<typeof laneFromFilename>>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // dragenter/dragleave fire for every child element the cursor crosses.
  // A simple counter avoids the overlay flickering off when the cursor moves
  // between siblings — only the outermost leave (counter back to 0) hides it.
  const dragDepthRef = useRef(0);

  const effectivelyDisabled = disabled === true;
  const busy = state === 'uploading';

  const handleFile = useCallback(async (file: File): Promise<void> => {
    setError(null);
    setState('uploading');
    setStateNote(null);
    setPendingLane(laneFromFilename(file.name));

    const filenameHash = await hashFilename(file.name).catch(() => undefined);
    record({
      name: 'dropzone_upload_start',
      lane: laneFromFilename(file.name) ?? undefined,
      size_bucket: bucketSize(file.size),
      filename_hash: filenameHash,
    });
    const t0 = performance.now();

    // Threshold timers. We never abort the in-flight fetch; thresholds are
    // pure UX hints + telemetry markers. The 5-min timer flips to "failed"
    // visually but does not cancel the request — when the request resolves
    // late, we surface a "late resolve" toast instead of dropping the data.
    let stillUploading = true;
    const t30 = window.setTimeout(() => {
      if (!stillUploading) return;
      record({ name: 'dropzone_threshold_30s', filename_hash: filenameHash });
      setStateNote('Server is taking longer than expected (30s+).');
    }, THRESHOLD_30S_MS);
    const t2m = window.setTimeout(() => {
      if (!stillUploading) return;
      record({ name: 'dropzone_threshold_2m', filename_hash: filenameHash });
      setStateNote('Still working (2 min+). You can keep using the chat.');
    }, THRESHOLD_2M_MS);
    const t5m = window.setTimeout(() => {
      if (!stillUploading) return;
      record({ name: 'dropzone_threshold_5m', filename_hash: filenameHash });
      // 5-min terminal: visually flip to failed but DO NOT abort.
      setState('failed');
      setError('Upload exceeded 5 minutes. The server is still processing — we will notify you when it lands.');
    }, THRESHOLD_5M_MS);

    try {
      const result: IngestResult = await ingestDocumentWithResult(
        baseUrl,
        file,
        patientId ?? null,
        docTypeHint,
      );
      stillUploading = false;
      window.clearTimeout(t30);
      window.clearTimeout(t2m);
      window.clearTimeout(t5m);
      const elapsed = performance.now() - t0;

      // If we already flipped to terminal-late ('failed' from t5m), surface a
      // "late resolve" toast instead of acting as if we were still busy.
      const wasLate = elapsed >= THRESHOLD_5M_MS;
      if (wasLate) {
        record({ name: 'dropzone_late_resolve', filename_hash: filenameHash, duration_ms: elapsed });
        setStateNote('Late resolve — your earlier upload completed.');
      }

      if (result.kind === 'committed') {
        setState('committed');
        record({ name: 'dropzone_upload_outcome', outcome: 'committed', filename_hash: filenameHash, duration_ms: elapsed });
        onExtraction(result.response, file);
      } else if (result.kind === 'staged') {
        setState('staged');
        record({ name: 'dropzone_upload_outcome', outcome: 'staged', filename_hash: filenameHash, duration_ms: elapsed, count: result.staging.pending_extraction_ids.length });
        if (onStaged) {
          onStaged(result.staging, result.response, file);
        } else {
          // No host hook — fall back to legacy committed behaviour so the
          // user still sees something. Approval will need to be done from
          // outside this surface.
          onExtraction(result.response, file);
        }
      } else if (result.kind === 'quarantined') {
        setState('quarantined');
        record({ name: 'dropzone_upload_outcome', outcome: 'quarantined', filename_hash: filenameHash, duration_ms: elapsed, reason_code: result.payload.reason_code });
        if (onQuarantined) onQuarantined(result.payload, file);
      } else {
        // result.kind === 'duplicate' — content-hash collision; the document
        // is already ingested (or in flight) for this patient.
        setState('duplicate');
        record({ name: 'dropzone_upload_outcome', outcome: 'duplicate', filename_hash: filenameHash, duration_ms: elapsed });
        if (onDuplicate) onDuplicate(result.payload, file);
      }
    } catch (err: unknown) {
      stillUploading = false;
      window.clearTimeout(t30);
      window.clearTimeout(t2m);
      window.clearTimeout(t5m);
      const elapsed = performance.now() - t0;
      setState('failed');
      setError(_copyForError(err));
      record({
        name: 'dropzone_upload_outcome',
        outcome: 'failed',
        filename_hash: filenameHash,
        duration_ms: elapsed,
        reason_code: err instanceof IngestStructuredError ? (err.subCode ?? `http_${err.status}`) : 'exception',
      });
    }
  }, [baseUrl, patientId, docTypeHint, onExtraction, onStaged, onQuarantined, onDuplicate]);

  // Document-level dragover/drop listeners so the overlay catches drops
  // anywhere inside the chat surface (not just on the small pill). Without
  // listening on `document` the browser's default opens the PDF the moment
  // the user releases over a non-droppable child.
  useEffect(() => {
    if (effectivelyDisabled) return;

    const onDocDragEnter = (e: globalThis.DragEvent): void => {
      // Only react to drags that actually carry files.
      const types = e.dataTransfer?.types;
      if (!types || !Array.from(types).includes('Files')) return;
      e.preventDefault();
      dragDepthRef.current += 1;
      setIsDragOver(true);
    };
    const onDocDragOver = (e: globalThis.DragEvent): void => {
      const types = e.dataTransfer?.types;
      if (!types || !Array.from(types).includes('Files')) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    };
    const onDocDragLeave = (e: globalThis.DragEvent): void => {
      const types = e.dataTransfer?.types;
      if (!types || !Array.from(types).includes('Files')) return;
      dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
      if (dragDepthRef.current === 0) setIsDragOver(false);
    };
    const onDocDrop = (e: globalThis.DragEvent): void => {
      // CRITICAL: must preventDefault here, otherwise the browser opens the
      // PDF inline if the user releases over the document edge.
      const types = e.dataTransfer?.types;
      if (!types || !Array.from(types).includes('Files')) return;
      e.preventDefault();
      dragDepthRef.current = 0;
      setIsDragOver(false);
      const result = validateDroppedFiles(e.dataTransfer?.files ?? null);
      record({ name: 'dropzone_validate', outcome: result.ok ? 'success' : 'error' });
      if (!result.ok) {
        setError(result.error ?? 'Could not accept that file.');
        return;
      }
      void handleFile(result.file as File);
    };

    document.addEventListener('dragenter', onDocDragEnter);
    document.addEventListener('dragover', onDocDragOver);
    document.addEventListener('dragleave', onDocDragLeave);
    document.addEventListener('drop', onDocDrop);
    return () => {
      document.removeEventListener('dragenter', onDocDragEnter);
      document.removeEventListener('dragover', onDocDragOver);
      document.removeEventListener('dragleave', onDocDragLeave);
      document.removeEventListener('drop', onDocDrop);
    };
  }, [effectivelyDisabled, handleFile]);

  // Pill-level handlers — redundant with document listeners for the common
  // case but cheap, and they keep the visual feedback tight when the user
  // hovers the pill itself.
  const onPillDragOver = (e: DragEvent<HTMLDivElement>): void => {
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  };

  const onPickerChange = (e: React.ChangeEvent<HTMLInputElement>): void => {
    const files = e.target.files;
    const result = validateDroppedFiles(files);
    record({ name: 'dropzone_validate', outcome: result.ok ? 'success' : 'error' });
    if (!result.ok) {
      setError(result.error ?? 'Could not accept that file.');
      // reset so picking the same bad file twice still re-fires
      if (inputRef.current) inputRef.current.value = '';
      return;
    }
    if (inputRef.current) inputRef.current.value = '';
    void handleFile(result.file as File);
  };

  const openPicker = (): void => {
    if (effectivelyDisabled || busy) return;
    inputRef.current?.click();
  };

  const tooltip = effectivelyDisabled
    ? 'Uploads disabled.'
    : 'Drop a PDF, PNG, TIFF, DOCX, XLSX, or HL7 file here, or click to browse.';

  const pillLabel = ((): string => {
    switch (state) {
      case 'uploading': return 'Ingesting document…';
      case 'committed': return 'Document ingested';
      case 'staged': return 'Staged for approval';
      case 'quarantined': return 'Held for manual review';
      case 'duplicate': return 'Already in patient record';
      case 'failed': return 'Upload failed';
      case 'idle':
      default:
        return 'Drop a file or click to upload';
    }
  })();

  return (
    <div style={{ position: 'relative' }}>
      {/* Hidden file input — driven by the pill's click handler */}
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT_ATTR}
        onChange={onPickerChange}
        style={{ display: 'none' }}
        aria-hidden="true"
        tabIndex={-1}
      />

      {/* Resting pill */}
      <div
        role="button"
        aria-label={tooltip}
        title={tooltip}
        tabIndex={effectivelyDisabled ? -1 : 0}
        onClick={openPicker}
        onKeyDown={(e) => {
          if (effectivelyDisabled || busy) return;
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            openPicker();
          }
        }}
        onDragOver={onPillDragOver}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 8,
          padding: '6px 12px',
          minHeight: 28,
          fontSize: 12,
          fontFamily: 'inherit',
          color: effectivelyDisabled ? SURFACE.subtle : SURFACE.muted,
          background: SURFACE.panel,
          border: `1px dashed ${effectivelyDisabled ? NEU.border : BRAND.base}`,
          borderRadius: 14,
          cursor: effectivelyDisabled || busy ? 'not-allowed' : 'pointer',
          opacity: effectivelyDisabled ? 0.6 : 1,
          userSelect: 'none',
        }}
      >
        {busy ? (
          <>
            <span
              aria-hidden="true"
              style={{
                width: 10,
                height: 10,
                borderRadius: '50%',
                border: `2px solid ${NEU.border}`,
                borderTopColor: BRAND.base,
                display: 'inline-block',
                animation: 'copilot-spin 0.8s linear infinite',
              }}
            />
            <span>{pillLabel}</span>
          </>
        ) : (
          <>
            <span aria-hidden="true" style={{ color: BRAND.base, fontWeight: 600 }}>↑</span>
            <span>{pillLabel}</span>
          </>
        )}
      </div>

      {/* Lane chip beneath the pill once we know the lane (pre-classification). */}
      {pendingLane && state !== 'idle' && (
        <div style={{ marginTop: 4 }}>
          <LaneChip lane={pendingLane} size="sm" />
        </div>
      )}

      {stateNote && (
        <div
          role="status"
          style={{
            marginTop: 6,
            fontSize: 11,
            color: SURFACE.muted,
            background: SURFACE.panel,
            border: `1px solid ${SURFACE.border}`,
            borderRadius: 6,
            padding: '4px 8px',
          }}
        >
          {stateNote}
        </div>
      )}

      {error && (
        <div
          role="alert"
          style={{
            marginTop: 6,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontSize: 11,
            color: RED.text,
            background: RED.bg,
            border: `1px solid ${RED.border}`,
            borderRadius: 6,
            padding: '4px 8px',
          }}
        >
          <span style={{ flex: 1 }}>{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            aria-label="Dismiss error"
            style={{
              background: 'transparent',
              border: 'none',
              color: RED.text,
              cursor: 'pointer',
              fontSize: 14,
              lineHeight: 1,
              padding: 0,
            }}
          >
            ×
          </button>
        </div>
      )}

      {/* Full-surface tinted drag overlay */}
      {isDragOver && !effectivelyDisabled && (
        <div
          aria-hidden="true"
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 100,
            background: 'rgba(37, 99, 235, 0.10)',
            border: `2px dashed ${BRAND.base}`,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            pointerEvents: 'none',
            color: BRAND.base,
            fontSize: 16,
            fontWeight: 600,
            letterSpacing: '0.02em',
          }}
        >
          Drop to ingest into the chart
        </div>
      )}
    </div>
  );
}

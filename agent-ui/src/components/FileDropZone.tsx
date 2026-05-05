/**
 * Drag-and-drop + click-to-upload entry point for the W2 Pillar 1 Path B
 * "Sara drops a PDF in chat" workflow. Mounted inside ChatSurface, just above
 * the message input row.
 *
 * UX:
 *   - Resting: a small inline pill ("Drop a PDF or click to upload"). Clicking
 *     the pill opens the OS file picker scoped to PDF + PNG.
 *   - Dragging anywhere over the chat surface: the pill expands into a
 *     full-width tinted overlay reading "Drop to ingest into the chart". On
 *     dragleave we shrink back. preventDefault on dragover/drop is REQUIRED —
 *     otherwise the browser's default takes over and opens the PDF in a new
 *     tab (the bug being fixed).
 *   - Uploading: spinner + "Ingesting document..." until the response lands.
 *   - Error: red inline message under the pill, dismissable.
 *
 * The drop overlay is window-level by attaching to the parent surface via
 * the parentRef the host wires through. We use the component's own bounding
 * box for the resting pill but listen on `document` for drag enter/leave so
 * the user can drop anywhere over the chat — not just on the pill.
 */

import { useCallback, useEffect, useRef, useState, type DragEvent, type ReactElement } from 'react';
import { ingestDocument, type IngestResponse } from '../api';
import { BRAND, NEU, RED, SURFACE } from '../styles/tokens';

const ACCEPTED_MIME_TYPES = ['application/pdf', 'image/png'] as const;
const ACCEPTED_EXTENSIONS = ['.pdf', '.png'] as const;
const ACCEPT_ATTR = ACCEPTED_EXTENSIONS.concat(ACCEPTED_MIME_TYPES as unknown as string[]).join(',');

export interface FileDropZoneProps {
  baseUrl: string;
  /**
   * Required for the multipart payload. When undefined / empty we render the
   * pill in disabled state with a tooltip — the backend rejects ingests
   * without a patient_id, so there is no point letting the user start one.
   */
  patientId: string | null | undefined;
  onExtraction: (response: IngestResponse, file: File) => void;
  disabled?: boolean;
  docTypeHint?: string;
}

interface ValidationOutcome {
  ok: boolean;
  file?: File;
  error?: string;
}

/**
 * Pure validator extracted so unit tests can hit it without DOM mocks.
 * Rules:
 *   - exactly one file (multi-drop unsupported in v1)
 *   - MIME or extension must match PDF / PNG
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
  if (!mimeOk && !extOk) {
    return { ok: false, error: `Unsupported file type. Drop a PDF or PNG.` };
  }
  return { ok: true, file: f };
}

export default function FileDropZone(props: FileDropZoneProps): ReactElement {
  const { baseUrl, patientId, onExtraction, disabled, docTypeHint } = props;
  const [isDragOver, setIsDragOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // dragenter/dragleave fire for every child element the cursor crosses.
  // A simple counter avoids the overlay flickering off when the cursor moves
  // between siblings — only the outermost leave (counter back to 0) hides it.
  const dragDepthRef = useRef(0);

  const effectivelyDisabled = disabled || !patientId;

  const handleFile = useCallback(async (file: File): Promise<void> => {
    if (!patientId) {
      setError('Select a patient first.');
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const resp = await ingestDocument(baseUrl, file, patientId, docTypeHint);
      onExtraction(resp, file);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Upload failed.';
      setError(msg);
    } finally {
      setBusy(false);
    }
  }, [baseUrl, patientId, docTypeHint, onExtraction]);

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
    ? (patientId ? 'Uploads disabled.' : 'Select a patient first.')
    : 'Drop a PDF or PNG here, or click to browse.';

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
            <span>Ingesting document…</span>
          </>
        ) : (
          <>
            <span aria-hidden="true" style={{ color: BRAND.base, fontWeight: 600 }}>↑</span>
            <span>Drop a PDF or click to upload</span>
          </>
        )}
      </div>

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

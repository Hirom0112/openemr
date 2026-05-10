"use client";

/**
 * DocumentSourceLink — a clickable Source-cell renderer for the Co-Pilot
 * Conditions / Labs tables. The user previously saw plain text like
 * "DocumentReference/<uuid> · Source: p1-b042"; this component turns the
 * uuid portion into a button that opens an in-app preview of the document.
 *
 * The preview renders by mimetype:
 *   - application/pdf  → `<iframe>` (browsers ship a PDF viewer)
 *   - image/*          → `<img>`
 *   - text/*           → `<pre>` with the response body
 *   - other            → "Cannot preview" + a download link
 *
 * The bytes come from `/api/documents/<uuid>`, a server route that proxies
 * the FHIR Binary endpoint with the user's OAuth bearer (see
 * `src/app/api/documents/[uuid]/route.ts`).
 *
 * Accessibility: Base UI's Dialog primitive provides the focus trap, escape
 * handling, click-outside dismiss, and `aria-modal` semantics.
 */
import * as React from "react";
import { Dialog } from "@base-ui/react/dialog";
import { X as CloseIcon } from "lucide-react";

import { cn } from "@/lib/utils";

export interface DocumentSourceLinkProps {
  /** DocumentReference uuid (hex, 32 chars). Empty string disables the link. */
  uuid: string;
  /** Human caption shown alongside the link (e.g. "Source: p1-b042"). */
  caption?: string | null;
  /**
   * Fallback display when the row has no parsed source. When provided and
   * `uuid` is empty, the component renders this text non-interactively.
   */
  fallbackText?: string | null;
  className?: string;
}

type PreviewState =
  | { kind: "idle" }
  | { kind: "loading" }
  | {
      kind: "ready";
      mimetype: string;
      blobUrl: string | null;
      textBody: string | null;
    }
  | { kind: "error"; message: string };

function classifyMimetype(
  mt: string,
): "pdf" | "image" | "text" | "other" {
  if (mt.startsWith("application/pdf")) return "pdf";
  if (mt.startsWith("image/")) return "image";
  if (mt.startsWith("text/")) return "text";
  return "other";
}

export function DocumentSourceLink({
  uuid,
  caption,
  fallbackText,
  className,
}: DocumentSourceLinkProps): React.ReactElement {
  const [open, setOpen] = React.useState(false);
  const [state, setState] = React.useState<PreviewState>({ kind: "idle" });

  React.useEffect(() => {
    return () => {
      // Release any blob URL we minted while the dialog was open.
      if (state.kind === "ready" && state.blobUrl) {
        URL.revokeObjectURL(state.blobUrl);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadDocument = React.useCallback(async () => {
    setState({ kind: "loading" });
    try {
      // next.config.ts sets basePath: "/dashboard". Client-side fetch()
      // does not auto-prefix the basePath, so the URL has to include it
      // explicitly or the request 404s through to OpenEMR's Apache.
      const res = await fetch(
        `/dashboard/api/documents/${encodeURIComponent(uuid)}`,
      );
      if (!res.ok) {
        setState({
          kind: "error",
          message: `Failed to load document (${res.status}).`,
        });
        return;
      }
      const mimetype =
        res.headers.get("content-type")?.split(";")[0]?.trim() ??
        "application/octet-stream";
      const kind = classifyMimetype(mimetype);
      if (kind === "text") {
        const body = await res.text();
        setState({ kind: "ready", mimetype, blobUrl: null, textBody: body });
        return;
      }
      const blob = await res.blob();
      const blobUrl = URL.createObjectURL(blob);
      setState({ kind: "ready", mimetype, blobUrl, textBody: null });
    } catch (err) {
      setState({
        kind: "error",
        message: err instanceof Error ? err.message : "Unexpected error",
      });
    }
  }, [uuid]);

  const handleOpenChange = React.useCallback(
    (next: boolean) => {
      setOpen(next);
      if (next && state.kind === "idle") {
        void loadDocument();
      }
      if (!next && state.kind === "ready" && state.blobUrl) {
        URL.revokeObjectURL(state.blobUrl);
        setState({ kind: "idle" });
      }
    },
    [loadDocument, state],
  );

  // No DocumentReference uuid → render fallback text (or em-dash) inert.
  if (!uuid) {
    return (
      <span
        className={cn("text-xs text-muted-foreground", className)}
        title={fallbackText ?? undefined}
      >
        {fallbackText ?? "—"}
      </span>
    );
  }

  const shortUuid = `${uuid.slice(0, 8)}…`;

  return (
    <Dialog.Root open={open} onOpenChange={handleOpenChange}>
      <Dialog.Trigger
        render={(props) => (
          <button
            {...props}
            type="button"
            className={cn(
              "inline-flex flex-col items-start gap-0 text-left text-xs",
              "text-primary underline-offset-2 hover:underline",
              "focus:outline-none focus-visible:ring-2 focus-visible:ring-primary",
              "rounded-sm",
              className,
            )}
            data-testid="document-source-link"
            aria-label={`Open source document ${shortUuid}`}
            title={`DocumentReference/${uuid}`}
          >
            <span>DocumentReference/{shortUuid}</span>
            {caption ? (
              <span
                className="text-[10px] text-muted-foreground"
                data-testid="document-source-caption"
              >
                {caption}
              </span>
            ) : null}
          </button>
        )}
      />
      <Dialog.Portal>
        <Dialog.Backdrop
          className={cn(
            "fixed inset-0 z-50 bg-black/50 backdrop-blur-sm",
            "data-[starting-style]:opacity-0 data-[ending-style]:opacity-0",
            "transition-opacity duration-150",
          )}
        />
        <Dialog.Popup
          className={cn(
            "fixed left-1/2 top-1/2 z-50 -translate-x-1/2 -translate-y-1/2",
            "flex h-[85vh] w-[min(960px,92vw)] flex-col overflow-hidden rounded-lg",
            "border border-border bg-background shadow-xl",
            "data-[starting-style]:scale-95 data-[starting-style]:opacity-0",
            "data-[ending-style]:scale-95 data-[ending-style]:opacity-0",
            "transition-[opacity,transform] duration-150",
          )}
          data-testid="document-source-dialog"
        >
          <header className="flex items-center justify-between border-b border-border px-4 py-2">
            <div className="flex flex-col">
              <Dialog.Title className="text-sm font-medium">
                Source document
              </Dialog.Title>
              <Dialog.Description className="text-xs text-muted-foreground">
                DocumentReference/{uuid}
              </Dialog.Description>
            </div>
            <Dialog.Close
              render={(props) => (
                <button
                  {...props}
                  type="button"
                  aria-label="Close"
                  className={cn(
                    "rounded-md p-1.5 text-muted-foreground",
                    "hover:bg-muted hover:text-foreground",
                    "focus:outline-none focus-visible:ring-2 focus-visible:ring-primary",
                  )}
                >
                  <CloseIcon className="h-4 w-4" aria-hidden="true" />
                </button>
              )}
            />
          </header>
          <div className="flex-1 overflow-auto bg-muted/30">
            <PreviewBody
              state={state}
              uuid={uuid}
              onRetry={() => void loadDocument()}
            />
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

interface PreviewBodyProps {
  state: PreviewState;
  uuid: string;
  onRetry: () => void;
}

function PreviewBody({
  state,
  uuid,
  onRetry,
}: PreviewBodyProps): React.ReactElement {
  if (state.kind === "idle" || state.kind === "loading") {
    return (
      <div
        className="flex h-full items-center justify-center text-sm text-muted-foreground"
        data-testid="document-source-loading"
      >
        Loading document…
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <div
        className="flex h-full flex-col items-center justify-center gap-2 px-4 text-center"
        data-testid="document-source-error"
      >
        <p className="text-sm text-foreground">{state.message}</p>
        <button
          type="button"
          onClick={onRetry}
          className="text-xs text-primary underline underline-offset-2"
        >
          Retry
        </button>
      </div>
    );
  }
  const kind = classifyMimetype(state.mimetype);
  if (kind === "pdf" && state.blobUrl) {
    return (
      <iframe
        src={state.blobUrl}
        title="Source document preview"
        className="h-full w-full"
        data-testid="document-source-iframe"
      />
    );
  }
  if (kind === "image" && state.blobUrl) {
    return (
      <div className="flex h-full items-center justify-center p-4">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={state.blobUrl}
          alt="Source document preview"
          className="max-h-full max-w-full object-contain"
          data-testid="document-source-image"
        />
      </div>
    );
  }
  if (kind === "text" && state.textBody !== null) {
    return (
      <pre
        className="m-0 h-full overflow-auto whitespace-pre-wrap p-4 text-xs leading-relaxed"
        data-testid="document-source-text"
      >
        {state.textBody}
      </pre>
    );
  }
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 px-4 text-center">
      <p className="text-sm text-foreground">
        Cannot preview {state.mimetype} in-app.
      </p>
      <a
        href={`/dashboard/api/documents/${encodeURIComponent(uuid)}`}
        download
        className="text-xs text-primary underline underline-offset-2"
        data-testid="document-source-download"
      >
        Download document
      </a>
    </div>
  );
}

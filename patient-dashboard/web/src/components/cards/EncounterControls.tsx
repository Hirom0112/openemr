"use client";

/**
 * Encounter controls in the patient header — Select Encounter dropdown
 * stub, Open-Encounter label, and "New encounter" `+` button. All three
 * are visually present per parity with the original
 * (`reference-screenshots/00-dashboard-FULL-PAGE-gloria.png`) but
 * functionally read-only in this port.
 *
 * Click-feedback contract (R2.2): clicking the stubbed dropdown or the
 * `+` button shows a self-dismissing toast reading "Encounter
 * management is not implemented in this port". The "Open Encounter"
 * label is a label, not interactive — left as static text. Avoids
 * silent-broken UX without pretending the controls are wired.
 *
 * Lives in a Client Component because it owns local toast state and
 * an `onClick`. The parent `PatientHeader` is a Server Component and
 * passes the encounter count in via props.
 */

import * as React from "react";
import { History, Plus } from "lucide-react";

const TOAST_MESSAGE = "Encounter management is not implemented in this port";
const TOAST_DURATION_MS = 3000;

export interface EncounterControlsProps {
    /** Encounter count for the "(N)" suffix in the dropdown stub. */
    count: number;
}

export function EncounterControls({
    count,
}: EncounterControlsProps): React.ReactElement {
    const [toastVisible, setToastVisible] = React.useState(false);
    const timeoutRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

    const showToast = React.useCallback(() => {
        setToastVisible(true);
        if (timeoutRef.current) clearTimeout(timeoutRef.current);
        timeoutRef.current = setTimeout(
            () => setToastVisible(false),
            TOAST_DURATION_MS,
        );
    }, []);

    React.useEffect(() => {
        return () => {
            if (timeoutRef.current) clearTimeout(timeoutRef.current);
        };
    }, []);

    return (
        <>
            <div className="ml-auto flex flex-wrap items-center gap-3">
                <span className="group relative inline-flex">
                    <button
                        type="button"
                        onClick={showToast}
                        className="flex items-center gap-1 rounded border bg-muted/40 px-2 py-1 text-sm text-muted-foreground hover:bg-muted/60"
                        data-testid="encounter-picker-stub"
                        aria-label="Select encounter (read-only port)"
                    >
                        <History className="h-4 w-4" aria-hidden="true" />
                        <span>Select Encounter ({count})</span>
                    </button>
                    <span
                        role="tooltip"
                        data-testid="encounter-picker-tooltip"
                        className="pointer-events-none absolute left-0 top-full z-10 mt-1 w-max max-w-xs rounded border bg-background px-2 py-1 text-xs text-foreground opacity-0 shadow-md transition-opacity group-hover:opacity-100 group-focus-within:opacity-100"
                    >
                        {TOAST_MESSAGE}
                    </span>
                </span>
                <span
                    className="text-sm text-muted-foreground"
                    data-testid="open-encounter-stub"
                >
                    Open Encounter: None
                </span>
                <span className="group relative inline-flex">
                    <button
                        type="button"
                        aria-label="New encounter (read-only port)"
                        onClick={showToast}
                        className="inline-flex h-8 w-8 items-center justify-center rounded border text-muted-foreground hover:bg-muted/60"
                        data-testid="new-encounter-stub"
                    >
                        <Plus className="h-4 w-4" />
                    </button>
                    <span
                        role="tooltip"
                        data-testid="new-encounter-tooltip"
                        className="pointer-events-none absolute right-0 top-full z-10 mt-1 w-max max-w-xs rounded border bg-background px-2 py-1 text-xs text-foreground opacity-0 shadow-md transition-opacity group-hover:opacity-100 group-focus-within:opacity-100"
                    >
                        {TOAST_MESSAGE}
                    </span>
                </span>
            </div>
            {toastVisible ? (
                <div
                    role="status"
                    aria-live="polite"
                    data-testid="encounter-toast"
                    className="fixed bottom-4 right-4 z-50 rounded border bg-background px-4 py-2 text-sm shadow-md"
                >
                    {TOAST_MESSAGE}
                </div>
            ) : null}
        </>
    );
}

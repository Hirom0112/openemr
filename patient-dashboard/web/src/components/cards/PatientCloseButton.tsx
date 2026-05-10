"use client";

/**
 * Close-patient affordance shown in the page-level PatientHeader.
 *
 * Lives in a Client Component because it owns the confirm gate and
 * the navigation handler. Hit area is 44x44px (WCAG 2.5.5) while the
 * icon stays visually 16px (h-4 w-4). The visible left margin moves
 * the target away from the patient name so a hover-drift from the
 * name does not land on Close.
 */

import * as React from "react";
import { X as XIcon } from "lucide-react";

const CONFIRM_PROMPT = "Close patient view?";

export function PatientCloseButton(): React.ReactElement {
    const onClick = React.useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
        if (typeof window === "undefined") {
            return;
        }
        const ok = window.confirm(CONFIRM_PROMPT);
        if (!ok) {
            event.preventDefault();
            return;
        }
        window.location.assign("/");
    }, []);

    return (
        <button
            type="button"
            onClick={onClick}
            aria-label="Close patient"
            data-testid="patient-close"
            className="ml-3 inline-flex h-11 w-11 items-center justify-center rounded text-muted-foreground hover:bg-muted"
        >
            <XIcon className="h-4 w-4" aria-hidden="true" />
        </button>
    );
}

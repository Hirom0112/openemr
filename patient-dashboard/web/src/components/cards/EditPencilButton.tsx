"use client";

/**
 * Edit-pencil affordance shared by every card. Lives in a Client Component
 * because Server Components cannot pass event-handler functions over the
 * RSC wire (passing `onClick={fn}` to a `<button>` from a Server Component
 * is a runtime error in Next 16+ App Router).
 *
 * Read-only stub per brief — the onClick logs a TODO. The real wiring
 * lands when mutation FHIR endpoints / SMART write scopes are in scope.
 * See `dashboard-inventory.md` for the per-card edit-affordance entries
 * and PATIENT_DASHBOARD_MIGRATION.md for the read-only-by-design framing.
 */

import * as React from "react";
import { Pencil } from "lucide-react";

export interface EditPencilButtonProps {
    /** Accessible label, e.g. "Edit allergies". */
    ariaLabel: string;
    /** Test selector, kept stable for vitest queries. */
    testId?: string;
    /** Identifier surfaced in the TODO log so we can tell which card fired. */
    cardName: string;
}

export function EditPencilButton({
    ariaLabel,
    testId,
    cardName,
}: EditPencilButtonProps): React.ReactElement {
    return (
        <button
            type="button"
            aria-label={ariaLabel}
            data-testid={testId}
            onClick={() => {
                // eslint-disable-next-line no-console
                console.log(
                    `[${cardName}] TODO: wire edit affordance — read-only this phase`,
                );
            }}
            className="inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-muted"
        >
            <Pencil className="h-4 w-4" aria-hidden="true" />
        </button>
    );
}

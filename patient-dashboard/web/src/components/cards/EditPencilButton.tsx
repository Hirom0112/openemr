"use client";

/**
 * Edit-pencil affordance shared by every card. Lives in a Client Component
 * because Server Components cannot pass event-handler functions over the
 * RSC wire (passing `onClick={fn}` to a `<button>` from a Server Component
 * is a runtime error in Next 16+ App Router).
 *
 * Read-only port. The pencil icon's *presence* on each card is parity
 * (`templates/patient/card/*.html.twig` all expose one). The pencil's
 * *disabled appearance* communicates "we know this affordance exists,
 * we deliberately did not wire it up" — graceful UX rather than a
 * silent-broken click.
 *
 * Tooltip rendering uses a CSS-only sibling element rather than the
 * native `title` attribute. Browsers gate `title` behind a ~1.5s
 * hover delay that varies per platform; the user reported seeing
 * the tooltip on some pencils but not others depending on hover
 * speed. The CSS-only approach shows the tooltip immediately and
 * consistently across cards and patients.
 *
 * The click handler still fires and still logs the per-card marker so
 * DevTools can confirm the wiring path; the disabled visual is the
 * user-facing signal, the log is the developer-facing one.
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

const READ_ONLY_TOOLTIP =
    "Read-only port — edit interactions are out of scope per brief";

export function EditPencilButton({
    ariaLabel,
    testId,
    cardName,
}: EditPencilButtonProps): React.ReactElement {
    return (
        <span className="group relative inline-flex">
            <button
                type="button"
                aria-label={ariaLabel}
                aria-disabled="true"
                data-testid={testId}
                data-readonly="true"
                onClick={() => {
                    // eslint-disable-next-line no-console
                    console.log(
                        `[${cardName}] TODO: wire edit affordance — read-only this phase`,
                    );
                }}
                className="inline-flex h-6 w-6 cursor-not-allowed items-center justify-center rounded text-muted-foreground/60 opacity-60 hover:bg-muted/40"
            >
                <Pencil className="h-4 w-4" aria-hidden="true" />
            </button>
            <span
                role="tooltip"
                data-testid={testId ? `${testId}-tooltip` : undefined}
                className="pointer-events-none absolute right-0 top-full z-10 mt-1 w-max max-w-xs rounded border bg-background px-2 py-1 text-xs text-foreground opacity-0 shadow-md transition-opacity group-hover:opacity-100 group-focus-within:opacity-100"
            >
                {READ_ONLY_TOOLTIP}
            </span>
        </span>
    );
}

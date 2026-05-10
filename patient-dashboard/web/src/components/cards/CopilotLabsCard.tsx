/**
 * CopilotLabsCard — server-rendered "Copilot Labs" card.
 *
 * Phase 6.2 Path B2 (custom panel, NOT dual-write). Surfaces approved
 * Observations that originated in the Clinical Co-Pilot's
 * `copilot_observations` MySQL table. Read path:
 *
 *   `copilot_observations` (MySQL)
 *     └─► OpenEMR FhirObservationCopilotService (FHIR projection,
 *         registered alongside the core lab service so its rows surface
 *         under category=laboratory; see agent-api/CLAUDE.md, the
 *         documented exception clause)
 *           └─► /apis/default/fhir/Observation?patient=...&category=laboratory
 *               └─► this card
 *
 * Discriminator: copilot rows carry a deterministic id prefixed
 * "copilot-" (e.g. "copilot-348-1742-6"); native lab rows do not.
 * We filter the laboratory bundle by that prefix so this card only
 * shows facts that came out of the Co-Pilot's extractor + verifier
 * pipeline, distinct from natively-entered procedure_result rows
 * the standard Labs surface owns.
 *
 * This file does NOT import any Co-Pilot code (patient-dashboard
 * CLAUDE.md rule #6). It depends only on the OpenEMR FHIR endpoint
 * the dashboard already consumes for Vitals.
 */
import * as React from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyCard, ErrorCard } from "@/components/cards/card-states";
import { DocumentSourceLink } from "@/components/DocumentSourceLink";
import { FhirClient } from "@/lib/fhir/client";
import { parseObservationSource } from "@/lib/fhir/document-source";
import { authSessionTokenProvider } from "@/lib/fhir/token";
import type { FhirObservation } from "@/lib/fhir/types";

const CARD_TITLE = "Copilot Labs";
const COPILOT_ID_PREFIX = "copilot-";
const MAX_VISIBLE = 10;

interface CopilotLabsCardProps {
    patientUuid: string;
}

interface CopilotLabsCardViewProps {
    observations: FhirObservation[];
}

/** True for Observations sourced from `copilot_observations`. */
export function isCopilotObservation(obs: FhirObservation): boolean {
    return typeof obs.id === "string" && obs.id.startsWith(COPILOT_ID_PREFIX);
}

/** Friendly label for the test name. Mirrors VitalsCard.displayLabel. */
export function copilotLabel(obs: FhirObservation): string {
    const display = obs.code?.coding?.[0]?.display?.trim();
    if (display) return display;
    const text = obs.code?.text?.trim();
    if (text) return text;
    return obs.code?.coding?.[0]?.code ?? "Lab";
}

/** Render value + unit. Falls back to valueString, finally em-dash. */
export function copilotValue(obs: FhirObservation): string {
    const q = obs.valueQuantity;
    if (q?.value != null) {
        return q.unit ? `${q.value} ${q.unit}` : `${q.value}`;
    }
    if (obs.valueString && obs.valueString.trim().length > 0) {
        return obs.valueString.trim();
    }
    return "—";
}

/**
 * Format effectiveDateTime as YYYY-MM-DD. Returns null when missing or
 * unparseable so the column can render an em-dash placeholder.
 */
export function copilotDate(obs: FhirObservation): string | null {
    const iso = obs.effectiveDateTime;
    if (!iso) return null;
    const m = /^(\d{4}-\d{2}-\d{2})/.exec(iso);
    return m ? m[1] : iso;
}

/**
 * Source label — pulls the first `note[].text` line, which the Co-Pilot
 * extractor populates as "Source: <citation>" (verified against pid 12,
 * 2026-05-10). Returns null when no note is present.
 */
export function copilotSource(obs: FhirObservation): string | null {
    const note = (obs as unknown as { note?: { text?: string }[] }).note;
    const first = note?.[0]?.text?.trim();
    return first && first.length > 0 ? first : null;
}

/**
 * Sort by effectiveDateTime descending (most recent first). Rows with
 * no date sort to the bottom in stable insertion order.
 */
export function sortByDateDesc(observations: FhirObservation[]): FhirObservation[] {
    return [...observations].sort((a, b) => {
        const aT = a.effectiveDateTime ? Date.parse(a.effectiveDateTime) : NaN;
        const bT = b.effectiveDateTime ? Date.parse(b.effectiveDateTime) : NaN;
        const aOk = Number.isFinite(aT);
        const bOk = Number.isFinite(bT);
        if (aOk && bOk) return bT - aT;
        if (aOk) return -1;
        if (bOk) return 1;
        return 0;
    });
}

/**
 * Source cell — converts the FHIR `derivedFrom` DocumentReference + the
 * "Source: <locator>" annotation into an in-app preview link. Falls back to
 * the original plain-text rendering when no DocumentReference is attached.
 */
function ObservationSourceCell({
    obs,
    fallback,
}: {
    obs: FhirObservation;
    fallback: string | null;
}): React.ReactElement {
    const parsed = parseObservationSource(obs);
    return (
        <DocumentSourceLink
            uuid={parsed?.uuid ?? ""}
            caption={parsed?.locator ?? null}
            fallbackText={fallback}
        />
    );
}

export function CopilotLabsCardSkeleton(): React.ReactElement {
    return (
        <Card size="sm" data-testid="copilot-labs-card-skeleton" aria-busy="true">
            <CardHeader>
                <CardTitle>{CARD_TITLE}</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-2">
                <Skeleton className="h-3 w-3/4" />
                <Skeleton className="h-3 w-2/3" />
                <Skeleton className="h-3 w-1/2" />
            </CardContent>
        </Card>
    );
}

export function CopilotLabsCardView({
    observations,
}: CopilotLabsCardViewProps): React.ReactElement {
    const copilotRows = sortByDateDesc(observations.filter(isCopilotObservation));

    if (copilotRows.length === 0) {
        return (
            <EmptyCard
                title={CARD_TITLE}
                message="No Copilot-extracted lab results yet."
            />
        );
    }

    const visible = copilotRows.slice(0, MAX_VISIBLE);
    const overflow = copilotRows.length - visible.length;

    return (
        <Card size="sm" data-testid="copilot-labs-card">
            <CardHeader>
                <CardTitle className="text-primary">{CARD_TITLE}</CardTitle>
            </CardHeader>
            <CardContent>
                <table
                    className="w-full text-sm"
                    data-testid="copilot-labs-table"
                >
                    <thead>
                        <tr className="text-left text-xs uppercase text-muted-foreground">
                            <th className="pb-1 pr-3 font-medium">Test</th>
                            <th className="pb-1 pr-3 font-medium">Value</th>
                            <th className="pb-1 pr-3 font-medium">Date</th>
                            <th className="pb-1 font-medium">Source</th>
                        </tr>
                    </thead>
                    <tbody>
                        {visible.map((obs) => {
                            const label = copilotLabel(obs);
                            const value = copilotValue(obs);
                            const date = copilotDate(obs);
                            const source = copilotSource(obs);
                            return (
                                <tr
                                    key={obs.id}
                                    data-testid="copilot-lab-row"
                                    className="border-t border-border/40"
                                >
                                    <td className="py-1 pr-3 font-medium">{label}</td>
                                    <td className="py-1 pr-3" data-testid="copilot-lab-value">
                                        {value}
                                    </td>
                                    <td className="py-1 pr-3 text-muted-foreground">
                                        {date ?? "—"}
                                    </td>
                                    <td className="py-1 text-xs">
                                        <ObservationSourceCell obs={obs} fallback={source} />
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
                {overflow > 0 ? (
                    <p
                        className="mt-2 text-xs text-muted-foreground"
                        data-testid="copilot-labs-overflow"
                    >
                        + {overflow} more not shown
                    </p>
                ) : null}
            </CardContent>
        </Card>
    );
}

export default async function CopilotLabsCard({
    patientUuid,
}: CopilotLabsCardProps): Promise<React.ReactElement> {
    const baseUrl = process.env.OPENEMR_BASE_URL;
    if (!baseUrl) {
        return (
            <ErrorCard
                title={CARD_TITLE}
                message="Server is not configured to reach OpenEMR."
            />
        );
    }

    try {
        const client = new FhirClient({
            baseUrl: `${baseUrl.replace(/\/+$/, "")}/apis/default/fhir`,
            tokenProvider: authSessionTokenProvider,
        });
        // Pull the lab bundle once. We over-request `_count` because the
        // copilot rows are interleaved with native procedure_result rows
        // and we filter client-side; bundles seen in dev for the busiest
        // patient (pid 12, 2026-05-10) total ~120 — comfortably under
        // 200.
        const observations = await client.getObservations(patientUuid, {
            category: "laboratory",
            count: 200,
        });
        return <CopilotLabsCardView observations={observations} />;
    } catch (error) {
        console.error("[CopilotLabsCard] FHIR fetch failed", error);
        return <ErrorCard title={CARD_TITLE} />;
    }
}

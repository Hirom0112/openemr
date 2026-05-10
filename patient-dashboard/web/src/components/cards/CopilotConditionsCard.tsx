/**
 * CopilotConditionsCard — server-rendered "Copilot Conditions" card.
 *
 * Phase 6 follow-up to close the 6.3 verification gap. The OpenEMR
 * stock chart Problem List sidebar (`stats.php`/`stats_full.php`) reads
 * the `lists` table directly via raw SQL and bypasses the FHIR
 * aggregator, so Conditions written by the Co-Pilot into
 * `copilot_conditions` never appear there. Read path here:
 *
 *   `copilot_conditions` (MySQL)
 *     └─► OpenEMR FhirConditionCopilotService (FHIR projection,
 *         registered into the core FhirConditionService aggregator
 *         alongside FhirConditionProblemListItemService — see
 *         `src/Services/FHIR/FhirConditionService.php` and the
 *         documented exception clause in agent-api/CLAUDE.md)
 *           └─► /apis/default/fhir/Condition?patient=...&category=problem-list-item
 *               └─► this card
 *
 * Discriminator: copilot rows carry the deterministic id prefix
 * "copilot-" (verified 2026-05-10, e.g. `copilot-999-phase63-test`);
 * native `lists` rows do not. Mirrors CopilotLabsCard's strategy.
 *
 * No Co-Pilot code import (patient-dashboard CLAUDE.md rule #6).
 */
import * as React from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyCard, ErrorCard } from "@/components/cards/card-states";
import { FhirClient } from "@/lib/fhir/client";
import { authSessionTokenProvider } from "@/lib/fhir/token";
import type { FhirCondition } from "@/lib/fhir/types";

const CARD_TITLE = "Copilot Conditions";
const COPILOT_ID_PREFIX = "copilot-";
const MAX_VISIBLE = 10;

interface CopilotConditionsCardProps {
    patientUuid: string;
}

interface CopilotConditionsCardViewProps {
    conditions: FhirCondition[];
}

/** True for Conditions sourced from `copilot_conditions`. */
export function isCopilotCondition(c: FhirCondition): boolean {
    return typeof c.id === "string" && c.id.startsWith(COPILOT_ID_PREFIX);
}

/** Friendly label for the condition. Prefers code.text, then coding display, then code. */
export function copilotConditionLabel(c: FhirCondition): string {
    const text = c.code?.text?.trim();
    if (text) return text;
    const display = c.code?.coding?.[0]?.display?.trim();
    if (display) return display;
    return c.code?.coding?.[0]?.code ?? "Condition";
}

/** Coding (e.g. ICD-10 code) for tooltip / secondary display. */
export function copilotConditionCode(c: FhirCondition): string | null {
    const code = c.code?.coding?.[0]?.code?.trim();
    return code && code.length > 0 ? code : null;
}

/** Active / inactive / resolved etc. — pulled from clinicalStatus.coding[0].code. */
export function copilotConditionStatus(c: FhirCondition): string {
    const code = c.clinicalStatus?.coding?.[0]?.code?.trim();
    if (code) return code;
    const text = c.clinicalStatus?.text?.trim();
    return text && text.length > 0 ? text : "—";
}

/**
 * Onset date as YYYY-MM-DD; falls back to recordedDate. Returns null
 * when neither is present so the column renders an em-dash.
 */
export function copilotConditionOnset(c: FhirCondition): string | null {
    const iso = c.onsetDateTime ?? c.recordedDate;
    if (!iso) return null;
    const m = /^(\d{4}-\d{2}-\d{2})/.exec(iso);
    return m ? m[1] : iso;
}

/**
 * Source label — `note[].text` (the Co-Pilot writer mirrors the Labs
 * convention of "Source: <citation>"). Returns null when absent.
 */
export function copilotConditionSource(c: FhirCondition): string | null {
    const note = (c as unknown as { note?: { text?: string }[] }).note;
    const first = note?.[0]?.text?.trim();
    return first && first.length > 0 ? first : null;
}

/** Sort by onset (or recorded) date desc; undated rows sort last. */
export function sortConditionsByDateDesc(conditions: FhirCondition[]): FhirCondition[] {
    const ts = (c: FhirCondition): number => {
        const iso = c.onsetDateTime ?? c.recordedDate;
        if (!iso) return NaN;
        return Date.parse(iso);
    };
    return [...conditions].sort((a, b) => {
        const aT = ts(a);
        const bT = ts(b);
        const aOk = Number.isFinite(aT);
        const bOk = Number.isFinite(bT);
        if (aOk && bOk) return bT - aT;
        if (aOk) return -1;
        if (bOk) return 1;
        return 0;
    });
}

export function CopilotConditionsCardSkeleton(): React.ReactElement {
    return (
        <Card size="sm" data-testid="copilot-conditions-card-skeleton" aria-busy="true">
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

export function CopilotConditionsCardView({
    conditions,
}: CopilotConditionsCardViewProps): React.ReactElement {
    const copilotRows = sortConditionsByDateDesc(conditions.filter(isCopilotCondition));

    if (copilotRows.length === 0) {
        return (
            <EmptyCard
                title={CARD_TITLE}
                message="No Copilot-extracted conditions yet."
            />
        );
    }

    const visible = copilotRows.slice(0, MAX_VISIBLE);
    const overflow = copilotRows.length - visible.length;

    return (
        <Card size="sm" data-testid="copilot-conditions-card">
            <CardHeader>
                <CardTitle className="text-primary">{CARD_TITLE}</CardTitle>
            </CardHeader>
            <CardContent>
                <table
                    className="w-full text-sm"
                    data-testid="copilot-conditions-table"
                >
                    <thead>
                        <tr className="text-left text-xs uppercase text-muted-foreground">
                            <th className="pb-1 pr-3 font-medium">Condition</th>
                            <th className="pb-1 pr-3 font-medium">Code</th>
                            <th className="pb-1 pr-3 font-medium">Status</th>
                            <th className="pb-1 pr-3 font-medium">Onset</th>
                            <th className="pb-1 font-medium">Source</th>
                        </tr>
                    </thead>
                    <tbody>
                        {visible.map((c) => {
                            const label = copilotConditionLabel(c);
                            const code = copilotConditionCode(c);
                            const status = copilotConditionStatus(c);
                            const onset = copilotConditionOnset(c);
                            const source = copilotConditionSource(c);
                            return (
                                <tr
                                    key={c.id}
                                    data-testid="copilot-condition-row"
                                    className="border-t border-border/40"
                                >
                                    <td className="py-1 pr-3 font-medium">{label}</td>
                                    <td className="py-1 pr-3 text-xs text-muted-foreground">
                                        {code ?? "—"}
                                    </td>
                                    <td className="py-1 pr-3">{status}</td>
                                    <td className="py-1 pr-3 text-muted-foreground">
                                        {onset ?? "—"}
                                    </td>
                                    <td
                                        className="py-1 text-xs text-muted-foreground"
                                        title={source ?? undefined}
                                    >
                                        {source ?? "—"}
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
                {overflow > 0 ? (
                    <p
                        className="mt-2 text-xs text-muted-foreground"
                        data-testid="copilot-conditions-overflow"
                    >
                        + {overflow} more not shown
                    </p>
                ) : null}
            </CardContent>
        </Card>
    );
}

export default async function CopilotConditionsCard({
    patientUuid,
}: CopilotConditionsCardProps): Promise<React.ReactElement> {
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
        // FhirConditionService aggregator merges copilot_conditions +
        // lists when category=problem-list-item; we filter the bundle
        // client-side by the "copilot-" id prefix.
        const conditions = await client.getConditions(patientUuid, {
            category: "problem-list-item",
        });
        return <CopilotConditionsCardView conditions={conditions} />;
    } catch (error) {
        console.error("[CopilotConditionsCard] FHIR fetch failed", error);
        return <ErrorCard title={CARD_TITLE} />;
    }
}

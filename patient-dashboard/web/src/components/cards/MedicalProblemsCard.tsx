/**
 * MedicalProblemsCard — problem-list card for the patient dashboard.
 *
 * Follows the PatientHeader Server-Component pattern: builds a
 * FhirClient with the Auth.js session token, fetches Conditions
 * server-side, and renders. The FHIR access token never leaves the
 * server.
 *
 * Spec sources (treat as authoritative — fields are not invented here):
 *   - patient-dashboard/dashboard-inventory.md  -> "Medical Problems"
 *   - patient-dashboard/dashboard-api-map.md    -> "Medical Problems"
 *   - patient-dashboard/reference-screenshots/12-card-problems-loaded.png
 *
 * The original twig template renders only the title; the brief for this
 * card asks for additional fields (onset date, status badge) that are
 * present in the FHIR resource but were hidden in the legacy view. We
 * surface them here per task brief and filter to clinicalStatus active /
 * inactive (excluding resolved) — the FHIR equivalent of the legacy
 * `filterActiveIssues()` carry-forward.
 */
import * as React from "react";
import { Pencil } from "lucide-react";

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardAction,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyCard, ErrorCard } from "@/components/cards/card-states";
import { FhirClient } from "@/lib/fhir/client";
import { authSessionTokenProvider } from "@/lib/fhir/token";
import type { FhirCondition } from "@/lib/fhir/types";

const CARD_TITLE = "Medical Problems";

interface MedicalProblemsCardProps {
  patientUuid: string;
}

/**
 * Resolve the human-readable problem display per dashboard-api-map.md:
 *   `code.text` first, then `code.coding[0].display`. Returns null when
 *   neither is populated — caller decides how to render.
 */
export function formatProblemDisplay(condition: FhirCondition): string | null {
  const text = condition.code?.text?.trim();
  if (text && text.length > 0) {
    return text;
  }
  const display = condition.code?.coding?.[0]?.display?.trim();
  if (display && display.length > 0) {
    return display;
  }
  return null;
}

/**
 * Extract the clinicalStatus.coding[0].code value when present, lowercased.
 */
export function getClinicalStatusCode(
  condition: FhirCondition,
): string | null {
  const code = condition.clinicalStatus?.coding?.[0]?.code;
  if (typeof code !== "string" || code.length === 0) {
    return null;
  }
  return code.toLowerCase();
}

/**
 * Format an onset date for display. Accepts a FHIR `dateTime` (which can
 * be just a date `YYYY-MM-DD` or a full ISO timestamp) and returns the
 * date-only `YYYY-MM-DD` portion. Returns null when missing or
 * unparseable.
 */
export function formatOnsetDate(value: string | undefined): string | null {
  if (!value) {
    return null;
  }
  const match = /^(\d{4}-\d{2}-\d{2})/.exec(value);
  return match ? match[1] : null;
}

/**
 * Filter to clinicalStatus active+inactive only — exclude resolved.
 * Matches the inventory's `filterActiveIssues` carry-forward, broadened
 * per task brief to include `inactive` so users can still see
 * historically-relevant problems.
 */
const VISIBLE_STATUSES: ReadonlySet<string> = new Set([
  "active",
  "recurrence",
  "relapse",
  "inactive",
  "remission",
]);

export function filterVisibleProblems(
  conditions: readonly FhirCondition[],
): FhirCondition[] {
  return conditions.filter((c) => {
    const status = getClinicalStatusCode(c);
    // No status -> default to visible (synthetic data may omit it).
    if (status === null) {
      return true;
    }
    return VISIBLE_STATUSES.has(status);
  });
}

/**
 * Skeleton companion for Suspense fallback. Mirrors the card chrome
 * (title bar + edit-action slot) so the layout doesn't jump on resolve.
 */
export function MedicalProblemsCardSkeleton(): React.ReactElement {
  return (
    <Card
      size="sm"
      data-testid="medical-problems-skeleton"
      aria-busy="true"
    >
      <CardHeader>
        <CardTitle>{CARD_TITLE}</CardTitle>
        <CardAction>
          <Skeleton className="h-6 w-6 rounded" />
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <Skeleton className="h-3 w-3/4" />
        <Skeleton className="h-3 w-1/2" />
      </CardContent>
    </Card>
  );
}

interface StatusBadgeProps {
  status: string;
}

function StatusBadge({ status }: StatusBadgeProps): React.ReactElement {
  // Visual variants are intentionally minimal (no design-token churn);
  // color is conveyed via background tint mapped from the status code.
  const tone =
    status === "active" || status === "recurrence" || status === "relapse"
      ? "bg-emerald-100 text-emerald-900"
      : status === "inactive" || status === "remission"
        ? "bg-amber-100 text-amber-900"
        : "bg-muted text-muted-foreground";
  return (
    <span
      data-testid="problem-status"
      data-status={status}
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${tone}`}
    >
      {status}
    </span>
  );
}

interface MedicalProblemsCardViewProps {
  conditions: readonly FhirCondition[];
}

/**
 * Pure-presentation view component, exported so tests can render
 * directly without faking the async Server Component.
 */
export function MedicalProblemsCardView({
  conditions,
}: MedicalProblemsCardViewProps): React.ReactElement {
  const visible = filterVisibleProblems(conditions);

  if (visible.length === 0) {
    return <EmptyCard title={CARD_TITLE} message="None" />;
  }

  return (
    <Card size="sm" data-testid="medical-problems-card">
      <CardHeader>
        <CardTitle>{CARD_TITLE}</CardTitle>
        <CardAction>
          {/* Edit pencil — STUB. Per dashboard-inventory.md "Edit affordance":
              targets stats_full.php?active=all&category=medical_problem.
              Wiring deferred; click logs a TODO marker. */}
          <button
            type="button"
            aria-label="Edit medical problems (not yet wired)"
            data-testid="medical-problems-edit"
            className="inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-muted"
            // eslint-disable-next-line no-console
            onClick={() => console.log("TODO: wire edit Medical Problems")}
          >
            <Pencil className="h-4 w-4" aria-hidden="true" />
          </button>
        </CardAction>
      </CardHeader>
      <CardContent>
        <ul
          className="flex flex-col gap-2"
          data-testid="medical-problems-list"
        >
          {visible.map((c, idx) => {
            const display = formatProblemDisplay(c) ?? "Unknown problem";
            const onset = formatOnsetDate(c.onsetDateTime);
            const status = getClinicalStatusCode(c);
            // FHIR `id` is the stable key when present; fall back to
            // index for synthetic data missing the field.
            const key = c.id ?? `idx-${idx}`;
            return (
              <li
                key={key}
                data-testid="problem-row"
                className="flex flex-wrap items-center gap-2 text-sm"
              >
                <span className="font-medium" data-testid="problem-display">
                  {display}
                </span>
                {onset !== null ? (
                  <span
                    className="text-xs text-muted-foreground"
                    data-testid="problem-onset"
                  >
                    {onset}
                  </span>
                ) : null}
                {status !== null ? <StatusBadge status={status} /> : null}
              </li>
            );
          })}
        </ul>
      </CardContent>
    </Card>
  );
}

/**
 * Server Component entry point. Builds a FhirClient from the ambient
 * Auth.js session, fetches Conditions filtered to `problem-list-item`,
 * renders.
 *
 * Errors are caught and surfaced via ErrorCard so a transient FHIR
 * failure does not blank the entire dashboard.
 */
export default async function MedicalProblemsCard({
  patientUuid,
}: MedicalProblemsCardProps): Promise<React.ReactElement> {
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
    const conditions = await client.getConditions(patientUuid, {
      category: "problem-list-item",
    });
    return <MedicalProblemsCardView conditions={conditions} />;
  } catch (error) {
    console.error("[MedicalProblemsCard] FHIR fetch failed", error);
    return <ErrorCard title={CARD_TITLE} />;
  }
}

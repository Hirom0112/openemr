/**
 * VitalsCard — server-rendered Vitals card for the patient dashboard.
 *
 * Follows the PatientHeader / AllergiesCard Server-Component pattern:
 * builds a FhirClient with the Auth.js session token provider, fetches
 * vital-sign Observation resources for the patient uuid, and renders the
 * "most recent of each LOINC" subset.
 *
 * Spec sources (treat as authoritative):
 *   - patient-dashboard/dashboard-inventory.md  -> "Vitals"
 *   - patient-dashboard/dashboard-api-map.md    -> "Vitals" (LOINC table)
 *   - patient-dashboard/reference-screenshots/16-card-vitals-loaded.png
 *   - patient-dashboard/reference-screenshots/24-card-vitals-loaded-alejandro.png
 *
 * Display rule (from the task brief):
 *   1. Fetch all `category=vital-signs` Observations.
 *   2. Group by `code.coding[0].code` (LOINC).
 *   3. Within each group, take the entry with the most-recent
 *      `effectiveDateTime`. That single row is what renders.
 *   4. Skip the panel LOINC `85353-1` ("Vital signs panel") — it is a
 *      container resource carrying `component[]`, not a value of its own.
 *      Its leaf members (heart rate, respiratory rate, BP, etc.) come
 *      back as their own top-level Observations and render via their
 *      individual LOINCs.
 *   5. The blood-pressure panel (LOINC `85354-9`) has no top-level
 *      `valueQuantity`; it carries systolic (`8480-6`) and diastolic
 *      (`8462-4`) sub-values in `component[]`. We render BP from the
 *      panel's components when present so a single "BP: 156/94 mmHg"
 *      row appears (matching the original card screenshot).
 *
 * Server `_sort` / `_count` were not used: the inventory's open Q8 about
 * whether OpenEMR honours `_sort=-date&_count=1` on `/Observation` is
 * still unresolved, and the bundle is small (15 entries verified for
 * Gloria), so client-side grouping is correct, deterministic, and cheap.
 */
import * as React from "react";
import { Pencil } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyCard, ErrorCard } from "@/components/cards/card-states";
import { FhirClient } from "@/lib/fhir/client";
import { authSessionTokenProvider } from "@/lib/fhir/token";
import type { FhirObservation } from "@/lib/fhir/types";

const CARD_TITLE = "Vitals";

/**
 * "Vital signs panel" LOINC. The FHIR US Core profile lets servers send
 * this as a container Observation that has no `valueQuantity` and points
 * at member rows via `hasMember`. Skipped at render time.
 */
const PANEL_LOINC_VITAL_SIGNS = "85353-1";

/**
 * Blood-pressure panel LOINC. This row carries no top-level value; the
 * systolic + diastolic values live in `component[]`. Rendered as a
 * combined "BP: systolic/diastolic" line.
 */
const LOINC_BP_PANEL = "85354-9";
const LOINC_BP_SYSTOLIC = "8480-6";
const LOINC_BP_DIASTOLIC = "8462-4";
// Body temperature LOINC. Original screenshot 16 renders both Fahrenheit
// and Celsius ("37 F (2.78 C)" — actually "98.6 F (37 C)" semantics).
// Our FHIR projection emits a single valueQuantity, so we synthesize the
// counterpart unit at render time.
const LOINC_TEMPERATURE = "8310-5";

/** Convert °F → °C with one decimal. */
export function fahrenheitToCelsius(f: number): number {
    return Math.round(((f - 32) * 5) / 9 * 10) / 10;
}

/** Convert °C → °F with one decimal. */
export function celsiusToFahrenheit(c: number): number {
    return Math.round(((c * 9) / 5 + 32) * 10) / 10;
}

/** Detect Fahrenheit vs Celsius from a FHIR Quantity unit string. */
export function isFahrenheitUnit(unit?: string): boolean {
    if (!unit) return false;
    const u = unit.replace(/\s+/g, "").toLowerCase();
    return u === "f" || u === "°f" || u === "[degf]" || u.endsWith("f");
}
export function isCelsiusUnit(unit?: string): boolean {
    if (!unit) return false;
    const u = unit.replace(/\s+/g, "").toLowerCase();
    return u === "c" || u === "°c" || u === "cel" || u === "[degc]" || u.endsWith("c");
}

interface VitalsCardProps {
  patientUuid: string;
}

interface VitalsCardViewProps {
  observations: FhirObservation[];
}

/**
 * Pull the primary LOINC code off an Observation. We read
 * `code.coding[0].code` per the inventory contract; observations with
 * no coding are dropped (we have nothing to group them by).
 */
export function loincOf(observation: FhirObservation): string | null {
  const code = observation.code?.coding?.[0]?.code;
  return code && code.length > 0 ? code : null;
}

/**
 * Group observations by their primary LOINC and keep, per group, the
 * single observation with the latest `effectiveDateTime`. Observations
 * without an effectiveDateTime sort below any dated row in the same
 * group (so a dated row always wins). The container panel LOINC is
 * dropped — its leaf members are the rows we actually render.
 */
export function mostRecentByLoinc(
  observations: FhirObservation[],
): FhirObservation[] {
  const byLoinc = new Map<string, FhirObservation>();

  for (const obs of observations) {
    const code = loincOf(obs);
    if (!code) {
      continue;
    }
    if (code === PANEL_LOINC_VITAL_SIGNS) {
      continue;
    }
    const existing = byLoinc.get(code);
    if (!existing) {
      byLoinc.set(code, obs);
      continue;
    }
    if (isMoreRecent(obs, existing)) {
      byLoinc.set(code, obs);
    }
  }

  return Array.from(byLoinc.values());
}

function isMoreRecent(a: FhirObservation, b: FhirObservation): boolean {
  const aT = a.effectiveDateTime ? Date.parse(a.effectiveDateTime) : NaN;
  const bT = b.effectiveDateTime ? Date.parse(b.effectiveDateTime) : NaN;
  if (Number.isFinite(aT) && Number.isFinite(bT)) {
    return aT > bT;
  }
  if (Number.isFinite(aT)) {
    return true;
  }
  return false;
}

/**
 * Best-effort "what to show as the value" for a single Observation.
 *
 * The card is a quick read; we do not invent units. Priority:
 *   1. `valueQuantity.value` + `unit` (the common path — heart rate,
 *      respiratory rate, temperature, weight, etc.)
 *   2. `valueString` (free-text observation; rare for vitals)
 *   3. BP panel: synthesise `systolic/diastolic unit` from
 *      `component[]`.
 *   4. Otherwise null — the row falls back to a "—" placeholder so the
 *      card never crashes on a malformed entry.
 */
export function formatObservationValue(obs: FhirObservation): string | null {
  // BP panel: synthesise from components.
  if (loincOf(obs) === LOINC_BP_PANEL) {
    const sys = findComponentValue(obs, LOINC_BP_SYSTOLIC);
    const dia = findComponentValue(obs, LOINC_BP_DIASTOLIC);
    if (sys?.value != null && dia?.value != null) {
      const unit = sys.unit ?? dia.unit ?? "";
      return unit ? `${sys.value}/${dia.value} ${unit}` : `${sys.value}/${dia.value}`;
    }
  }

  const q = obs.valueQuantity;
  if (q?.value != null) {
    // Temperature: render both units. Source unit + value first, then
    // converted counterpart in parentheses. Mirrors original screenshot 16
    // ("98.6 F (37 C)").
    if (loincOf(obs) === LOINC_TEMPERATURE && q.unit) {
        if (isFahrenheitUnit(q.unit)) {
            return `${q.value} ${q.unit} (${fahrenheitToCelsius(q.value)} C)`;
        }
        if (isCelsiusUnit(q.unit)) {
            return `${q.value} ${q.unit} (${celsiusToFahrenheit(q.value)} F)`;
        }
    }
    return q.unit ? `${q.value} ${q.unit}` : `${q.value}`;
  }

  if (obs.valueString && obs.valueString.trim().length > 0) {
    return obs.valueString.trim();
  }

  return null;
}

function findComponentValue(
  obs: FhirObservation,
  loinc: string,
): { value: number; unit?: string } | null {
  for (const comp of obs.component ?? []) {
    if (comp.code?.coding?.[0]?.code === loinc && comp.valueQuantity?.value != null) {
      return {
        value: comp.valueQuantity.value,
        unit: comp.valueQuantity.unit,
      };
    }
  }
  return null;
}

/**
 * Display label for a single Observation row. Prefers the FHIR-supplied
 * `code.coding[0].display`, falling back to `code.text`, finally the raw
 * LOINC code as a last resort so the row is never label-less.
 */
export function displayLabel(obs: FhirObservation): string {
  const display = obs.code?.coding?.[0]?.display?.trim();
  if (display) {
    return display;
  }
  const text = obs.code?.text?.trim();
  if (text) {
    return text;
  }
  return loincOf(obs) ?? "Observation";
}

/**
 * Pick the latest `effectiveDateTime` across the kept observations and
 * format as `YYYY-MM-DD HH:mm` (UTC slice from the ISO string). Returns
 * null when none of the rows carry a dateTime.
 */
export function lastUpdatedLabel(observations: FhirObservation[]): string | null {
  let latest = -Infinity;
  let latestRaw: string | null = null;
  for (const obs of observations) {
    if (!obs.effectiveDateTime) {
      continue;
    }
    const t = Date.parse(obs.effectiveDateTime);
    if (Number.isFinite(t) && t > latest) {
      latest = t;
      latestRaw = obs.effectiveDateTime;
    }
  }
  if (!latestRaw) {
    return null;
  }
  // Slice on the ISO string: YYYY-MM-DDTHH:mm:ss... -> YYYY-MM-DD HH:mm
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(latestRaw);
  if (m) {
    return `${m[1]} ${m[2]}`;
  }
  return latestRaw;
}

/**
 * Loading skeleton companion. Mirrors the loaded card chrome (title +
 * pencil action) so the affordance is stable across states.
 */
export function VitalsCardSkeleton(): React.ReactElement {
  return (
    <Card size="sm" data-testid="vitals-card-skeleton" aria-busy="true">
      <CardHeader>
        <CardTitle>{CARD_TITLE}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <Skeleton className="h-3 w-1/2" />
        <Skeleton className="h-3 w-2/3" />
        <Skeleton className="h-3 w-1/2" />
        <Skeleton className="h-3 w-2/3" />
      </CardContent>
    </Card>
  );
}

/**
 * Pure-presentation view component, exported so tests can render it
 * directly without faking an async Server Component.
 */
export function VitalsCardView({
  observations,
}: VitalsCardViewProps): React.ReactElement {
  const rows = mostRecentByLoinc(observations);

  if (rows.length === 0) {
    return <EmptyCard title={CARD_TITLE} message="No vitals recorded" />;
  }

  // Stable order: sort by display label so renders are deterministic
  // for tests and the screen-reader sequence is alphabetical. The
  // original dashboard's row order is fixed (BP, Temp, ...); we don't
  // reproduce that ordering exactly because the inventory's seven-row
  // hardcoded sequence is form-specific, not LOINC-driven.
  rows.sort((a, b) => displayLabel(a).localeCompare(displayLabel(b)));

  const lastUpdated = lastUpdatedLabel(rows);

  return (
    <Card size="sm" data-testid="vitals-card">
      <CardHeader className="grid-cols-[1fr_auto] items-center">
        <CardTitle className="text-primary">{CARD_TITLE}</CardTitle>
        <button
          type="button"
          aria-label="Edit vitals"
          data-testid="vitals-edit"
          // Read-only stub per brief — full wiring lives in a later
          // phase. Original dashboard uses a "Trend" link rather than
          // a pencil; surfaced as a pencil here for chrome consistency
          // with the other cards.
          onClick={() => {
            console.log("[VitalsCard] TODO: wire edit affordance");
          }}
          className="inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-muted"
        >
          <Pencil className="h-4 w-4" aria-hidden="true" />
        </button>
      </CardHeader>
      <CardContent>
        {lastUpdated ? (
          <p
            className="mb-2 text-xs text-muted-foreground"
            data-testid="vitals-last-updated"
          >
            Last updated: {lastUpdated}
          </p>
        ) : null}
        <dl
          className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm"
          data-testid="vitals-list"
        >
          {rows.map((obs) => {
            const label = displayLabel(obs);
            const value = formatObservationValue(obs);
            return (
              <React.Fragment key={obs.id}>
                <dt
                  className="font-medium text-muted-foreground"
                  data-testid="vital-label"
                >
                  {label}
                </dt>
                <dd data-testid="vital-value">{value ?? "—"}</dd>
              </React.Fragment>
            );
          })}
        </dl>
      </CardContent>
    </Card>
  );
}

/**
 * Server Component entry point. Builds a FhirClient from the ambient
 * Auth.js session, fetches vital-signs Observations for the patient
 * uuid, and renders the view. Auth gating happens at the page level —
 * this card trusts that the FHIR token provider has a session to
 * consume.
 */
export default async function VitalsCard({
  patientUuid,
}: VitalsCardProps): Promise<React.ReactElement> {
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
    const observations = await client.getObservations(patientUuid, {
      category: "vital-signs",
    });
    return <VitalsCardView observations={observations} />;
  } catch (error) {
    console.error("[VitalsCard] FHIR fetch failed", error);
    return <ErrorCard title={CARD_TITLE} />;
  }
}

/**
 * PatientHeader — the page-level identity banner for a patient view.
 *
 * Pattern-setter for Phase 4. Every other dashboard card follows this
 * shape: an async Server Component that builds a FhirClient with the
 * Auth.js session token, fetches FHIR data server-side, and renders.
 * The FHIR access token never leaves the server.
 *
 * Spec sources (treat as authoritative — fields are not invented here):
 *   - patient-dashboard/dashboard-inventory.md  -> "Patient header"
 *   - patient-dashboard/dashboard-api-map.md    -> "Patient header"
 *   - patient-dashboard/reference-screenshots/10-card-header.png
 */
import * as React from "react";
import { History, Plus, User as UserIcon, X as XIcon } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorCard } from "@/components/cards/card-states";
import { FhirClient } from "@/lib/fhir/client";
import { authSessionTokenProvider } from "@/lib/fhir/token";
import type { FhirPatient } from "@/lib/fhir/types";

const HEADER_TITLE = "Patient";

interface PatientHeaderProps {
  patientId: string;
}

interface DisplayName {
  primary: string;
  hasName: boolean;
}

/**
 * Format a FHIR HumanName per inventory: `family, given1 given2...`.
 * If both family and given are missing we surface "Unknown patient" rather
 * than crashing — the route stays on this page and the rest of the
 * banner (MRN, DOB) still renders. This is the documented fallback.
 */
export function formatPatientName(patient: FhirPatient): DisplayName {
  const name = patient.name?.[0];
  const family = name?.family?.trim();
  const given = (name?.given ?? [])
    .map((g) => g.trim())
    .filter((g) => g.length > 0);

  if (family && given.length > 0) {
    return { primary: `${family}, ${given.join(" ")}`, hasName: true };
  }
  if (family) {
    return { primary: family, hasName: true };
  }
  if (given.length > 0) {
    return { primary: given.join(" "), hasName: true };
  }
  if (name?.text && name.text.trim().length > 0) {
    return { primary: name.text.trim(), hasName: true };
  }
  return { primary: "Unknown patient", hasName: false };
}

/**
 * Compute integer age in years from a YYYY-MM-DD birthDate against `now`.
 * Hand-rolled (no date library per scope). Handles the leap-day boundary:
 * a person born Feb 29 is one year older only on/after Mar 1 in non-leap
 * years.
 */
export function computeAge(birthDate: string, now: Date = new Date()): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(birthDate);
  if (!match) {
    return null;
  }
  const [, y, m, d] = match;
  const year = Number(y);
  const month = Number(m);
  const day = Number(d);
  if (
    !Number.isFinite(year) ||
    !Number.isFinite(month) ||
    !Number.isFinite(day) ||
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > 31
  ) {
    return null;
  }

  let age = now.getFullYear() - year;
  const monthDiff = now.getMonth() + 1 - month;
  if (monthDiff < 0 || (monthDiff === 0 && now.getDate() < day)) {
    age -= 1;
  }
  return age >= 0 ? age : null;
}

/**
 * Slim header-shaped skeleton used as the page-level Suspense fallback.
 * Headless (no card title) because the real header has no title bar
 * either — the patient name is the title.
 */
export function PatientHeaderSkeleton(): React.ReactElement {
  return (
    <Card size="sm" data-testid="patient-header-skeleton" aria-busy="true">
      <CardContent className="flex items-center gap-4 py-2">
        <Skeleton className="h-10 w-10 rounded-full" />
        <div className="flex flex-col gap-2">
          <Skeleton className="h-4 w-48" />
          <Skeleton className="h-3 w-32" />
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Skeleton className="h-8 w-44" />
          <Skeleton className="h-8 w-8 rounded-md" />
        </div>
      </CardContent>
    </Card>
  );
}

interface PatientHeaderViewProps {
  patientId: string;
  patient: FhirPatient;
}

/**
 * Pure-presentation view component, exported so the test suite can render
 * it directly without faking an async Server Component.
 */
export function PatientHeaderView({
  patientId,
  patient,
}: PatientHeaderViewProps): React.ReactElement {
  const { primary: displayName, hasName } = formatPatientName(patient);
  const dob = patient.birthDate ?? null;
  const age = dob ? computeAge(dob) : null;

  return (
    <Card
      size="sm"
      data-testid="patient-header"
      data-has-name={hasName ? "true" : "false"}
    >
      <CardContent className="flex flex-wrap items-center gap-4 py-2">
        {/* Avatar — placeholder silhouette. Patient.photo is documented as
            ⚠ NOT FOUND in dashboard-api-map.md (Q3 still outstanding); the
            original dashboard's pic_array() backing is non-FHIR. */}
        <div
          aria-hidden="true"
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground"
        >
          <UserIcon className="h-6 w-6" />
        </div>

        {/* Name + MRN block */}
        <div className="flex min-w-0 flex-col">
          <div className="flex items-center gap-2">
            <span
              className="truncate text-lg font-semibold text-primary"
              data-testid="patient-name"
            >
              {displayName}
            </span>
            <span
              className="text-sm text-muted-foreground"
              data-testid="patient-mrn"
            >
              ({patientId})
            </span>
            {/* Close button — STUB. Per dashboard-inventory.md "Close button":
                "x" icon clears the patient context. Targets the patient
                finder root for now; full wiring deferred to Phase 5. */}
            <a
              href="/"
              aria-label="Close patient"
              className="ml-1 inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-muted"
              data-testid="patient-close"
            >
              <XIcon className="h-4 w-4" />
            </a>
          </div>
          <div className="text-sm text-muted-foreground">
            {dob ? (
              <>
                <span data-testid="patient-dob">DOB: {dob}</span>
                {age !== null ? (
                  <>
                    <span> </span>
                    <span data-testid="patient-age">Age: {age}</span>
                  </>
                ) : null}
              </>
            ) : (
              <span data-testid="patient-dob-missing">DOB: unknown</span>
            )}
          </div>
        </div>

        {/* Encounter controls — STUB.
            Per dashboard-inventory.md "Encounter picker": shows recent-
            encounters icon + "Select Encounter (N)" select; (N) is the
            count. Wiring to the FHIR Encounter endpoint is out of scope
            for the read-only header per task brief. Rendered disabled
            with the literal "(1)" matching the reference screenshot for
            Gloria Tran. */}
        <div className="ml-auto flex flex-wrap items-center gap-3">
          <div
            className="flex items-center gap-1 rounded border bg-muted/40 px-2 py-1 text-sm text-muted-foreground"
            data-testid="encounter-picker-stub"
            aria-disabled="true"
          >
            <History className="h-4 w-4" aria-hidden="true" />
            <span>Select Encounter (1)</span>
          </div>
          <span
            className="text-sm text-muted-foreground"
            data-testid="open-encounter-stub"
          >
            Open Encounter: None
          </span>
          {/* New-encounter "+" button — STUB. Per inventory "New encounter":
              creates a new encounter (target URL TODO in inventory). */}
          <button
            type="button"
            aria-label="New encounter (not yet wired)"
            disabled
            className="inline-flex h-8 w-8 items-center justify-center rounded border text-muted-foreground opacity-60"
            data-testid="new-encounter-stub"
          >
            <Plus className="h-4 w-4" />
          </button>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * Server Component entry point. Builds a FhirClient from the ambient
 * Auth.js session, resolves pid -> FhirPatient, renders the view.
 *
 * Errors are caught and surfaced via ErrorCard so a transient FHIR
 * failure does not blank the entire page. A truly missing patient
 * (bundle empty) is rendered the same way — the inventory promises the
 * header is "always populated for a valid pid", so absence is an error
 * state from the user's perspective.
 */
export default async function PatientHeader({
  patientId,
}: PatientHeaderProps): Promise<React.ReactElement> {
  const baseUrl = process.env.OPENEMR_BASE_URL;
  if (!baseUrl) {
    return (
      <ErrorCard
        title={HEADER_TITLE}
        message="Server is not configured to reach OpenEMR."
      />
    );
  }

  try {
    const client = new FhirClient({
      baseUrl: `${baseUrl.replace(/\/+$/, "")}/apis/default/fhir`,
      tokenProvider: authSessionTokenProvider,
    });
    const patient = await client.getPatientByPid(patientId);
    if (!patient) {
      return (
        <ErrorCard
          title={HEADER_TITLE}
          message={`Patient ${patientId} was not found.`}
        />
      );
    }
    return <PatientHeaderView patientId={patientId} patient={patient} />;
  } catch (error) {
    console.error("[PatientHeader] FHIR fetch failed", error);
    return <ErrorCard title={HEADER_TITLE} />;
  }
}

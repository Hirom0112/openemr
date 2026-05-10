/**
 * Triage counts surfaced as a small inline ribbon under PatientHeader.
 *
 * Pure: no IO, no FHIR client. Counts are computed from already-fetched
 * resources so the helper can be called from a server component that has
 * already paid the fetch cost.
 */
import { filterMedications } from "@/lib/fhir/synthesis";
import type {
    FhirAllergyIntolerance,
    FhirCondition,
    FhirMedicationRequest,
} from "@/lib/fhir/types";

export interface TriageCounts {
    severeAllergies: number;
    activeProblems: number;
    activeMeds: number;
}

const PROBLEM_ACTIVE_STATUSES: ReadonlySet<string> = new Set([
    "active",
    "recurrence",
    "relapse",
]);

function problemStatusCode(c: FhirCondition): string | null {
    const code = c.clinicalStatus?.coding?.[0]?.code;
    return code ? code.toLowerCase() : null;
}

export function computeTriageCounts(
    allergies: readonly FhirAllergyIntolerance[],
    conditions: readonly FhirCondition[],
    medicationRequests: readonly FhirMedicationRequest[],
): TriageCounts {
    const severeAllergies = allergies.filter(
        (a) =>
            a.criticality === "high" ||
            a.reaction?.[0]?.severity === "severe",
    ).length;

    const activeProblems = conditions.filter((c) => {
        const code = problemStatusCode(c);
        if (code === null) return true;
        return PROBLEM_ACTIVE_STATUSES.has(code);
    }).length;

    const activeMeds = filterMedications(medicationRequests).filter(
        (m) => m.status === "active",
    ).length;

    return { severeAllergies, activeProblems, activeMeds };
}

function pluralize(count: number, singular: string, plural: string): string {
    return count === 1 ? `${count} ${singular}` : `${count} ${plural}`;
}

export function formatTriageCounts(counts: TriageCounts): string {
    const parts = [
        pluralize(counts.severeAllergies, "severe allergy", "severe allergies"),
        pluralize(counts.activeProblems, "active problem", "active problems"),
        pluralize(counts.activeMeds, "active med", "active meds"),
    ];
    return parts.join(" · ");
}

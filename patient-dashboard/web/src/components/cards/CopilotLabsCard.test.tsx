// @vitest-environment jsdom
/**
 * CopilotLabsCard tests.
 *
 * Fixtures captured from the live local FHIR endpoint
 * (`/apis/default/fhir/Observation?patient=...&category=laboratory`)
 * for patient pid 12 / uuid a1af78eb-0afd-425f-adb7-6d0fbe2e0b63 on
 * 2026-05-10. The full bundle returned 122 entries — 120 with the
 * "copilot-" id prefix, 2 native procedure_result rows. The card
 * filters to the copilot-prefixed subset; the tests below exercise
 * the discriminator + the four display columns + the empty state.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import {
    CopilotLabsCardView,
    copilotDate,
    copilotLabel,
    copilotSource,
    copilotValue,
    isCopilotObservation,
    sortByDateDesc,
} from "./CopilotLabsCard";
import type { FhirObservation } from "@/lib/fhir/types";

const copilotAlk: FhirObservation = {
    resourceType: "Observation",
    id: "copilot-392-LP-UNKNOWN",
    status: "final",
    code: {
        coding: [
            { system: "http://loinc.org", code: "LP-UNKNOWN", display: "alkaline phosphatase" },
        ],
    },
    effectiveDateTime: "2026-04-15T00:00:00+00:00",
    valueQuantity: { value: 102, unit: "U/L" },
    // The FHIR projection emits `note[].text` as "Source: <citation>".
    // Cast through unknown because FhirObservation's TS shape doesn't
    // declare `note` (the dashboard didn't need it before this card).
    ...({ note: [{ text: "Source: p1-b042" }] } as unknown as Partial<FhirObservation>),
};

const copilotAlt: FhirObservation = {
    resourceType: "Observation",
    id: "copilot-348-1742-6",
    status: "final",
    code: {
        coding: [
            {
                system: "http://loinc.org",
                code: "1742-6",
                display: "Alanine aminotransferase [Enzymatic activity/volume]",
            },
        ],
    },
    effectiveDateTime: "2026-03-12T00:00:00+00:00",
    valueQuantity: { value: 56, unit: "U/L" },
};

const nativeLab: FhirObservation = {
    resourceType: "Observation",
    id: "a1b62700-0000-0000-0000-000000000001",
    status: "final",
    code: { coding: [{ system: "http://loinc.org", code: "2345-7", display: "Glucose" }] },
    effectiveDateTime: "2025-01-01T00:00:00+00:00",
    valueQuantity: { value: 95, unit: "mg/dL" },
};

describe("isCopilotObservation", () => {
    it("matches copilot-prefixed ids", () => {
        expect(isCopilotObservation(copilotAlk)).toBe(true);
        expect(isCopilotObservation(copilotAlt)).toBe(true);
    });
    it("rejects native FHIR uuids", () => {
        expect(isCopilotObservation(nativeLab)).toBe(false);
    });
});

describe("display helpers", () => {
    it("copilotLabel prefers code.coding[0].display", () => {
        expect(copilotLabel(copilotAlk)).toBe("alkaline phosphatase");
    });
    it("copilotValue formats valueQuantity with unit", () => {
        expect(copilotValue(copilotAlk)).toBe("102 U/L");
    });
    it("copilotValue falls back to em-dash when nothing renderable", () => {
        const empty: FhirObservation = {
            resourceType: "Observation",
            id: "copilot-x-y",
            status: "final",
            code: {},
        };
        expect(copilotValue(empty)).toBe("—");
    });
    it("copilotDate slices ISO to YYYY-MM-DD", () => {
        expect(copilotDate(copilotAlk)).toBe("2026-04-15");
    });
    it("copilotDate returns null when missing", () => {
        const noDate = { ...copilotAlk, effectiveDateTime: undefined };
        expect(copilotDate(noDate)).toBeNull();
    });
    it("copilotSource pulls first note text", () => {
        expect(copilotSource(copilotAlk)).toBe("Source: p1-b042");
    });
    it("copilotSource returns null when no note", () => {
        expect(copilotSource(copilotAlt)).toBeNull();
    });
});

describe("sortByDateDesc", () => {
    it("orders most-recent first", () => {
        const sorted = sortByDateDesc([copilotAlt, copilotAlk]);
        expect(sorted.map((o) => o.id)).toEqual([copilotAlk.id, copilotAlt.id]);
    });
});

describe("CopilotLabsCardView", () => {
    it("renders the empty state when no copilot rows are present", () => {
        render(<CopilotLabsCardView observations={[nativeLab]} />);
        expect(screen.getByTestId("empty-card")).toBeTruthy();
        expect(screen.queryByTestId("copilot-labs-card")).toBeNull();
    });

    it("renders a row per copilot Observation, native rows excluded", () => {
        render(
            <CopilotLabsCardView observations={[nativeLab, copilotAlk, copilotAlt]} />,
        );
        const rows = screen.getAllByTestId("copilot-lab-row");
        expect(rows).toHaveLength(2);
        // Date desc — alk (2026-04-15) should come before alt (2026-03-12).
        expect(rows[0].textContent).toContain("alkaline phosphatase");
        expect(rows[0].textContent).toContain("102 U/L");
        expect(rows[0].textContent).toContain("2026-04-15");
        expect(rows[0].textContent).toContain("Source: p1-b042");
    });
});

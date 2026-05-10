// @vitest-environment jsdom
/**
 * CopilotConditionsCard tests. Mirrors CopilotLabsCard.test pattern.
 *
 * Fixture row shape captured from the local FHIR endpoint during the
 * Phase 6.3 verification walk (2026-05-10): a Condition POSTed via
 * `oe-module-clinical-copilot/public/condition.php` lands in
 * `copilot_conditions` and surfaces via FhirConditionCopilotService
 * with id like "copilot-999-phase63-test", clinicalStatus.code "active",
 * onsetDateTime "2024-03-15T00:00:00+00:00", and a coding entry for
 * ICD-10 ("I10").
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import {
    CopilotConditionsCardView,
    copilotConditionCode,
    copilotConditionLabel,
    copilotConditionOnset,
    copilotConditionSource,
    copilotConditionStatus,
    isCopilotCondition,
    sortConditionsByDateDesc,
} from "./CopilotConditionsCard";
import type { FhirCondition } from "@/lib/fhir/types";

const copilotHtn: FhirCondition = {
    resourceType: "Condition",
    id: "copilot-999-phase63-test",
    clinicalStatus: {
        coding: [
            {
                system: "http://terminology.hl7.org/CodeSystem/condition-clinical",
                code: "active",
            },
        ],
    },
    code: {
        text: "Essential hypertension",
        coding: [{ system: "http://hl7.org/fhir/sid/icd-10-cm", code: "I10", display: "Essential hypertension" }],
    },
    onsetDateTime: "2024-03-15T00:00:00+00:00",
    ...({ note: [{ text: "Source: p12-b001" }] } as unknown as Partial<FhirCondition>),
};

const copilotDm2: FhirCondition = {
    resourceType: "Condition",
    id: "copilot-1042-E11-9",
    clinicalStatus: {
        coding: [{ code: "active" }],
    },
    code: {
        coding: [
            { system: "http://hl7.org/fhir/sid/icd-10-cm", code: "E11.9", display: "Type 2 diabetes mellitus" },
        ],
    },
    recordedDate: "2023-06-01T00:00:00+00:00",
};

const nativeListsRow: FhirCondition = {
    resourceType: "Condition",
    id: "a1b62700-0000-0000-0000-000000000001",
    clinicalStatus: { coding: [{ code: "active" }] },
    code: { text: "Asthma" },
    onsetDateTime: "2025-01-01T00:00:00+00:00",
};

describe("isCopilotCondition", () => {
    it("matches copilot-prefixed ids", () => {
        expect(isCopilotCondition(copilotHtn)).toBe(true);
        expect(isCopilotCondition(copilotDm2)).toBe(true);
    });
    it("rejects native FHIR uuids", () => {
        expect(isCopilotCondition(nativeListsRow)).toBe(false);
    });
});

describe("display helpers", () => {
    it("copilotConditionLabel prefers code.text", () => {
        expect(copilotConditionLabel(copilotHtn)).toBe("Essential hypertension");
    });
    it("copilotConditionLabel falls back to coding display when no text", () => {
        expect(copilotConditionLabel(copilotDm2)).toBe("Type 2 diabetes mellitus");
    });
    it("copilotConditionCode returns the first coding.code", () => {
        expect(copilotConditionCode(copilotHtn)).toBe("I10");
        expect(copilotConditionCode(copilotDm2)).toBe("E11.9");
    });
    it("copilotConditionStatus reads clinicalStatus.coding[0].code", () => {
        expect(copilotConditionStatus(copilotHtn)).toBe("active");
    });
    it("copilotConditionStatus returns em-dash when missing", () => {
        const noStatus: FhirCondition = { ...copilotHtn, clinicalStatus: undefined };
        expect(copilotConditionStatus(noStatus)).toBe("—");
    });
    it("copilotConditionOnset slices ISO to YYYY-MM-DD", () => {
        expect(copilotConditionOnset(copilotHtn)).toBe("2024-03-15");
    });
    it("copilotConditionOnset falls back to recordedDate", () => {
        expect(copilotConditionOnset(copilotDm2)).toBe("2023-06-01");
    });
    it("copilotConditionOnset returns null when both missing", () => {
        const noDate: FhirCondition = {
            ...copilotHtn,
            onsetDateTime: undefined,
            recordedDate: undefined,
        };
        expect(copilotConditionOnset(noDate)).toBeNull();
    });
    it("copilotConditionSource pulls first note text", () => {
        expect(copilotConditionSource(copilotHtn)).toBe("Source: p12-b001");
    });
    it("copilotConditionSource returns null when no note", () => {
        expect(copilotConditionSource(copilotDm2)).toBeNull();
    });
});

describe("sortConditionsByDateDesc", () => {
    it("orders most-recent first across onsetDateTime + recordedDate", () => {
        const sorted = sortConditionsByDateDesc([copilotDm2, copilotHtn]);
        expect(sorted.map((c) => c.id)).toEqual([copilotHtn.id, copilotDm2.id]);
    });
});

describe("CopilotConditionsCardView", () => {
    it("renders the empty state when no copilot rows are present", () => {
        render(<CopilotConditionsCardView conditions={[nativeListsRow]} />);
        expect(screen.getByTestId("empty-card")).toBeTruthy();
        expect(screen.queryByTestId("copilot-conditions-card")).toBeNull();
    });

    it("renders a row per copilot Condition, native rows excluded", () => {
        render(
            <CopilotConditionsCardView
                conditions={[nativeListsRow, copilotDm2, copilotHtn]}
            />,
        );
        const rows = screen.getAllByTestId("copilot-condition-row");
        expect(rows).toHaveLength(2);
        // Date desc — htn (2024-03-15) before dm2 (2023-06-01).
        expect(rows[0].textContent).toContain("Essential hypertension");
        expect(rows[0].textContent).toContain("I10");
        expect(rows[0].textContent).toContain("active");
        expect(rows[0].textContent).toContain("2024-03-15");
        expect(rows[0].textContent).toContain("Source: p12-b001");
    });
});

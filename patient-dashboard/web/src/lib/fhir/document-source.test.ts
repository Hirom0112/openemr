/**
 * Unit tests for the DocumentSource parser helpers. The two FHIR projections
 * the Co-Pilot module emits — Observation (`derivedFrom` + `note`) and
 * Condition (combined note text) — encode the same provenance differently;
 * these tests pin the parser against both shapes.
 */
import { describe, expect, it } from "vitest";

import {
  parseConditionSource,
  parseObservationSource,
  hasDocumentRef,
} from "./document-source";
import type { FhirCondition, FhirObservation } from "./types";

describe("parseObservationSource", () => {
  it("pulls uuid from derivedFrom and locator from note", () => {
    const obs: FhirObservation = {
      resourceType: "Observation",
      id: "copilot-12-1",
      status: "final",
      code: { text: "Glucose" },
      derivedFrom: [
        { reference: "DocumentReference/abcdef0123456789abcdef0123456789" },
      ],
      note: [{ text: "Source: p1-b042" }],
    };
    const parsed = parseObservationSource(obs);
    expect(parsed?.uuid).toBe("abcdef0123456789abcdef0123456789");
    expect(parsed?.locator).toBe("Source: p1-b042");
    expect(hasDocumentRef(parsed)).toBe(true);
  });

  it("returns null uuid when only the note is present", () => {
    const obs: FhirObservation = {
      resourceType: "Observation",
      id: "copilot-12-2",
      status: "final",
      code: { text: "Sodium" },
      note: [{ text: "Source: p2-b001" }],
    };
    const parsed = parseObservationSource(obs);
    expect(parsed?.uuid).toBe("");
    expect(parsed?.locator).toBe("Source: p2-b001");
    expect(hasDocumentRef(parsed)).toBe(false);
  });

  it("returns null when neither note nor derivedFrom is present", () => {
    const obs: FhirObservation = {
      resourceType: "Observation",
      id: "x",
      status: "final",
      code: { text: "x" },
    };
    expect(parseObservationSource(obs)).toBeNull();
  });
});

describe("parseConditionSource", () => {
  it("parses a combined DocumentReference + Source note", () => {
    const c: FhirCondition = {
      resourceType: "Condition",
      id: "copilot-12-1",
      note: [
        {
          text:
            "DocumentReference/0123456789abcdef0123456789abcdef · Source: p12-b001",
        },
      ],
    };
    const parsed = parseConditionSource(c);
    expect(parsed?.uuid).toBe("0123456789abcdef0123456789abcdef");
    expect(parsed?.locator).toBe("Source: p12-b001");
    expect(hasDocumentRef(parsed)).toBe(true);
  });

  it("falls back to uuid='' when the note has only a citation", () => {
    const c: FhirCondition = {
      resourceType: "Condition",
      id: "copilot-12-2",
      note: [{ text: "Source: p12-b001" }],
    };
    const parsed = parseConditionSource(c);
    expect(parsed?.uuid).toBe("");
    expect(parsed?.locator).toBe("Source: p12-b001");
    expect(hasDocumentRef(parsed)).toBe(false);
  });

  it("returns null when no note is attached", () => {
    const c: FhirCondition = {
      resourceType: "Condition",
      id: "copilot-12-3",
    };
    expect(parseConditionSource(c)).toBeNull();
  });
});

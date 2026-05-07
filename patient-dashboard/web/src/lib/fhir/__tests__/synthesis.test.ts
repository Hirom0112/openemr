import { describe, it, expect } from "vitest";
import { filterMedications, filterPrescriptions } from "../synthesis";
import type { FhirMedicationRequest } from "../types";

function mkReq(
  partial: Partial<FhirMedicationRequest> & {
    status: FhirMedicationRequest["status"];
    intent: FhirMedicationRequest["intent"];
  },
): FhirMedicationRequest {
  return {
    resourceType: "MedicationRequest",
    ...partial,
    id: partial.id ?? "rx-1",
    status: partial.status,
    intent: partial.intent,
  };
}

describe("filterMedications", () => {
  it("keeps active, on-hold, and completed", () => {
    const reqs = [
      mkReq({ id: "1", status: "active", intent: "plan" }),
      mkReq({ id: "2", status: "on-hold", intent: "plan" }),
      mkReq({ id: "3", status: "completed", intent: "order" }),
      mkReq({ id: "4", status: "stopped", intent: "order" }),
      mkReq({ id: "5", status: "cancelled", intent: "plan" }),
      mkReq({ id: "6", status: "entered-in-error", intent: "plan" }),
    ];

    const out = filterMedications(reqs).map((r) => r.id);
    expect(out).toEqual(["1", "2", "3"]);
  });

  it("returns empty for empty input", () => {
    expect(filterMedications([])).toEqual([]);
  });
});

describe("filterPrescriptions", () => {
  it("requires intent=order, status=active, and a requester", () => {
    const reqs = [
      mkReq({
        id: "ok",
        status: "active",
        intent: "order",
        requester: { reference: "Practitioner/1", display: "Dr. Strange" },
      }),
      mkReq({
        id: "wrong-intent",
        status: "active",
        intent: "plan",
        requester: { reference: "Practitioner/1" },
      }),
      mkReq({
        id: "wrong-status",
        status: "completed",
        intent: "order",
        requester: { reference: "Practitioner/1" },
      }),
      mkReq({ id: "no-requester", status: "active", intent: "order" }),
    ];

    const out = filterPrescriptions(reqs).map((r) => r.id);
    expect(out).toEqual(["ok"]);
  });

  it("returns empty when every entry is intent=plan (the 1.5 empirical case)", () => {
    // Synthetic dataset across pids 4, 5, 13, 24, 26, 27 emits intent=plan
    // with requester absent on every MedicationRequest row.
    const reqs = [
      mkReq({ id: "1", status: "active", intent: "plan" }),
      mkReq({ id: "2", status: "active", intent: "plan" }),
      mkReq({ id: "3", status: "completed", intent: "plan" }),
    ];

    expect(filterPrescriptions(reqs)).toEqual([]);
  });
});

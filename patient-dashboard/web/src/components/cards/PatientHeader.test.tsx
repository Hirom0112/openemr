// @vitest-environment jsdom
/**
 * PatientHeader render + helpers tests.
 *
 * Per-file `@vitest-environment jsdom` keeps the rest of the suite running
 * under `node` (the FhirClient tests in src/lib/fhir/__tests__ depend on
 * the node environment).
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  PatientHeaderView,
  computeAge,
  formatPatientName,
} from "./PatientHeader";
import type { FhirPatient } from "@/lib/fhir/types";

const gloria: FhirPatient = {
  resourceType: "Patient",
  id: "a1af78de-c153-42e7-89b2-55749a3da9ca",
  identifier: [{ system: "pubpid", value: "4" }],
  name: [{ family: "Tran", given: ["Gloria"] }],
  birthDate: "1958-09-03",
  gender: "female",
};

describe("PatientHeaderView (render)", () => {
  it("renders name, MRN, DOB, and computed age for a fully-populated patient", () => {
    // Pin date so age is deterministic regardless of when the test runs.
    // Computed against 2026-05-07 (the date the spec was captured).
    const expectedAge = computeAge(
      gloria.birthDate as string,
      new Date("2026-05-07T00:00:00Z"),
    );
    render(<PatientHeaderView patientId="4" patient={gloria} />);

    expect(screen.getByTestId("patient-name").textContent).toBe("Tran, Gloria");
    expect(screen.getByTestId("patient-mrn").textContent).toBe("(4)");
    expect(screen.getByTestId("patient-dob").textContent).toBe(
      "DOB: 1958-09-03",
    );
    // Computed age helper is independently tested below; here we just
    // verify it surfaces in the DOM.
    expect(screen.getByTestId("patient-age").textContent).toBe(
      `Age: ${expectedAge}`,
    );
  });

  it("renders the encounter and open-encounter stubs per the reference screenshot", () => {
    render(<PatientHeaderView patientId="4" patient={gloria} />);

    expect(screen.getByTestId("encounter-picker-stub").textContent).toContain(
      "Select Encounter (1)",
    );
    expect(screen.getByTestId("open-encounter-stub").textContent).toBe(
      "Open Encounter: None",
    );
  });

  it("falls back gracefully when the patient has no name", () => {
    const nameless: FhirPatient = {
      resourceType: "Patient",
      id: "no-name-uuid",
      birthDate: "1990-01-15",
    };
    render(<PatientHeaderView patientId="99" patient={nameless} />);

    // Fallback label is shown; the rest of the banner still renders.
    expect(screen.getByTestId("patient-name").textContent).toBe(
      "Unknown patient",
    );
    expect(screen.getByTestId("patient-mrn").textContent).toBe("(99)");
    expect(screen.getByTestId("patient-dob").textContent).toBe(
      "DOB: 1990-01-15",
    );
    // hasName attribute lets stylistic/analytic consumers detect the fallback.
    expect(
      screen.getByTestId("patient-header").getAttribute("data-has-name"),
    ).toBe("false");
  });

  it("hides DOB/Age when birthDate is missing without crashing", () => {
    const noDob: FhirPatient = {
      resourceType: "Patient",
      id: "no-dob-uuid",
      name: [{ family: "Doe", given: ["Jane"] }],
    };
    render(<PatientHeaderView patientId="5" patient={noDob} />);

    expect(screen.getByTestId("patient-dob-missing")).toBeTruthy();
    expect(screen.queryByTestId("patient-age")).toBeNull();
  });
});

describe("formatPatientName", () => {
  it("formats family + given as 'family, given1 given2'", () => {
    expect(
      formatPatientName({
        resourceType: "Patient",
        id: "x",
        name: [{ family: "Tran", given: ["Gloria", "M."] }],
      }).primary,
    ).toBe("Tran, Gloria M.");
  });

  it("falls back to family-only or given-only when one side is missing", () => {
    expect(
      formatPatientName({
        resourceType: "Patient",
        id: "x",
        name: [{ family: "Cher" }],
      }).primary,
    ).toBe("Cher");
    expect(
      formatPatientName({
        resourceType: "Patient",
        id: "x",
        name: [{ given: ["Madonna"] }],
      }).primary,
    ).toBe("Madonna");
  });

  it("uses name.text as a last resort, then 'Unknown patient'", () => {
    expect(
      formatPatientName({
        resourceType: "Patient",
        id: "x",
        name: [{ text: "  Patient Zero  " }],
      }).primary,
    ).toBe("Patient Zero");
    expect(
      formatPatientName({ resourceType: "Patient", id: "x" }).primary,
    ).toBe("Unknown patient");
  });
});

describe("computeAge", () => {
  it("returns 67 for Gloria Tran (1958-09-03) on 2026-05-07", () => {
    expect(computeAge("1958-09-03", new Date("2026-05-07T00:00:00Z"))).toBe(67);
  });

  it("does not bump age until the birthday has passed in the current year", () => {
    // Day before birthday: still 39.
    expect(computeAge("1985-06-15", new Date("2025-06-14T12:00:00Z"))).toBe(39);
    // On birthday: 40.
    expect(computeAge("1985-06-15", new Date("2025-06-15T12:00:00Z"))).toBe(40);
    // Day after: 40.
    expect(computeAge("1985-06-15", new Date("2025-06-16T12:00:00Z"))).toBe(40);
  });

  it("handles the leap-day boundary (Feb 29 -> non-leap years)", () => {
    // Born 2000-02-29. On 2025-02-28 (non-leap), birthday hasn't 'happened'
    // yet — age is 24. On 2025-03-01, age is 25.
    expect(computeAge("2000-02-29", new Date("2025-02-28T12:00:00Z"))).toBe(24);
    expect(computeAge("2000-02-29", new Date("2025-03-01T12:00:00Z"))).toBe(25);
    // On a leap year, Feb 29 itself bumps the age.
    expect(computeAge("2000-02-29", new Date("2024-02-29T12:00:00Z"))).toBe(24);
  });

  it("returns null for malformed birth dates", () => {
    expect(computeAge("not-a-date")).toBeNull();
    expect(computeAge("1990-13-01")).toBeNull();
    expect(computeAge("")).toBeNull();
  });
});

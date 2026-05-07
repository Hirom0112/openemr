import { describe, it, expect } from "vitest";
import { allergyDisplay } from "../allergy-display";
import type { FhirAllergyIntolerance } from "../types";

function mkAllergy(
  partial: Partial<FhirAllergyIntolerance>,
): FhirAllergyIntolerance {
  return {
    resourceType: "AllergyIntolerance",
    id: "ai-1",
    ...partial,
  };
}

describe("allergyDisplay", () => {
  it("prefers code.text when present", () => {
    const a = mkAllergy({
      code: {
        text: "Peanut",
        coding: [{ display: "Peanut allergen" }],
      },
    });
    expect(allergyDisplay(a)).toBe("Peanut");
  });

  it("uses coding[].display when code.text is missing", () => {
    const a = mkAllergy({
      code: { coding: [{ display: "Penicillin" }] },
    });
    expect(allergyDisplay(a)).toBe("Penicillin");
  });

  it("falls back to text.div when coding[].display is literally 'Unknown' (Gloria's record)", () => {
    // 1.5 verification: Gloria's Sulfonamide allergy emits
    // code.coding[0].display = "Unknown" (data-absent-reason) and stashes
    // the actual allergen name in text.div as XHTML.
    const a = mkAllergy({
      code: {
        coding: [{ system: "data-absent-reason", code: "unknown", display: "Unknown" }],
      },
      text: {
        status: "generated",
        div: '<div xmlns="http://www.w3.org/1999/xhtml">Sulfonamide</div>',
      },
    });
    expect(allergyDisplay(a)).toBe("Sulfonamide");
  });

  it("returns 'Unknown allergen' when nothing is populated", () => {
    expect(allergyDisplay(mkAllergy({}))).toBe("Unknown allergen");
  });

  it("decodes basic HTML entities in text.div", () => {
    const a = mkAllergy({
      text: {
        div: "<div>Aspirin &amp; NSAIDs</div>",
      },
    });
    expect(allergyDisplay(a)).toBe("Aspirin & NSAIDs");
  });
});

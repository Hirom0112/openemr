import { describe, it, expect, vi, beforeEach } from "vitest";
import { FhirClient } from "../client";
import { FhirApiError } from "../error";
import { staticTokenProvider } from "../token";
import type { FhirBundle, FhirPatient } from "../types";

const BASE = "https://localhost:9300/apis/default/fhir";
const TOKEN = "test-bearer-token-1234";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/fhir+json" },
  });
}

function makeClient(fetchImpl: typeof fetch): FhirClient {
  return new FhirClient({
    baseUrl: BASE,
    tokenProvider: staticTokenProvider(TOKEN),
    fetchImpl,
  });
}

const gloriaPatient: FhirPatient = {
  resourceType: "Patient",
  id: "a1af78de-c153-42e7-89b2-55749a3da9ca",
  name: [{ family: "Tran", given: ["Gloria"] }],
  birthDate: "1958-09-03",
  gender: "female",
};

describe("FhirClient.getPatientByPid", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("returns the first patient resource from a one-entry bundle", async () => {
    const bundle: FhirBundle<FhirPatient> = {
      resourceType: "Bundle",
      total: 1,
      entry: [{ resource: gloriaPatient }],
    };
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(bundle));
    const client = makeClient(fetchImpl as unknown as typeof fetch);

    const patient = await client.getPatientByPid("4");

    expect(patient).not.toBeNull();
    expect(patient?.id).toBe("a1af78de-c153-42e7-89b2-55749a3da9ca");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${BASE}/Patient?identifier=4`);
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBe(`Bearer ${TOKEN}`);
  });

  it("returns null on an empty bundle", async () => {
    const bundle: FhirBundle<FhirPatient> = {
      resourceType: "Bundle",
      total: 0,
      entry: [],
    };
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(bundle));
    const client = makeClient(fetchImpl as unknown as typeof fetch);

    const patient = await client.getPatientByPid("99999");

    expect(patient).toBeNull();
  });
});

describe("FhirClient.request error handling", () => {
  it("throws FhirApiError with status=401 on unauthorized", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(jsonResponse({ resourceType: "OperationOutcome" }, 401));
    const client = makeClient(fetchImpl as unknown as typeof fetch);

    await expect(client.getPatient("uuid-x")).rejects.toMatchObject({
      name: "FhirApiError",
      status: 401,
    });
  });

  it("throws FhirApiError with status=500 on server error", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      new Response("boom", { status: 500 }),
    );
    const client = makeClient(fetchImpl as unknown as typeof fetch);

    const err = await client.getPatient("uuid-x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(FhirApiError);
    expect((err as FhirApiError).status).toBe(500);
  });

  it("throws FhirApiError when fetch rejects (network failure)", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error("ECONNREFUSED"));
    const client = makeClient(fetchImpl as unknown as typeof fetch);

    const err = await client.getPatient("uuid-x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(FhirApiError);
    expect((err as FhirApiError).status).toBe(0);
    expect((err as FhirApiError).cause).toBeInstanceOf(Error);
  });

  it("attaches the bearer token from the token provider on every call", async () => {
    const fetchImpl = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(jsonResponse({ resourceType: "Bundle", entry: [] })),
      );
    const client = makeClient(fetchImpl as unknown as typeof fetch);

    await client.getAllergyIntolerances("uuid-x");
    await client.getCareTeams("uuid-x");

    expect(fetchImpl).toHaveBeenCalledTimes(2);
    for (const call of fetchImpl.mock.calls) {
      const init = call[1] as RequestInit;
      const headers = new Headers(init.headers);
      expect(headers.get("Authorization")).toBe(`Bearer ${TOKEN}`);
    }
  });
});

describe("FhirClient query parameter assembly", () => {
  it("includes category on Condition when provided", async () => {
    const fetchImpl = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(jsonResponse({ resourceType: "Bundle", entry: [] })),
      );
    const client = makeClient(fetchImpl as unknown as typeof fetch);

    await client.getConditions("uuid-x", { category: "problem-list-item" });

    const [url] = fetchImpl.mock.calls[0] as [string];
    expect(url).toContain("patient=uuid-x");
    expect(url).toContain("category=problem-list-item");
  });

  it("includes _sort and _count on Observation when provided", async () => {
    const fetchImpl = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(jsonResponse({ resourceType: "Bundle", entry: [] })),
      );
    const client = makeClient(fetchImpl as unknown as typeof fetch);

    await client.getObservations("uuid-x", {
      category: "vital-signs",
      sort: "-date",
      count: 1,
    });

    const [url] = fetchImpl.mock.calls[0] as [string];
    expect(url).toContain("category=vital-signs");
    expect(url).toContain("_sort=-date");
    expect(url).toContain("_count=1");
  });
});

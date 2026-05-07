/**
 * FhirClient — typed access layer for OpenEMR's FHIR R4 / US Core 3.1.1
 * endpoints under `/apis/default/fhir`. Built from the verified shapes in
 * `dashboard-api-map.md` (1.5 verification, 2026-05-07) and the
 * synthesis rules in `dashboard-inventory.md`.
 *
 * 401 handling: this client does NOT refresh tokens. Auth.js (Phase 3.2)
 * refreshes lazily inside its JWT callback. The client's job is to surface
 * 401s as `FhirApiError(status=401)` so the card boundary can render a
 * re-auth state. Implementing refresh here would race the Auth.js callback
 * and mask token-rotation bugs.
 */

import { FhirApiError } from "./error";
import type { TokenProvider } from "./token";
import type {
  FhirAllergyIntolerance,
  FhirBundle,
  FhirCareTeam,
  FhirCondition,
  FhirConditionCategory,
  FhirMedicationRequest,
  FhirMedicationRequestIntent,
  FhirObservation,
  FhirObservationCategory,
  FhirPatient,
} from "./types";

export interface FhirClientOptions {
  baseUrl: string;
  tokenProvider: TokenProvider;
  fetchImpl?: typeof fetch;
}

export class FhirClient {
  private readonly baseUrl: string;
  private readonly tokenProvider: TokenProvider;
  private readonly fetchImpl: typeof fetch;

  constructor(options: FhirClientOptions) {
    // Strip a trailing slash so callers can pass either form.
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.tokenProvider = options.tokenProvider;
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  /**
   * Resolve a legacy numeric pid (from the dashboard URL) to a Patient
   * resource. Returns null when the bundle is empty (unknown pid).
   *
   * 1.5 verification: `GET /Patient?identifier={pid}` returns a one-entry
   * bundle whose `entry[0].resource.id` is the FHIR uuid.
   */
  async getPatientByPid(pid: string): Promise<FhirPatient | null> {
    const bundle = await this.request<FhirBundle<FhirPatient>>(
      `/Patient?identifier=${encodeURIComponent(pid)}`,
    );
    const entry = bundle.entry?.[0];
    return entry?.resource ?? null;
  }

  async getPatient(uuid: string): Promise<FhirPatient> {
    return this.request<FhirPatient>(`/Patient/${encodeURIComponent(uuid)}`);
  }

  async getAllergyIntolerances(
    patientUuid: string,
  ): Promise<FhirAllergyIntolerance[]> {
    const bundle = await this.request<FhirBundle<FhirAllergyIntolerance>>(
      `/AllergyIntolerance?patient=${encodeURIComponent(patientUuid)}`,
    );
    return bundle.entry?.map((e) => e.resource) ?? [];
  }

  async getConditions(
    patientUuid: string,
    opts?: { category?: FhirConditionCategory },
  ): Promise<FhirCondition[]> {
    const params = new URLSearchParams({ patient: patientUuid });
    if (opts?.category) {
      params.set("category", opts.category);
    }
    const bundle = await this.request<FhirBundle<FhirCondition>>(
      `/Condition?${params.toString()}`,
    );
    return bundle.entry?.map((e) => e.resource) ?? [];
  }

  async getMedicationRequests(
    patientUuid: string,
    opts?: { intent?: FhirMedicationRequestIntent; status?: string[] },
  ): Promise<FhirMedicationRequest[]> {
    const params = new URLSearchParams({ patient: patientUuid });
    if (opts?.intent) {
      params.set("intent", opts.intent);
    }
    if (opts?.status && opts.status.length > 0) {
      params.set("status", opts.status.join(","));
    }
    const bundle = await this.request<FhirBundle<FhirMedicationRequest>>(
      `/MedicationRequest?${params.toString()}`,
    );
    return bundle.entry?.map((e) => e.resource) ?? [];
  }

  async getCareTeams(patientUuid: string): Promise<FhirCareTeam[]> {
    const bundle = await this.request<FhirBundle<FhirCareTeam>>(
      `/CareTeam?patient=${encodeURIComponent(patientUuid)}&status=active`,
    );
    return bundle.entry?.map((e) => e.resource) ?? [];
  }

  async getObservations(
    patientUuid: string,
    opts?: {
      category?: FhirObservationCategory;
      sort?: string;
      count?: number;
    },
  ): Promise<FhirObservation[]> {
    const params = new URLSearchParams({ patient: patientUuid });
    if (opts?.category) {
      params.set("category", opts.category);
    }
    if (opts?.sort) {
      params.set("_sort", opts.sort);
    }
    if (typeof opts?.count === "number") {
      params.set("_count", String(opts.count));
    }
    const bundle = await this.request<FhirBundle<FhirObservation>>(
      `/Observation?${params.toString()}`,
    );
    return bundle.entry?.map((e) => e.resource) ?? [];
  }

  /**
   * Low-level request primitive. Attaches `Authorization: Bearer <token>`,
   * accepts JSON, throws `FhirApiError` on non-2xx or transport failure.
   *
   * Marked public-ish (not protected) so cards with bespoke needs (e.g. an
   * Encounter count, paging follow-up) can drop down to it without
   * subclassing. The high-level methods are still the preferred entry points.
   */
  async request<T>(path: string, init?: RequestInit): Promise<T> {
    const token = await this.tokenProvider.getAccessToken();
    const url = path.startsWith("http")
      ? path
      : `${this.baseUrl}${path.startsWith("/") ? path : `/${path}`}`;

    const headers = new Headers(init?.headers);
    headers.set("Authorization", `Bearer ${token}`);
    if (!headers.has("Accept")) {
      headers.set("Accept", "application/fhir+json, application/json");
    }

    let response: Response;
    try {
      response = await this.fetchImpl(url, { ...init, headers });
    } catch (cause) {
      throw new FhirApiError("FHIR network request failed", {
        status: 0,
        cause,
      });
    }

    if (!response.ok) {
      let outcome: unknown = undefined;
      try {
        outcome = await response.clone().json();
      } catch {
        // body wasn't JSON — leave outcome undefined.
      }
      throw new FhirApiError(
        `FHIR request failed with status ${response.status}`,
        {
          status: response.status,
          fhirOperationOutcome: outcome,
        },
      );
    }

    try {
      return (await response.json()) as T;
    } catch (cause) {
      throw new FhirApiError("FHIR response was not valid JSON", {
        status: response.status,
        cause,
      });
    }
  }
}

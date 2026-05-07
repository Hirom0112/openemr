/**
 * Narrow TypeScript slices of the FHIR R4 / US Core resources consumed by the
 * patient dashboard cards. We intentionally do not pull `@types/fhir` — only
 * the fields the cards read are modelled here. Sourced from
 * `dashboard-api-map.md` and the 1.5 verification results.
 */

export interface FhirCoding {
  system?: string;
  code?: string;
  display?: string;
}

export interface FhirCodeableConcept {
  coding?: FhirCoding[];
  text?: string;
}

export interface FhirNarrative {
  status?: "generated" | "extensions" | "additional" | "empty";
  div?: string;
}

export interface FhirReference {
  reference?: string;
  type?: string;
  display?: string;
}

export interface FhirIdentifier {
  system?: string;
  value?: string;
}

export interface FhirHumanName {
  use?: string;
  family?: string;
  given?: string[];
  text?: string;
}

export interface FhirAttachment {
  contentType?: string;
  data?: string;
  url?: string;
  title?: string;
}

export interface FhirQuantity {
  value?: number;
  unit?: string;
  system?: string;
  code?: string;
}

export interface FhirPeriod {
  start?: string;
  end?: string;
}

export interface FhirMeta {
  lastUpdated?: string;
  versionId?: string;
}

// Patient

export interface FhirPatient {
  resourceType: "Patient";
  id: string;
  meta?: FhirMeta;
  identifier?: FhirIdentifier[];
  name?: FhirHumanName[];
  birthDate?: string;
  gender?: "male" | "female" | "other" | "unknown";
  photo?: FhirAttachment[];
}

// AllergyIntolerance

export type FhirAllergyClinicalStatus =
  | "active"
  | "inactive"
  | "resolved";

export type FhirAllergyVerificationStatus =
  | "unconfirmed"
  | "confirmed"
  | "refuted"
  | "entered-in-error";

export type FhirAllergyCriticality = "low" | "high" | "unable-to-assess";

export type FhirAllergyCategory =
  | "food"
  | "medication"
  | "environment"
  | "biologic";

export interface FhirAllergyReaction {
  manifestation?: FhirCodeableConcept[];
  description?: string;
  severity?: "mild" | "moderate" | "severe";
}

export interface FhirAllergyIntolerance {
  resourceType: "AllergyIntolerance";
  id: string;
  meta?: FhirMeta;
  clinicalStatus?: FhirCodeableConcept;
  verificationStatus?: FhirCodeableConcept;
  code?: FhirCodeableConcept;
  text?: FhirNarrative;
  category?: FhirAllergyCategory[];
  criticality?: FhirAllergyCriticality;
  reaction?: FhirAllergyReaction[];
  patient?: FhirReference;
}

// Condition

export type FhirConditionClinicalStatus =
  | "active"
  | "recurrence"
  | "relapse"
  | "inactive"
  | "remission"
  | "resolved";

export type FhirConditionVerificationStatus =
  | "unconfirmed"
  | "provisional"
  | "differential"
  | "confirmed"
  | "refuted"
  | "entered-in-error";

export type FhirConditionCategory =
  | "problem-list-item"
  | "encounter-diagnosis"
  | "health-concern";

export interface FhirCondition {
  resourceType: "Condition";
  id: string;
  meta?: FhirMeta;
  clinicalStatus?: FhirCodeableConcept;
  verificationStatus?: FhirCodeableConcept;
  code?: FhirCodeableConcept;
  category?: FhirCodeableConcept[];
  onsetDateTime?: string;
  recordedDate?: string;
  subject?: FhirReference;
}

// MedicationRequest

export type FhirMedicationRequestStatus =
  | "active"
  | "on-hold"
  | "cancelled"
  | "completed"
  | "entered-in-error"
  | "stopped"
  | "draft"
  | "unknown";

export type FhirMedicationRequestIntent =
  | "proposal"
  | "plan"
  | "order"
  | "original-order"
  | "instance-order"
  | "option"
  | "directive";

export interface FhirDoseAndRate {
  type?: FhirCodeableConcept;
  doseQuantity?: FhirQuantity;
}

export interface FhirDosageInstruction {
  text?: string;
  patientInstruction?: string;
  timing?: { code?: FhirCodeableConcept };
  route?: FhirCodeableConcept;
  doseAndRate?: FhirDoseAndRate[];
}

export interface FhirMedicationRequest {
  resourceType: "MedicationRequest";
  id: string;
  meta?: FhirMeta;
  status: FhirMedicationRequestStatus;
  intent: FhirMedicationRequestIntent;
  medicationCodeableConcept?: FhirCodeableConcept;
  medicationReference?: FhirReference;
  dosageInstruction?: FhirDosageInstruction[];
  requester?: FhirReference;
  authoredOn?: string;
  subject?: FhirReference;
}

// CareTeam

export type FhirCareTeamStatus =
  | "proposed"
  | "active"
  | "suspended"
  | "inactive"
  | "entered-in-error";

export interface FhirCareTeamParticipant {
  role?: FhirCodeableConcept[];
  member?: FhirReference;
  onBehalfOf?: FhirReference;
  period?: FhirPeriod;
}

export interface FhirCareTeam {
  resourceType: "CareTeam";
  id: string;
  meta?: FhirMeta;
  status: FhirCareTeamStatus;
  name?: string;
  participant?: FhirCareTeamParticipant[];
  subject?: FhirReference;
}

// Observation

export type FhirObservationStatus =
  | "registered"
  | "preliminary"
  | "final"
  | "amended"
  | "corrected"
  | "cancelled"
  | "entered-in-error"
  | "unknown";

export type FhirObservationCategory =
  | "vital-signs"
  | "laboratory"
  | "social-history"
  | "imaging"
  | "procedure"
  | "survey"
  | "exam"
  | "therapy"
  | "activity";

export interface FhirObservationComponent {
  code: FhirCodeableConcept;
  valueQuantity?: FhirQuantity;
  valueCodeableConcept?: FhirCodeableConcept;
  valueString?: string;
}

export interface FhirObservation {
  resourceType: "Observation";
  id: string;
  meta?: FhirMeta;
  status: FhirObservationStatus;
  code: FhirCodeableConcept;
  category?: FhirCodeableConcept[];
  valueQuantity?: FhirQuantity;
  valueCodeableConcept?: FhirCodeableConcept;
  valueString?: string;
  effectiveDateTime?: string;
  component?: FhirObservationComponent[];
  subject?: FhirReference;
}

// Bundle

export interface FhirBundleEntry<T> {
  resource: T;
  fullUrl?: string;
}

export interface FhirBundle<T> {
  resourceType: "Bundle";
  type?: string;
  total?: number;
  entry?: FhirBundleEntry<T>[];
}

// OperationOutcome / error

export interface FhirOperationOutcomeIssue {
  severity?: "fatal" | "error" | "warning" | "information";
  code?: string;
  diagnostics?: string;
}

export interface FhirOperationOutcome {
  resourceType: "OperationOutcome";
  issue?: FhirOperationOutcomeIssue[];
}

export interface FhirError {
  resourceType?: "OperationOutcome";
  status: number;
  issue?: FhirOperationOutcomeIssue[];
}

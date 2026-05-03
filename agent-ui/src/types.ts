export interface CopilotConfig {
  agentApiUrl: string;
  providerId: string | number;
  csrfToken?: string;
  sessionId: string;
  patientIds?: string[];
  providerName?: string;
}

/** Legacy triage entry shape from /triage/census */
export interface TriageEntry {
  patient_id: string;
  name: string;
  mrn: string;
  triage_level: number;
  triage_label: string;
  explanation: string;
  matched_criteria: Record<string, unknown>;
  verification_warnings?: string[];
}

/** Single patient in a census response — matches the actual backend payload */
export interface CensusPatient {
  patient_id: string;
  name: string;
  mrn: string;
  openemr_pid?: string;
  triage_level: number;
  triage_label: string;
  explanation: string;
  matched_criteria: Record<string, unknown>;
  verification_warnings?: string[];
  admit_date?: string;
  days_since_admit?: number;
}

export interface ConversationTurn {
  role: 'user' | 'assistant';
  content: string;
}

// ── Dispatcher response envelope ──────────────────────────────────────────────

export interface Citation {
  patient_id: string;
  resource_type: string;
  resource_id: string;
  effective_datetime: string | null;
  value_summary: string;
  claim_class: string;
}

export type ErrorClass = 'transient' | 'persistent' | 'missing_data' | 'unknown';

export interface AgentResponseMetadata {
  /** Coarse, user-facing category for the error. Set on type === 'error'. */
  error_class?: ErrorClass;
  /** Whether the UI should offer a Retry CTA. */
  retry_suggested?: boolean;
  /** If set, suggested wait time before retry (e.g. from a 429). */
  retry_after_ms?: number;
  /** Internal failure class (telemetry / support handle). */
  failure_class?: string;
  /** Patient context established by the most recent successful tool call. */
  patient_id?: string;
  /** Display name for the patient surfaced by the most recent tool call. */
  patient_name?: string;
  /** Numeric OpenEMR PID for chart navigation, when available. */
  openemr_pid?: string;
  [key: string]: unknown;
}

export interface AgentResponse {
  type: 'census' | 'briefing' | 'query_answer' | 'medication_safety' | 'handoff' | 'text' | 'error';
  data: unknown;
  narrative: string;
  citations: Citation[];
  metadata?: AgentResponseMetadata;
}

// ── Per-type data shapes ───────────────────────────────────────────────────────

export interface PatientSummary {
  patient_id: string;
  name: string;
  bed: string;
  priority: string;   // "P1" … "P10"
  one_line: string;
  openemr_pid?: string;
}

/** Census payload as returned by the backend get_census_summary tool */
export interface CensusData {
  census: CensusPatient[];
  total: number;
  requested?: number;
  dropped?: number;
  dropped_ids?: string[];
}

export interface ClinicalClaim {
  text: string;
  source_resource: string;
  source_code: string;
  source_value: string;
  source_dt: string;
}

export interface BriefingResponseSection {
  section: string;
  summary: string;
  claims: ClinicalClaim[];
}

export interface BriefingSection {
  patient_id: string;
  name: string;
  sections: BriefingResponseSection[];
  alerts: string[];
  generated_at: string;
}

export interface QueryAnswerData {
  found: boolean;
  answer: string;
  searched?: string;
  window_months?: number;
}

export interface MedicationSafetyData {
  current_medications: string[];
  allergies: string[];
  interactions: string[];
}

export interface HandoffPatient {
  patient_id: string;
  name: string;
  status: string;
  active_issues: string[];
  pending_items: string[];
  escalation_triggers: string[];
  /**
   * When true, this entry is a placeholder while the per-patient handoff
   * stream is still in flight. Renderer shows a "generating…" card.
   */
  pending?: boolean;
  /**
   * When set, the per-patient handoff failed and the renderer shows an
   * error card with this message in place of the I-PASS sections.
   */
  error?: string;
}

export interface HandoffData {
  patients: HandoffPatient[];
  shift_end_time?: string;
}

export interface TriageRationaleData {
  patient_id: string;
  priority: string;
  rules_fired: string[];
  thresholds_crossed: string[];
  explanation: string;
}

declare global {
  interface Window {
    __COPILOT_CONFIG__: CopilotConfig;
  }
}

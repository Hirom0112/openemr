export interface CopilotConfig {
  agentApiUrl: string;
  providerId: string;
  csrfToken: string;
  sessionId: string;
  patientIds: string[];
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

export interface AgentResponse {
  type: 'census' | 'briefing' | 'query_answer' | 'medication_safety' | 'handoff' | 'text' | 'error';
  data: unknown;
  narrative: string;
  citations: Citation[];
  metadata?: Record<string, unknown>;
}

// ── Per-type data shapes ───────────────────────────────────────────────────────

export interface PatientSummary {
  patient_id: string;
  name: string;
  bed: string;
  priority: string;   // "P1" … "P10"
  one_line: string;
}

export interface CensusData {
  patients: PatientSummary[];
  total: number;
}

export interface BriefingSection {
  active_problems: string[];
  recent_vitals: Record<string, string>;
  medications: string[];
  pending_results: string[];
  clinical_summary: string;
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

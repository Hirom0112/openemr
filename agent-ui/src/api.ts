import type { AgentResponse, TriageRationaleData } from './types';

const cfg = () => window.__COPILOT_CONFIG__;

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${cfg().agentApiUrl}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`API error ${res.status}: ${path}`);
  return res.json();
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${cfg().agentApiUrl}${path}`);
  if (!res.ok) throw new Error(`API error ${res.status}: ${path}`);
  return res.json();
}

export async function fetchHealth() {
  return get<{ status: string; redis: boolean }>('/health');
}

/** Fire-and-forget signal to warm the FHIR cache on panel mount. */
export async function prefetchPatientData(sessionId: string, patientIds: string[]): Promise<void> {
  try {
    await post('/agent/prefetch', {
      session_id: sessionId,
      provider_id: cfg().providerId,
      patient_ids: patientIds,
    });
  } catch {
    // Non-blocking — pre-fetch failure must never block the panel
  }
}

/** Main dispatcher — all conversational turns route through here. */
export async function sendAgentMessage(
  message: string,
  sessionId: string,
): Promise<AgentResponse> {
  // TODO: replace with streaming (Phase 14)
  return post<AgentResponse>('/agent/query', {
    message,
    session_id: sessionId,
    provider_id: cfg().providerId,
    patient_ids: cfg().patientIds ?? [],
    provider_name: cfg().providerName ?? 'Provider',
  });
}

/** Direct-call triage rationale — bypasses dispatcher for <2s budget. */
export async function fetchTriageRationale(patientId: string): Promise<TriageRationaleData> {
  return post<TriageRationaleData>(`/agent/triage_rationale/${patientId}`, {
    patient_id: patientId,
    provider_id: cfg().providerId,
  });
}

// ── Legacy endpoints (kept for backward compatibility) ────────────────────────

export async function fetchCensus(patientIds: string[], sessionId: string) {
  return post<{ census: import('./types').TriageEntry[]; total: number }>(
    '/triage/census',
    { patient_ids: patientIds, session_id: sessionId }
  );
}

export async function sendQuery(sessionId: string, patientId: string, query: string) {
  return post<{ answer: string; route: unknown; turn: number }>(
    `/session/${sessionId}/query`,
    { patient_id: patientId, query }
  );
}

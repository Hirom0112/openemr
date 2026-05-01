import type { AgentResponse, TriageRationaleData } from './types';

const cfg = () => window.__COPILOT_CONFIG__;

async function post<T>(path: string, body: unknown, timeoutMs = 30_000): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${cfg().agentApiUrl}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`API error ${res.status}: ${path}`);
    return res.json();
  } finally {
    clearTimeout(timer);
  }
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${cfg().agentApiUrl}${path}`);
  if (!res.ok) throw new Error(`API error ${res.status}: ${path}`);
  return res.json();
}

export async function fetchHealth() {
  const url = `${cfg().agentApiUrl}/health`;
  console.log('[copilot] fetchHealth →', url);
  const result = await get<{ status: string; redis: boolean }>('/health');
  console.log('[copilot] fetchHealth ←', result);
  return result;
}

/** Fire-and-forget signal to warm the FHIR cache on panel mount. */
export async function prefetchPatientData(sessionId: string, patientIds: string[]): Promise<void> {
  try {
    await post('/agent/prefetch', {
      session_id: sessionId,
      provider_id: String(cfg().providerId),
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
  censusContext?: string,
): Promise<AgentResponse> {
  // 90s: handoffs over a full census fire N parallel LLM calls and can legitimately take >30s
  return post<AgentResponse>('/agent/query', {
    message,
    session_id: sessionId,
    provider_id: String(cfg().providerId),
    patient_ids: cfg().patientIds ?? [],
    provider_name: cfg().providerName ?? 'Provider',
    ...(censusContext ? { census_context: censusContext } : {}),
  }, 90_000);
}

/** Direct-call triage rationale — bypasses dispatcher for <2s budget. */
export async function fetchTriageRationale(patientId: string, sessionId?: string): Promise<TriageRationaleData> {
  return post<TriageRationaleData>(`/agent/triage_rationale/${patientId}`, {
    patient_id: patientId,
    session_id: sessionId ?? cfg().sessionId ?? null,
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

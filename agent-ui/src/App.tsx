import { useEffect, useState, useCallback, useRef } from 'react';
import { fetchHealth, getPending } from './api';
import type { PendingExtractionRow, PostApprovalContext, StagingMetadata } from './api';
import ChatSurface from './components/ChatSurface';
import DocumentsTab from './components/DocumentsTab';
import ApprovalModal from './components/ApprovalModal';
import DocumentReviewPanel from './components/DocumentReviewPanel';
import { BRAND, NEU, SURFACE } from './styles/tokens';
import type { Lane } from './styles/tokens';
import type { CopilotConfig } from './types';

interface AppProps {
  config: CopilotConfig;
}

type TabKey = 'chat' | 'documents';

const PENDING_POLL_MS = 30_000;

export default function App({ config }: AppProps) {
  const sessionId = config.sessionId ?? `session-${Date.now()}`;
  const patientIds: string[] = (config.patientIds as string[] | undefined) ?? [];
  const providerName: string = config.providerName ?? '';

  const [agentOnline, setAgentOnline] = useState<boolean | null>(null);
  const [activeTab, setActiveTab] = useState<TabKey>('chat');

  // Lifted from ChatSurface so DocumentsTab can also trigger the modal.
  // The ApprovalModal mount lives at App-level so it overlays both tabs.
  const [pendingApproval, setPendingApproval] = useState<{
    staging: StagingMetadata;
    lane: Lane | null;
  } | null>(null);

  // Phase-3 — rich review panel target. When non-null the panel mounts
  // over the active tab; HL7/XLSX still use ApprovalModal (above).
  const [reviewTarget, setReviewTarget] = useState<{
    documentReferenceId: string;
    fileBatchId: string;
    rowIds: number[];
  } | null>(null);

  // Phase-3 — post-approval RAG result, handed down to ChatSurface so it
  // can render as a guidelines card on the next assistant turn. The full
  // {documentReferenceId, ragResult} envelope lets ChatSurface dedupe.
  const [postApprovalGuidelines, setPostApprovalGuidelines] = useState<{
    documentReferenceId: string;
    ragResult: PostApprovalContext;
  } | null>(null);

  // Pending-extractions polling. Lifted out of the (now-deleted) sidebar so
  // both the tab badge and DocumentsTab consume the same source of truth.
  const [pendingRows, setPendingRows] = useState<PendingExtractionRow[]>([]);
  const inFlight = useRef(false);

  const ingestPatientId: string | null = patientIds.length > 0 ? patientIds[0] : null;
  const ingestBaseUrl: string =
    (window.__COPILOT_CONFIG__?.agentApiUrl as string | undefined) ?? '';

  useEffect(() => {
    fetchHealth()
      .then((h) => setAgentOnline(h.status === 'ok'))
      .catch(() => setAgentOnline(false));
  }, []);

  const refetchPending = useCallback(async (): Promise<void> => {
    if (!ingestPatientId || !ingestBaseUrl) {
      setPendingRows([]);
      return;
    }
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const resp = await getPending(ingestBaseUrl, {
        patient_id: ingestPatientId,
        state: 'pending',
      });
      setPendingRows(resp.rows);
    } catch {
      // Swallow — count just won't update this tick. Surfacing a banner here
      // would be noise; the DocumentsTab can show a retry affordance later.
    } finally {
      inFlight.current = false;
    }
  }, [ingestBaseUrl, ingestPatientId]);

  // Refetch on patient change.
  useEffect(() => {
    void refetchPending();
  }, [refetchPending]);

  // Poll while patient is selected. Pauses while the page is hidden.
  useEffect(() => {
    if (!ingestPatientId) return;
    const id = window.setInterval(() => {
      if (document.hidden) return;
      void refetchPending();
    }, PENDING_POLL_MS);
    return () => window.clearInterval(id);
  }, [ingestPatientId, refetchPending]);

  // After the modal closes (approve/reject lands), refresh the inbox so the
  // count + DocumentsTab card reflect the new state.
  const handleApprovalClose = useCallback(() => {
    setPendingApproval(null);
    void refetchPending();
  }, [refetchPending]);

  const triggerApproval = useCallback(
    (staging: StagingMetadata, lane: Lane | null) => {
      setPendingApproval({ staging, lane });
    },
    [],
  );

  const triggerRichReview = useCallback(
    (documentReferenceId: string, fileBatchId: string, rowIds: number[]) => {
      setReviewTarget({ documentReferenceId, fileBatchId, rowIds });
    },
    [],
  );

  const handleReviewCompleted = useCallback(
    (documentReferenceId: string, ragResult: PostApprovalContext | null) => {
      // Non-empty RAG → push to ChatSurface so the next assistant turn
      // shows the post-approval guidelines card. Switching tabs first so
      // the message is on screen before the iframe re-paints.
      if (ragResult) {
        setPostApprovalGuidelines({ documentReferenceId, ragResult });
      }
      setReviewTarget(null);
      setActiveTab('chat');
      void refetchPending();
    },
    [refetchPending],
  );

  const pendingCount = pendingRows.length;

  return (
    <div style={styles.root}>
      <div style={styles.header}>
        <span style={styles.title}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ marginRight: 8, verticalAlign: 'middle' }} aria-hidden="true">
            <path d="M6 3v6a4 4 0 0 0 8 0V3" />
            <path d="M10 13v3a5 5 0 0 0 10 0v-2" />
            <circle cx="20" cy="11" r="2" />
          </svg>
          Clinical Co-Pilot
        </span>
        <span
          style={{
            ...styles.badge,
            background:
              agentOnline === true ? '#27ae60'
              : agentOnline === false ? '#c0392b'
              : '#888',
          }}
        >
          {agentOnline === true ? 'Online' : agentOnline === false ? 'Offline' : 'Connecting…'}
        </span>
      </div>

      {/* Tab strip */}
      <div role="tablist" aria-label="Co-Pilot views" style={styles.tabStrip}>
        <TabButton
          label="Chat"
          active={activeTab === 'chat'}
          onClick={() => setActiveTab('chat')}
        />
        <TabButton
          label={pendingCount > 0 ? `Documents (${pendingCount})` : 'Documents'}
          active={activeTab === 'documents'}
          onClick={() => setActiveTab('documents')}
          emphasize={pendingCount > 0}
        />
      </div>

      <div style={styles.body}>
        {agentOnline === false ? (
          <div style={styles.offline}>
            <strong>Agent unavailable</strong> — check that the Co-Pilot service is running.
          </div>
        ) : agentOnline === null ? (
          <div style={styles.spinner}>Connecting…</div>
        ) : (
          <>
            <div
              role="tabpanel"
              hidden={activeTab !== 'chat'}
              style={{
                display: activeTab === 'chat' ? 'flex' : 'none',
                flex: 1,
                minHeight: 0,
                flexDirection: 'column',
              }}
            >
              <ChatSurface
                sessionId={sessionId}
                patientIds={patientIds}
                providerName={providerName}
                pendingCount={pendingCount}
                onSwitchToDocumentsTab={() => setActiveTab('documents')}
                onTriggerApproval={triggerApproval}
                postApprovalGuidelines={postApprovalGuidelines}
              />
            </div>
            <div
              role="tabpanel"
              hidden={activeTab !== 'documents'}
              style={{
                display: activeTab === 'documents' ? 'flex' : 'none',
                flex: 1,
                minHeight: 0,
                flexDirection: 'column',
              }}
            >
              <DocumentsTab
                rows={pendingRows}
                patientId={ingestPatientId}
                onTriggerApproval={triggerApproval}
                onTriggerRichReview={triggerRichReview}
              />
            </div>
          </>
        )}
      </div>

      {/* App-level ApprovalModal — overlays both tabs so DocumentsTab card
          clicks and post-upload triggers from ChatSurface route to the same
          surface. */}
      {pendingApproval && (
        <ApprovalModal
          baseUrl={ingestBaseUrl}
          staging={pendingApproval.staging}
          lane={pendingApproval.lane}
          onClose={handleApprovalClose}
        />
      )}

      {/* Phase-3 rich review panel. Same overlay tier as ApprovalModal —
          only one can be open at a time because they're triggered from
          mutually-exclusive document-format branches. */}
      {reviewTarget && ingestPatientId && (
        <DocumentReviewPanel
          baseUrl={ingestBaseUrl}
          patientId={ingestPatientId}
          documentReferenceId={reviewTarget.documentReferenceId}
          fileBatchId={reviewTarget.fileBatchId}
          pendingRowIds={reviewTarget.rowIds}
          onClose={() => setReviewTarget(null)}
          onCompleted={handleReviewCompleted}
        />
      )}
    </div>
  );
}

interface TabButtonProps {
  label: string;
  active: boolean;
  onClick: () => void;
  emphasize?: boolean;
}

function TabButton({ label, active, onClick, emphasize }: TabButtonProps) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      style={{
        background: 'transparent',
        border: 'none',
        borderBottom: active ? `2px solid ${BRAND.base}` : '2px solid transparent',
        padding: '8px 16px',
        fontSize: 13,
        fontFamily: 'inherit',
        fontWeight: active ? 600 : 500,
        color: active ? BRAND.base : (emphasize ? BRAND.base : SURFACE.fg),
        cursor: 'pointer',
        transition: 'border-color 0.15s, color 0.15s',
      }}
    >
      {label}
    </button>
  );
}

const styles: Record<string, React.CSSProperties> = {
  root: {
    display: 'flex',
    flexDirection: 'column',
    height: '100vh',
    fontFamily: 'system-ui, -apple-system, sans-serif',
    fontSize: 13,
    color: '#222',
    background: '#f8f9fa',
    boxSizing: 'border-box',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '10px 16px',
    background: '#2c3e9e',
    color: '#fff',
    flex: '0 0 auto',
  },
  title: {
    fontWeight: 700,
    fontSize: 15,
    display: 'flex',
    alignItems: 'center',
  },
  badge: {
    color: '#fff',
    borderRadius: 10,
    padding: '2px 10px',
    fontSize: 11,
    border: '1px solid rgba(255,255,255,0.4)',
  },
  tabStrip: {
    display: 'flex',
    gap: 4,
    padding: '0 12px',
    background: SURFACE.bg,
    borderBottom: `1px solid ${NEU.border}`,
    flex: '0 0 auto',
  },
  body: {
    flex: 1,
    minHeight: 0,
    display: 'flex',
    flexDirection: 'column',
    overflow: 'hidden',
  },
  offline: { padding: 24, color: '#c0392b' },
  spinner: { padding: 24, color: '#888' },
};

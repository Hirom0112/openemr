import { useEffect, useState } from 'react';
import { fetchHealth } from './api';
import ChatSurface from './components/ChatSurface';

const SESSION_ID =
  window.__COPILOT_CONFIG__?.sessionId ?? `session-${Date.now()}`;

const PATIENT_IDS: string[] =
  window.__COPILOT_CONFIG__?.patientIds ?? [];

export default function App() {
  const [agentOnline, setAgentOnline] = useState<boolean | null>(null);

  useEffect(() => {
    fetchHealth()
      .then((h) => setAgentOnline(h.status === 'ok'))
      .catch(() => setAgentOnline(false));
  }, []);

  if (agentOnline === false) {
    return (
      <div style={styles.offline}>
        <strong>Agent unavailable</strong> — view chart directly.
      </div>
    );
  }

  if (agentOnline === null) {
    return <div style={styles.spinner}>Connecting…</div>;
  }

  return (
    <div style={styles.root}>
      <div style={styles.header}>
        <span style={styles.title}>Clinical Co-Pilot</span>
        <span style={styles.badge}>Online</span>
      </div>
      <div style={styles.body}>
        <ChatSurface sessionId={SESSION_ID} patientIds={PATIENT_IDS} />
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  root: {
    fontFamily: 'system-ui, sans-serif',
    fontSize: 13,
    color: '#222',
    maxWidth: 440,
    margin: '0 auto',
    padding: 8,
    display: 'flex',
    flexDirection: 'column',
    height: '100vh',
    boxSizing: 'border-box',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: 10,
    paddingBottom: 8,
    borderBottom: '1px solid #e8e8e8',
  },
  title: { fontWeight: 700, fontSize: 15 },
  badge: {
    background: '#27ae60',
    color: '#fff',
    borderRadius: 10,
    padding: '2px 8px',
    fontSize: 11,
  },
  body: { flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' },
  offline: { padding: 20, color: '#c0392b', fontFamily: 'system-ui, sans-serif' },
  spinner: { padding: 20, color: '#888', fontFamily: 'system-ui, sans-serif' },
};

import { useEffect, useState } from 'react';
import { fetchHealth } from './api';
import ChatSurface from './components/ChatSurface';
import type { CopilotConfig } from './types';

interface AppProps {
  config: CopilotConfig;
}

export default function App({ config }: AppProps) {
  const sessionId = config.sessionId ?? `session-${Date.now()}`;
  const patientIds: string[] = (config.patientIds as string[] | undefined) ?? [];
  const providerName: string = config.providerName ?? '';

  const [agentOnline, setAgentOnline] = useState<boolean | null>(null);

  useEffect(() => {
    fetchHealth()
      .then((h) => setAgentOnline(h.status === 'ok'))
      .catch(() => setAgentOnline(false));
  }, []);

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

      <div style={styles.body}>
        {agentOnline === false ? (
          <div style={styles.offline}>
            <strong>Agent unavailable</strong> — check that the Co-Pilot service is running.
          </div>
        ) : agentOnline === null ? (
          <div style={styles.spinner}>Connecting…</div>
        ) : (
          <ChatSurface
            sessionId={sessionId}
            patientIds={patientIds}
            providerName={providerName}
          />
        )}
      </div>
    </div>
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

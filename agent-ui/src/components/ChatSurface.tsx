import { useState, useEffect, useCallback, useRef } from 'react';
import { sendAgentMessage, prefetchPatientData } from '../api';
import type { AgentResponse } from '../types';
import ResponseRenderer from './ResponseRenderer';

const CENSUS_INIT_MESSAGE = '__census_summary__';

function timeGreeting(): string {
  const h = new Date().getHours();
  if (h < 12) return 'Good morning';
  if (h < 17) return 'Good afternoon';
  return 'Good evening';
}

function ThinkingDots() {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3, height: 16 }}>
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          style={{
            width: 6,
            height: 6,
            borderRadius: '50%',
            background: '#9ca3af',
            display: 'inline-block',
            animation: `copilot-pulse 1.2s ease-in-out ${i * 0.2}s infinite`,
          }}
        />
      ))}
    </span>
  );
}

interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content?: string;
  response?: AgentResponse;
}

interface ChatSurfaceProps {
  sessionId: string;
  patientIds: string[];
  providerName?: string;
}

export default function ChatSurface({ sessionId, patientIds, providerName }: ChatSurfaceProps) {
  const displayName = (providerName && providerName.trim()) || 'Doctor';
  const greeting = `${timeGreeting()}, ${displayName}. Ready for morning rounds? I'll pull up your census now.`;

  const [messages, setMessages] = useState<Message[]>([
    { id: 'greeting', role: 'system', content: greeting },
  ]);
  const [inputText, setInputText] = useState('');
  const [loading, setLoading] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const censusDispatched = useRef(false);
  const censusContext = useRef<string | undefined>(undefined);

  useEffect(() => {
    if (censusDispatched.current) return;
    censusDispatched.current = true;
    void prefetchPatientData(sessionId, patientIds);
    void dispatchMessage(CENSUS_INIT_MESSAGE, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const dispatchMessage = useCallback(async (text: string, isAutoDispatch = false) => {
    if (!isAutoDispatch) {
      setMessages((prev) => [
        ...prev,
        { id: `user-${Date.now()}`, role: 'user', content: text },
      ]);
    }

    setLoading(true);
    try {
      const response = await sendAgentMessage(text, sessionId, censusContext.current);

      // After the census loads, cache a name→ID map so the LLM doesn't need
      // to re-fetch it on every subsequent query.
      if (response.type === 'census' && response.data) {
        const data = response.data as { census?: Array<{ patient_id: string; name: string }> };
        if (data.census?.length) {
          const lines = data.census.map((p) => `  - ${p.name}: ${p.patient_id}`).join('\n');
          censusContext.current = `## Census patient name → ID mapping\n${lines}`;
        }
      }

      setMessages((prev) => [
        ...prev,
        { id: `assistant-${Date.now()}`, role: 'assistant', response },
      ]);
    } catch {
      setMessages((prev) => [
        ...prev,
        {
          id: `error-${Date.now()}`,
          role: 'assistant',
          response: {
            type: 'error',
            data: null,
            narrative: 'Agent unavailable — view chart directly.',
            citations: [],
          },
        },
      ]);
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  const handleSend = useCallback(() => {
    const text = inputText.trim();
    if (!text || loading) return;
    setInputText('');
    void dispatchMessage(text);
  }, [inputText, loading, dispatchMessage]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <>
      {/* Keyframe animation injected once */}
      <style>{`
        @keyframes copilot-pulse {
          0%, 80%, 100% { opacity: 0.2; transform: scale(0.8); }
          40% { opacity: 1; transform: scale(1); }
        }
      `}</style>

      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%', minHeight: 0 }}>
        {/* Message list */}
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '12px 14px' }}>
          {messages.map((msg) => {
            if (msg.role === 'system') {
              return (
                <div key={msg.id} style={styles.systemMsg}>
                  <span style={styles.systemIcon}>✦</span>
                  {msg.content}
                </div>
              );
            }

            if (msg.role === 'user') {
              return (
                <div key={msg.id} style={styles.userRow}>
                  <div style={styles.userBubble}>{msg.content}</div>
                </div>
              );
            }

            // assistant
            return (
              <div key={msg.id} style={styles.assistantRow}>
                <div style={styles.assistantAvatar} aria-hidden="true">AI</div>
                <div style={styles.assistantBubble}>
                  {msg.response ? (
                    <ResponseRenderer
                      response={msg.response}
                      onBrief={(name, patientId) => void dispatchMessage(
                        patientId ? `Brief ${name} (patient_id: ${patientId})` : `Brief ${name}`
                      )}
                      providerName={displayName}
                    />
                  ) : (
                    <span style={{ color: '#9ca3af' }}>…</span>
                  )}
                </div>
              </div>
            );
          })}

          {loading && (
            <div style={styles.assistantRow}>
              <div style={styles.assistantAvatar} aria-hidden="true">AI</div>
              <div style={{ ...styles.assistantBubble, padding: '10px 14px' }}>
                <ThinkingDots />
              </div>
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        {/* Input row */}
        <div style={styles.inputRow}>
          <input
            style={styles.input}
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={`Ask about a patient — try "Brief Marcus Webb" or "why is ${displayName.split(' ')[0]} P1?"`}
            disabled={loading}
          />
          <button
            style={{
              ...styles.sendBtn,
              opacity: loading || !inputText.trim() ? 0.5 : 1,
              cursor: loading || !inputText.trim() ? 'not-allowed' : 'pointer',
            }}
            onClick={handleSend}
            disabled={loading || !inputText.trim()}
          >
            Send
          </button>
        </div>
      </div>
    </>
  );
}

const styles: Record<string, React.CSSProperties> = {
  systemMsg: {
    display: 'flex',
    alignItems: 'flex-start',
    gap: 8,
    fontSize: 13,
    color: '#1e3a8a',
    fontWeight: 500,
    padding: '8px 12px',
    marginBottom: 12,
    borderLeft: '3px solid #3b5bdb',
    background: 'linear-gradient(90deg, #eef2ff 0%, #f8f9fa 100%)',
    borderRadius: '0 6px 6px 0',
    lineHeight: 1.5,
  },
  systemIcon: {
    color: '#3b5bdb',
    fontSize: 14,
    flexShrink: 0,
    marginTop: 1,
  },
  userRow: {
    display: 'flex',
    justifyContent: 'flex-end',
    marginBottom: 10,
  },
  userBubble: {
    background: 'linear-gradient(135deg, #3b5bdb 0%, #2c3e9e 100%)',
    color: '#fff',
    borderRadius: '14px 14px 4px 14px',
    padding: '9px 14px',
    maxWidth: '78%',
    fontSize: 13,
    lineHeight: 1.5,
    boxShadow: '0 1px 3px rgba(44,62,158,0.25)',
  },
  assistantRow: {
    display: 'flex',
    alignItems: 'flex-start',
    gap: 8,
    marginBottom: 10,
    marginRight: '4%',
  },
  assistantAvatar: {
    flexShrink: 0,
    width: 26,
    height: 26,
    borderRadius: '50%',
    background: 'linear-gradient(135deg, #3b5bdb 0%, #6d28d9 100%)',
    color: '#fff',
    fontSize: 9,
    fontWeight: 800,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    letterSpacing: 0.5,
    marginTop: 2,
    boxShadow: '0 1px 3px rgba(0,0,0,0.18)',
  },
  assistantBubble: {
    background: '#fff',
    border: '1px solid #e5e7eb',
    borderRadius: '4px 14px 14px 14px',
    padding: '10px 14px',
    fontSize: 13,
    lineHeight: 1.55,
    boxShadow: '0 1px 4px rgba(0,0,0,0.06)',
    flex: 1,
    minWidth: 0,
  },
  inputRow: {
    display: 'flex',
    gap: 8,
    padding: '10px 14px',
    borderTop: '1px solid #e5e7eb',
    flex: '0 0 auto',
    background: '#fff',
  },
  input: {
    flex: 1,
    padding: '9px 12px',
    border: '1px solid #d1d5db',
    borderRadius: 8,
    fontFamily: 'inherit',
    fontSize: 13,
    outline: 'none',
    background: '#f9fafb',
    color: '#111',
    transition: 'border-color 0.15s',
  },
  sendBtn: {
    padding: '9px 18px',
    background: '#2c3e9e',
    color: '#fff',
    border: 'none',
    borderRadius: 8,
    fontSize: 13,
    fontWeight: 600,
    letterSpacing: 0.2,
    transition: 'opacity 0.15s',
  },
};

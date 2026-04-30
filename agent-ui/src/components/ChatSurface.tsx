import { useState, useEffect, useCallback, useRef } from 'react';
import { sendAgentMessage, prefetchPatientData } from '../api';
import type { AgentResponse } from '../types';
import ResponseRenderer from './ResponseRenderer';

const CENSUS_INIT_MESSAGE = '__census_init__';

function timeGreeting(): string {
  const h = new Date().getHours();
  if (h < 12) return 'Good morning';
  if (h < 17) return 'Good afternoon';
  return 'Good evening';
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
}

export default function ChatSurface({ sessionId, patientIds }: ChatSurfaceProps) {
  const greeting = `${timeGreeting()}, Dr. Chen. Ready for morning rounds? I'll pull up your census now.`;

  const [messages, setMessages] = useState<Message[]>([
    { id: 'greeting', role: 'system', content: greeting },
  ]);
  const [inputText, setInputText] = useState('');
  const [loading, setLoading] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const censusDispatched = useRef(false);

  // On mount: fire pre-fetch in parallel with greeting render, then auto-dispatch census
  useEffect(() => {
    if (censusDispatched.current) return;
    censusDispatched.current = true;

    // Fire pre-fetch — non-blocking, does not delay census dispatch
    void prefetchPatientData(sessionId, patientIds);

    // Auto-dispatch synthetic census message immediately after pre-fetch signal
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
    // TODO: replace with streaming (Phase 14)
    try {
      const response = await sendAgentMessage(text, sessionId);
      setMessages((prev) => [
        ...prev,
        { id: `assistant-${Date.now()}`, role: 'assistant', response },
      ]);
    } catch (err) {
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
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Message list */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '8px 0' }}>
        {messages.map((msg) => {
          if (msg.role === 'system') {
            return (
              <div key={msg.id} style={styles.systemMsg}>{msg.content}</div>
            );
          }
          if (msg.role === 'user') {
            return (
              <div key={msg.id} style={styles.userBubble}>{msg.content}</div>
            );
          }
          // assistant
          return (
            <div key={msg.id} style={styles.assistantBubble}>
              {msg.response ? (
                <ResponseRenderer response={msg.response} />
              ) : (
                <span style={{ color: '#aaa' }}>…</span>
              )}
            </div>
          );
        })}

        {loading && (
          <div style={styles.assistantBubble}>
            <span style={{ color: '#aaa', fontStyle: 'italic' }}>Thinking…</span>
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
          placeholder="Ask about a patient…"
          disabled={loading}
        />
        <button style={styles.sendBtn} onClick={handleSend} disabled={loading || !inputText.trim()}>
          Send
        </button>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  systemMsg: {
    fontSize: 13,
    color: '#2c3e9e',
    fontWeight: 500,
    padding: '6px 10px',
    marginBottom: 8,
    borderLeft: '3px solid #2c3e9e',
    background: '#f0f4ff',
    borderRadius: '0 4px 4px 0',
  },
  userBubble: {
    background: '#2c3e9e',
    color: '#fff',
    borderRadius: '12px 12px 4px 12px',
    padding: '7px 12px',
    marginBottom: 8,
    marginLeft: '20%',
    fontSize: 13,
    lineHeight: 1.4,
  },
  assistantBubble: {
    background: '#fff',
    border: '1px solid #e0e0e0',
    borderRadius: '4px 12px 12px 12px',
    padding: '8px 12px',
    marginBottom: 8,
    marginRight: '5%',
    fontSize: 13,
  },
  inputRow: {
    display: 'flex',
    gap: 6,
    paddingTop: 8,
    borderTop: '1px solid #e8e8e8',
  },
  input: {
    flex: 1,
    padding: '7px 10px',
    border: '1px solid #ccc',
    borderRadius: 4,
    fontFamily: 'inherit',
    fontSize: 13,
  },
  sendBtn: {
    padding: '7px 16px',
    background: '#2c3e9e',
    color: '#fff',
    border: 'none',
    borderRadius: 4,
    cursor: 'pointer',
    fontSize: 13,
  },
};

import { useState } from 'react';
import type { CensusData, Citation, TriageRationaleData } from '../types';
import { fetchTriageRationale } from '../api';
import PatientCard from './PatientCard';

interface CensusRendererProps {
  data: CensusData;
  narrative: string;
  citations: Citation[];
  onPatientSelect?: (patientId: string) => void;
}

export default function CensusRenderer({ data, narrative, onPatientSelect }: CensusRendererProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [rationaleMap, setRationaleMap] = useState<Record<string, TriageRationaleData | 'loading' | 'error'>>({});

  const handleExpand = async (patientId: string) => {
    if (rationaleMap[patientId]) {
      // Toggle off if already shown
      setRationaleMap((prev) => {
        const next = { ...prev };
        delete next[patientId];
        return next;
      });
      return;
    }
    setRationaleMap((prev) => ({ ...prev, [patientId]: 'loading' }));
    try {
      const rationale = await fetchTriageRationale(patientId);
      setRationaleMap((prev) => ({ ...prev, [patientId]: rationale }));
    } catch {
      setRationaleMap((prev) => ({ ...prev, [patientId]: 'error' }));
    }
  };

  const handleSelect = (patientId: string) => {
    setSelectedId(patientId);
    onPatientSelect?.(patientId);
  };

  const patients = data?.patients ?? [];

  return (
    <div>
      {narrative && (
        <p style={{ fontSize: 12, color: '#666', margin: '0 0 8px' }}>{narrative}</p>
      )}
      {patients.map((pt, idx) => (
        <div key={pt.patient_id}>
          <PatientCard
            patient={pt}
            rank={idx + 1}
            selected={selectedId === pt.patient_id}
            onExpand={handleExpand}
            onSelect={handleSelect}
          />
          {rationaleMap[pt.patient_id] && rationaleMap[pt.patient_id] !== 'loading' && rationaleMap[pt.patient_id] !== 'error' && (
            <div style={{
              marginTop: -4,
              marginBottom: 6,
              marginLeft: 34,
              padding: '6px 10px',
              background: '#f8f9fa',
              border: '1px solid #e0e0e0',
              borderTop: 'none',
              borderRadius: '0 0 4px 4px',
              fontSize: 12,
              color: '#444',
            }}>
              {(() => {
                const r = rationaleMap[pt.patient_id] as TriageRationaleData;
                return (
                  <>
                    <strong>{r.priority}</strong> — {r.explanation}
                    {r.rules_fired?.length > 0 && (
                      <div style={{ marginTop: 3, color: '#666' }}>
                        Rules: {r.rules_fired.join(', ')}
                      </div>
                    )}
                  </>
                );
              })()}
            </div>
          )}
          {rationaleMap[pt.patient_id] === 'loading' && (
            <div style={{ marginLeft: 34, fontSize: 11, color: '#aaa', marginBottom: 6 }}>Loading rationale…</div>
          )}
          {rationaleMap[pt.patient_id] === 'error' && (
            <div style={{ marginLeft: 34, fontSize: 11, color: '#e74c3c', marginBottom: 6 }}>Could not load rationale.</div>
          )}
        </div>
      ))}
    </div>
  );
}

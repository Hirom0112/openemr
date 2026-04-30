import { describe, test, expect } from 'vitest';
import { buildCitationUrl } from './citations';
import type { Citation } from '../types';

const baseCitation: Citation = {
  patient_id: 'pt-001',
  resource_type: 'Observation',
  resource_id: 'obs-123',
  effective_datetime: '2026-04-30T08:00:00Z',
  value_summary: 'K+ 5.9 mEq/L',
  claim_class: 'lab_value',
};

describe('buildCitationUrl', () => {
  test('Observation returns results_report URL with patient_id', () => {
    const url = buildCitationUrl(baseCitation);
    expect(url).not.toBeNull();
    expect(url).toContain('pt-001');
    expect(url).toContain('results_report');
  });

  test('DiagnosticReport returns results_report URL', () => {
    const url = buildCitationUrl({ ...baseCitation, resource_type: 'DiagnosticReport' });
    expect(url).toContain('results_report');
  });

  test('MedicationRequest returns medications section URL', () => {
    const url = buildCitationUrl({ ...baseCitation, resource_type: 'MedicationRequest' });
    expect(url).not.toBeNull();
    expect(url).toContain('medications');
    expect(url).toContain('pt-001');
  });

  test('AllergyIntolerance returns allergies section URL', () => {
    const url = buildCitationUrl({ ...baseCitation, resource_type: 'AllergyIntolerance' });
    expect(url).not.toBeNull();
    expect(url).toContain('allergies');
  });

  test('Condition returns problems section URL', () => {
    const url = buildCitationUrl({ ...baseCitation, resource_type: 'Condition' });
    expect(url).not.toBeNull();
    expect(url).toContain('problems');
  });

  test('Encounter returns encounter_top URL', () => {
    const url = buildCitationUrl({ ...baseCitation, resource_type: 'Encounter' });
    expect(url).not.toBeNull();
    expect(url).toContain('encounter_top');
  });

  test('Flag (isolation) returns patient summary URL', () => {
    const url = buildCitationUrl({ ...baseCitation, resource_type: 'Flag', claim_class: 'isolation' });
    expect(url).not.toBeNull();
    expect(url).toContain('pt-001');
  });

  test('Unknown resource type returns null', () => {
    const url = buildCitationUrl({ ...baseCitation, resource_type: 'UnknownResource' });
    expect(url).toBeNull();
  });

  test('All URLs contain the patient_id', () => {
    const types = ['Observation', 'MedicationRequest', 'Condition', 'AllergyIntolerance', 'Encounter', 'DiagnosticReport', 'Flag'];
    for (const resource_type of types) {
      const url = buildCitationUrl({ ...baseCitation, resource_type });
      expect(url).toContain('pt-001');
    }
  });
});

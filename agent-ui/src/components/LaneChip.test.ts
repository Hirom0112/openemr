import { describe, test, expect } from 'vitest';
import { laneFromFilename } from './LaneChip';

describe('LaneChip.laneFromFilename (Slice 9.8)', () => {
  test('classifies PDF/PNG/TIFF/DOCX as DOCUMENT', () => {
    expect(laneFromFilename('a.pdf')).toBe('DOCUMENT');
    expect(laneFromFilename('a.PNG')).toBe('DOCUMENT');
    expect(laneFromFilename('a.tiff')).toBe('DOCUMENT');
    expect(laneFromFilename('a.tif')).toBe('DOCUMENT');
    expect(laneFromFilename('a.docx')).toBe('DOCUMENT');
  });

  test('classifies HL7 as HL7', () => {
    expect(laneFromFilename('msg.hl7')).toBe('HL7');
  });

  test('classifies XLSX as WORKBOOK', () => {
    expect(laneFromFilename('panel.xlsx')).toBe('WORKBOOK');
  });

  test('returns null for unknown extensions', () => {
    expect(laneFromFilename('something.txt')).toBe(null);
    expect(laneFromFilename('noextension')).toBe(null);
  });
});

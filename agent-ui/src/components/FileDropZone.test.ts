import { describe, test, expect } from 'vitest';
import { validateDroppedFiles } from './FileDropZone';

/**
 * Pure-logic regression spec for the FileDropZone validator. The component's
 * fetch / DOM behaviour is integration-level; this file pins the rules that
 * gate uploads so a future refactor can't silently let through .docx or
 * multi-file drops.
 */

function makeFile(name: string, type: string, sizeBytes = 1): File {
  // File size is determined by the blob parts; pad to the requested size for
  // the MAX_FILE_SIZE_BYTES path in Slice 9.8.
  const body = sizeBytes <= 1 ? 'x' : 'x'.repeat(sizeBytes);
  return new File([body], name, { type });
}

describe('FileDropZone.validateDroppedFiles', () => {
  test('accepts a single PDF (by mime type)', () => {
    const result = validateDroppedFiles([makeFile('lab.pdf', 'application/pdf')]);
    expect(result.ok).toBe(true);
    expect(result.file?.name).toBe('lab.pdf');
  });

  test('accepts a single PNG (by mime type)', () => {
    const result = validateDroppedFiles([makeFile('intake.png', 'image/png')]);
    expect(result.ok).toBe(true);
    expect(result.file?.name).toBe('intake.png');
  });

  test('falls back to extension when mime is empty', () => {
    // Some OS file pickers omit the mime; the extension is our backstop.
    const result = validateDroppedFiles([makeFile('scan.pdf', '')]);
    expect(result.ok).toBe(true);
  });

  // Slice 9.8: docx/xlsx/tiff/hl7 are now in the whitelist.
  test('accepts a Word document (.docx)', () => {
    const result = validateDroppedFiles([
      makeFile('note.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    ]);
    expect(result.ok).toBe(true);
  });

  test('accepts an XLSX workbook', () => {
    const result = validateDroppedFiles([
      makeFile('book.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    ]);
    expect(result.ok).toBe(true);
  });

  test('accepts a TIFF (by extension when MIME is image/tiff)', () => {
    const result = validateDroppedFiles([makeFile('fax.tiff', 'image/tiff')]);
    expect(result.ok).toBe(true);
  });

  test('accepts an HL7 file with text/plain MIME (fallback)', () => {
    const result = validateDroppedFiles([makeFile('lab.hl7', 'text/plain')]);
    expect(result.ok).toBe(true);
  });

  test('accepts an HL7 file with application/octet-stream MIME (fallback)', () => {
    const result = validateDroppedFiles([makeFile('lab.hl7', 'application/octet-stream')]);
    expect(result.ok).toBe(true);
  });

  test('rejects an unrelated text file', () => {
    const result = validateDroppedFiles([makeFile('notes.txt', 'text/plain')]);
    expect(result.ok).toBe(false);
    expect(result.error).toMatch(/Unsupported/);
  });

  test('rejects files larger than 25 MB', () => {
    // 26 MB string blob — over the cap.
    const big = 26 * 1024 * 1024;
    const result = validateDroppedFiles([makeFile('huge.pdf', 'application/pdf', big)]);
    expect(result.ok).toBe(false);
    expect(result.error).toMatch(/too large/i);
  });

  test('rejects multiple files', () => {
    const result = validateDroppedFiles([
      makeFile('a.pdf', 'application/pdf'),
      makeFile('b.pdf', 'application/pdf'),
    ]);
    expect(result.ok).toBe(false);
    expect(result.error).toMatch(/one file/i);
  });

  test('rejects empty drop', () => {
    expect(validateDroppedFiles([]).ok).toBe(false);
    expect(validateDroppedFiles(null).ok).toBe(false);
  });
});

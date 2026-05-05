import { describe, test, expect } from 'vitest';
import { validateDroppedFiles } from './FileDropZone';

/**
 * Pure-logic regression spec for the FileDropZone validator. The component's
 * fetch / DOM behaviour is integration-level; this file pins the rules that
 * gate uploads so a future refactor can't silently let through .docx or
 * multi-file drops.
 */

function makeFile(name: string, type: string): File {
  return new File(['x'], name, { type });
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

  test('rejects a Word document', () => {
    const result = validateDroppedFiles([
      makeFile('note.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    ]);
    expect(result.ok).toBe(false);
    expect(result.error).toMatch(/Unsupported/);
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

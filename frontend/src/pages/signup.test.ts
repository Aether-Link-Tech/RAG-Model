import { describe, expect, it } from 'vitest';
import { validate } from './signup';
import { precheck } from '../components/UploadPanel';

const form = (password: string, confirm = password) => ({ fullName: 'Alex', email: 'alex@example.com', password, confirm });

describe('signup validation (FR-001 rules)', () => {
  it('accepts a password meeting all four rules', () => {
    expect(validate(form('Str0ng!pass'), true)).toBe('');
  });

  it.each([
    ['short1!A', ''],
    ['alllowercase1!', 'upper & lower case'],
    ['NoDigits!!', 'a number'],
    ['NoSpecial123', 'a special character'],
  ])('rejects %s', (password, missing) => {
    const message = validate(form(password), true);
    if (missing) expect(message).toContain(missing);
  });

  it('requires matching confirmation and the robot check', () => {
    expect(validate(form('Str0ng!pass', 'other'), true)).toMatch(/do not match/);
    expect(validate(form('Str0ng!pass'), false)).toMatch(/robot/);
  });
});

describe('upload precheck', () => {
  const file = (name: string, size: number) => new File([new Uint8Array(size)], name);

  it('allows pdf/docx/txt', () => {
    expect(precheck(file('a.pdf', 10))).toBeNull();
    expect(precheck(file('a.DOCX', 10))).toBeNull();
  });

  it('rejects other types, empty and oversized files', () => {
    expect(precheck(file('run.exe', 10))?.kind).toBe('type');
    expect(precheck(file('a.txt', 0))?.kind).toBe('empty');
    expect(precheck(file('big.pdf', 26 * 1024 * 1024))?.kind).toBe('size');
  });
});

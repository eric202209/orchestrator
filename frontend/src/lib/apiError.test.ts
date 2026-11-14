import { describe, expect, it } from 'vitest';
import { apiErrorMessage } from './apiError';

describe('apiErrorMessage', () => {
  it.each([
    ['Readable detail', 'Readable detail'],
    [{ message: 'Execution is disabled in this development mode.', code: 'DEVELOPMENT_EXECUTION_DISABLED' }, 'Execution is disabled in this development mode.'],
    [{ code: 'TASK_EXECUTION_ACTIVE' }, 'TASK_EXECUTION_ACTIVE'],
    [{ message: {}, code: 'FALLBACK_CODE' }, 'FALLBACK_CODE'],
    [null, 'Fallback'],
    [[], 'Fallback'],
    [{ stack: 'secret stack trace' }, 'Fallback'],
    [{ message: '  ', code: 123 }, 'Fallback'],
  ])('formats detail %j', (detail, expected) => {
    expect(apiErrorMessage({ response: { data: { detail } } }, 'Fallback')).toBe(expected);
  });
  it('handles network errors', () => {
    expect(apiErrorMessage({ code: 'ERR_NETWORK' })).toMatch(/Network error/);
    expect(apiErrorMessage({ code: 'ECONNABORTED' })).toMatch(/timed out/);
  });
  it.each([undefined, null, 'oops', new Error('secret'), { response: { data: '<html>secret</html>' } }])('handles malformed errors', (error) => {
    expect(apiErrorMessage(error, 'Readable fallback')).toBe('Readable fallback');
  });
});

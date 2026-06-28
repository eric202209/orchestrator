import { describe, expect, it } from 'vitest';

import { isNoisySessionLogMessage, selectRenderableSessionLogs } from './sessionLogNoise';

describe('isNoisySessionLogMessage', () => {
  it('filters telemetry fragments in clean mode heuristics', () => {
    expect(isNoisySessionLogMessage('"replayInvalid": false,')).toBe(true);
    expect(isNoisySessionLogMessage('"livenessState": "working",')).toBe(true);
    expect(isNoisySessionLogMessage('"stopReason": "stop"')).toBe(true);
    expect(isNoisySessionLogMessage('"schemaChars": 1234,')).toBe(true);
  });

  it('keeps meaningful structured diagnostics visible', () => {
    expect(isNoisySessionLogMessage('"error": "model backend unavailable"')).toBe(
      false
    );
    expect(
      isNoisySessionLogMessage(
        '[ORCHESTRATION] Planning response received; parsing and validating plan'
      )
    ).toBe(false);
  });
});

describe('selectRenderableSessionLogs', () => {
  it('falls back to raw logs when clean mode would hide every entry', () => {
    const logs = [
      { message: '"propertiesCount": 4,' },
      { message: '"schemaChars": 120,' },
    ];

    expect(selectRenderableSessionLogs(logs, 'clean')).toEqual(logs);
  });

  it('keeps clean filtering when at least one useful log remains', () => {
    const logs = [
      { message: '"propertiesCount": 4,' },
      { message: 'Task completed successfully' },
    ];

    expect(selectRenderableSessionLogs(logs, 'clean')).toEqual([
      { message: 'Task completed successfully' },
    ]);
  });
});

import { describe, expect, it } from 'vitest';
import {
  InvalidPatientCodeError,
  PiiLeakError,
  assertNoIdentifiers,
  assertPseudonymousPatientCode,
  buildUploadPayload,
  minimizeWindow,
} from '../privacy/data_minimizer';
import type { BehavioralWindow } from '../types';

const baseWindow: BehavioralWindow = {
  windowStart: 1_700_000_000_000,
  windowMs: 3_600_000,
  utcOffsetMinutes: 60,
  samples: [
    { feature: 'activity_ratio', value: 0.42, confidence: 1 },
    { feature: 'mobility_radius_m', value: 1834.2, confidence: 0.75 },
  ],
};

describe('assertPseudonymousPatientCode', () => {
  it('accepts a clinic issued code', () => {
    expect(() => assertPseudonymousPatientCode('TRC-2f81a9c4')).not.toThrow();
  });

  it('rejects an email address', () => {
    expect(() => assertPseudonymousPatientCode('anna.becker@clinic.example')).toThrow(
      InvalidPatientCodeError,
    );
  });

  it('rejects a person name', () => {
    expect(() => assertPseudonymousPatientCode('Anna Becker')).toThrow(InvalidPatientCodeError);
  });

  it('rejects a bare medical record number', () => {
    expect(() => assertPseudonymousPatientCode('884120397')).toThrow(InvalidPatientCodeError);
  });

  it('rejects a code that is too short to be pseudonymous', () => {
    expect(() => assertPseudonymousPatientCode('ab12')).toThrow(InvalidPatientCodeError);
  });
});

describe('minimizeWindow', () => {
  it('drops features outside the vocabulary', () => {
    const result = minimizeWindow({
      ...baseWindow,
      samples: [
        ...baseWindow.samples,
        // Cast models a contributor adding a field that typechecked upstream.
        { feature: 'home_address' as never, value: 1, confidence: 1 },
      ],
    });

    expect(result.samples.map((s) => s.feature)).toEqual(['activity_ratio', 'mobility_radius_m']);
  });

  it('keeps the first occurrence of a duplicated feature', () => {
    const result = minimizeWindow({
      ...baseWindow,
      samples: [
        { feature: 'activity_ratio', value: 0.1, confidence: 1 },
        { feature: 'activity_ratio', value: 0.9, confidence: 1 },
      ],
    });

    expect(result.samples).toHaveLength(1);
    expect(result.samples[0]?.value).toBe(0.1);
  });

  it('clamps a value above the feature ceiling', () => {
    const result = minimizeWindow({
      ...baseWindow,
      samples: [{ feature: 'activity_ratio', value: 4.5, confidence: 1 }],
    });

    expect(result.samples[0]?.value).toBe(1);
  });

  it('turns a non finite reading into zero at zero confidence', () => {
    const result = minimizeWindow({
      ...baseWindow,
      samples: [{ feature: 'movement_intensity', value: Number.NaN, confidence: 1 }],
    });

    expect(result.samples[0]).toEqual({
      feature: 'movement_intensity',
      value: 0,
      confidence: 0,
    });
  });
});

describe('assertNoIdentifiers', () => {
  it('passes a clean payload', () => {
    expect(() =>
      assertNoIdentifiers({ v: 1, patientCode: 'TRC-2f81a9c4', windows: [baseWindow] }),
    ).not.toThrow();
  });

  it('rejects a forbidden key anywhere in the tree', () => {
    expect(() => assertNoIdentifiers({ windows: [{ latitude: 48.2 }] })).toThrow(PiiLeakError);
  });

  it('rejects a forbidden key regardless of casing or separators', () => {
    expect(() => assertNoIdentifiers({ device_ID: 'abc' })).toThrow(PiiLeakError);
  });

  it('rejects an embedded email address', () => {
    expect(() => assertNoIdentifiers({ note: 'sent to anna@clinic.example' })).toThrow(PiiLeakError);
  });

  it('rejects a non finite number', () => {
    expect(() => assertNoIdentifiers({ value: Number.POSITIVE_INFINITY })).toThrow(PiiLeakError);
  });
});

describe('buildUploadPayload', () => {
  it('produces exactly the wire shape', () => {
    const payload = buildUploadPayload({
      patientCode: 'TRC-2f81a9c4',
      sdkVersion: '0.1.0',
      batchId: 'b6f8e0f2-3a4c-4d5e-8f90-1a2b3c4d5e6f',
      createdAt: 1_700_000_600_000,
      windows: [baseWindow],
    });

    expect(Object.keys(payload).sort()).toEqual([
      'batchId',
      'createdAt',
      'patientCode',
      'sdkVersion',
      'v',
      'windows',
    ]);
    expect(payload.windows[0]?.samples).toHaveLength(2);
  });

  it('strips a coordinate a sensor smuggled onto the window object', () => {
    const contaminated = {
      ...baseWindow,
      coords: { lat: 48.2, lng: 16.3 },
    } as unknown as BehavioralWindow;

    const payload = buildUploadPayload({
      patientCode: 'TRC-2f81a9c4',
      sdkVersion: '0.1.0',
      batchId: 'b6f8e0f2-3a4c-4d5e-8f90-1a2b3c4d5e6f',
      createdAt: 1_700_000_600_000,
      windows: [contaminated],
    });

    // The whitelist rebuild removes it, and the scan then confirms the
    // rebuilt payload is clean. Both halves have to work for this to pass.
    expect(JSON.stringify(payload)).not.toContain('coords');
    expect(Object.keys(payload.windows[0] as object).sort()).toEqual([
      'samples',
      'utcOffsetMinutes',
      'windowMs',
      'windowStart',
    ]);
  });

  it('throws when the patient code itself carries identity', () => {
    expect(() =>
      buildUploadPayload({
        patientCode: 'anna.becker@clinic.example',
        sdkVersion: '0.1.0',
        batchId: 'b6f8e0f2-3a4c-4d5e-8f90-1a2b3c4d5e6f',
        createdAt: 1_700_000_600_000,
        windows: [baseWindow],
      }),
    ).toThrow(PiiLeakError);
  });
});

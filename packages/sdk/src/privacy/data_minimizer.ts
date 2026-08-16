import { BEHAVIORAL_FEATURES } from '../types';
import { clamp, round } from '../internal/statistics';
import type { BehavioralFeature, BehavioralWindow, FeatureSample } from '../types';

/**
 * The last checkpoint before anything leaves the device.
 *
 * Every sensor module is already written to emit nothing but numbers, so in
 * normal operation this file changes nothing. It exists because a privacy
 * guarantee that depends on every future contributor remembering the rule is
 * not a guarantee. The minimizer rebuilds the payload from a whitelist and
 * then scans what it built, so a field added carelessly upstream fails here
 * instead of arriving at the server.
 *
 * The rule enforced is narrow and checkable: the upload contains feature
 * names from a fixed vocabulary, finite numbers, timestamps, a pseudonymous
 * patient code and an SDK version. Nothing else.
 */

const ALLOWED_FEATURES = new Set<string>(BEHAVIORAL_FEATURES);

/** Upper bounds per feature. Values above these indicate a sensor fault. */
const FEATURE_CEILINGS: Record<BehavioralFeature, number> = {
  activity_ratio: 1,
  movement_intensity: 16,
  longest_sedentary_minutes: 1440,
  activity_fragmentation: 600,
  mobility_radius_m: 500000,
  location_entropy: 8,
  place_count: 64,
  primary_place_ratio: 1,
  screen_on_minutes: 1440,
  unlock_count: 2000,
  mean_session_minutes: 1440,
  usage_loop_index: 100,
  night_usage_minutes: 1440,
  communication_session_count: 500,
  communication_minutes: 1440,
};

/**
 * Key names that must never appear anywhere in an upload payload.
 * Checked against the serialised object, not against a type, because the
 * failure mode being defended against is a value that typechecked fine.
 */
const FORBIDDEN_KEYS = [
  'latitude',
  'longitude',
  'lat',
  'lng',
  'lon',
  'coords',
  'coordinates',
  'address',
  'name',
  'firstname',
  'lastname',
  'fullname',
  'email',
  'phone',
  'msisdn',
  'imei',
  'imsi',
  'macaddress',
  'advertisingid',
  'idfa',
  'gaid',
  'androidid',
  'deviceid',
  'serial',
  'ssid',
  'bssid',
  'contact',
  'contacts',
  'message',
  'messages',
  'transcript',
  'text',
  'content',
  'body',
  'packagename',
  'bundleid',
  'appname',
  'url',
  'query',
  'mrn',
  'ssn',
  'dob',
  'birthdate',
];

const EMAIL_PATTERN = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;
const PHONE_PATTERN = /(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{3}[\s.-]?\d{3,4}[\s.-]?\d{0,4}/;
const IMEI_PATTERN = /\b\d{15}\b/;

export class PiiLeakError extends Error {
  readonly path: string;

  constructor(path: string, detail: string) {
    super(`Upload payload rejected at "${path}": ${detail}`);
    this.name = 'PiiLeakError';
    this.path = path;
  }
}

export class InvalidPatientCodeError extends Error {
  constructor(detail: string) {
    super(`patientCode rejected: ${detail}`);
    this.name = 'InvalidPatientCodeError';
  }
}

/**
 * Reject a patient code that carries identity.
 *
 * Clinics do reach for a name or an email when wiring up an integration. The
 * SDK refuses at start rather than uploading identified data for months and
 * discovering it during an audit.
 */
export function assertPseudonymousPatientCode(code: string): void {
  const trimmed = code.trim();

  if (trimmed.length < 6) {
    throw new InvalidPatientCodeError('must be at least 6 characters');
  }
  if (trimmed.length > 128) {
    throw new InvalidPatientCodeError('must be at most 128 characters');
  }
  if (EMAIL_PATTERN.test(trimmed)) {
    throw new InvalidPatientCodeError('looks like an email address');
  }
  if (/\s/.test(trimmed)) {
    throw new InvalidPatientCodeError('must not contain whitespace, which suggests a person name');
  }
  if (!/^[A-Za-z0-9_.:-]+$/.test(trimmed)) {
    throw new InvalidPatientCodeError('must contain only letters, digits and the set _.:-');
  }
  if (/^\d{6,}$/.test(trimmed)) {
    throw new InvalidPatientCodeError(
      'is a bare digit string, which is usually a medical record number',
    );
  }
}

/**
 * Rebuild a window from the whitelist.
 *
 * Unknown features are dropped rather than passed through, duplicates keep
 * the first occurrence, and non finite values become zero at zero confidence
 * so the scoring engine can tell a fault from a genuine reading of zero.
 */
export function minimizeWindow(window: BehavioralWindow): BehavioralWindow {
  const seen = new Set<string>();
  const samples: FeatureSample[] = [];

  for (const sample of window.samples) {
    if (!ALLOWED_FEATURES.has(sample.feature)) continue;
    if (seen.has(sample.feature)) continue;
    seen.add(sample.feature);

    const ceiling = FEATURE_CEILINGS[sample.feature];
    const finite = Number.isFinite(sample.value);

    samples.push({
      feature: sample.feature,
      value: finite ? round(clamp(sample.value, 0, ceiling), 4) : 0,
      confidence: finite ? round(clamp(sample.confidence, 0, 1), 3) : 0,
    });
  }

  return {
    windowStart: Math.floor(window.windowStart),
    windowMs: Math.floor(window.windowMs),
    utcOffsetMinutes: Math.trunc(window.utcOffsetMinutes),
    samples,
  };
}

export interface UploadPayload {
  readonly v: 1;
  readonly patientCode: string;
  readonly sdkVersion: string;
  readonly batchId: string;
  readonly createdAt: number;
  readonly windows: readonly BehavioralWindow[];
}

/** Build the plaintext payload that will be encrypted into the envelope. */
export function buildUploadPayload(input: {
  patientCode: string;
  sdkVersion: string;
  batchId: string;
  createdAt: number;
  windows: readonly BehavioralWindow[];
}): UploadPayload {
  const payload: UploadPayload = {
    v: 1,
    patientCode: input.patientCode,
    sdkVersion: input.sdkVersion,
    batchId: input.batchId,
    createdAt: Math.floor(input.createdAt),
    windows: input.windows.map(minimizeWindow),
  };

  assertNoIdentifiers(payload);
  return payload;
}

/**
 * Depth first scan for forbidden keys and identifier shaped values.
 *
 * Throws rather than redacting. A silent redaction hides the fact that
 * something upstream is putting identity into the pipeline, and that is a bug
 * worth surfacing loudly during integration.
 */
export function assertNoIdentifiers(value: unknown, path = '$'): void {
  if (value === null || value === undefined) return;

  if (typeof value === 'number') {
    if (!Number.isFinite(value)) {
      throw new PiiLeakError(path, 'non finite number');
    }
    return;
  }

  if (typeof value === 'boolean') return;

  if (typeof value === 'string') {
    assertStringIsSafe(value, path);
    return;
  }

  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i += 1) {
      assertNoIdentifiers(value[i], `${path}[${i}]`);
    }
    return;
  }

  if (typeof value === 'object') {
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      const normalisedKey = key.toLowerCase().replace(/[^a-z]/g, '');
      if (FORBIDDEN_KEYS.includes(normalisedKey)) {
        throw new PiiLeakError(`${path}.${key}`, 'key is on the forbidden identifier list');
      }
      assertNoIdentifiers(child, `${path}.${key}`);
    }
    return;
  }

  throw new PiiLeakError(path, `unsupported value type ${typeof value}`);
}

function assertStringIsSafe(value: string, path: string): void {
  if (value.length > 256) {
    throw new PiiLeakError(path, 'string longer than 256 characters, which is never a feature name');
  }
  if (EMAIL_PATTERN.test(value)) {
    throw new PiiLeakError(path, 'value contains an email address');
  }
  if (IMEI_PATTERN.test(value)) {
    throw new PiiLeakError(path, 'value contains a 15 digit device identifier');
  }

  // The patient code and the SDK version are the only strings in the payload
  // that are not drawn from the feature vocabulary, and both are constrained
  // to a narrow character set. A string with spaces and digit runs is prose
  // or a phone number, neither of which belongs here.
  if (/\s/.test(value) && PHONE_PATTERN.test(value)) {
    throw new PiiLeakError(path, 'value looks like a phone number');
  }
}

/** Feature vocabulary check exposed for host app tests. */
export function isKnownFeature(name: string): name is BehavioralFeature {
  return ALLOWED_FEATURES.has(name);
}

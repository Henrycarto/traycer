import type {
  AlertRecord,
  PatientSummary,
  SdkTelemetry,
  Severity,
  TrajectorySeries,
  VoiceSample,
} from './types';

/**
 * Generated data for the landing page and for a dashboard running without a
 * backend.
 *
 * Deterministic, seeded, and obviously synthetic. Nothing in this file came
 * from a person. Behavioral traces from psychiatric patients do not belong in
 * a public repository even in anonymised form, and a fixture drawn from one
 * real patient would put that patient's routine into a git history forever.
 *
 * The shapes are drawn to match what the literature describes rather than to
 * look dramatic: a slow contraction over two to three weeks, not a cliff.
 */

const WARNING_THRESHOLD = 55;
const ALERT_THRESHOLD = 75;

/** Small deterministic PRNG so every render produces the same series. */
function seeded(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 0xffffffff;
  };
}

function dayIso(daysAgo: number): string {
  const date = new Date(Date.UTC(2026, 7, 16));
  date.setUTCDate(date.getUTCDate() - daysAgo);
  return date.toISOString().slice(0, 10);
}

function hoursAgoIso(hours: number): string {
  const date = new Date(Date.UTC(2026, 7, 16, 8, 0, 0));
  date.setUTCHours(date.getUTCHours() - hours);
  return date.toISOString();
}

export type TrajectoryShape = 'stable' | 'drifting' | 'crossing' | 'critical';

/**
 * Thirty daily scores.
 *
 * `crossing` is the shape the landing page shows: eighteen quiet days, then a
 * gradual climb that enters the warning band around day twenty two. That is
 * roughly the interval the prodrome literature describes, and it is the case
 * the whole product exists to catch.
 */
export function generateTrajectory(shape: TrajectoryShape, seed: number): number[] {
  const random = seeded(seed);
  const scores: number[] = [];

  for (let day = 0; day < 30; day += 1) {
    const noise = (random() - 0.5) * 6;
    let value: number;

    switch (shape) {
      case 'stable':
        value = 28 + noise;
        break;
      case 'drifting':
        value = 30 + day * 0.7 + noise;
        break;
      case 'crossing':
        value = day < 18 ? 31 + noise : 31 + (day - 18) * 3.4 + noise;
        break;
      case 'critical':
        value = day < 12 ? 34 + noise : 34 + (day - 12) * 3.1 + noise;
        break;
    }

    scores.push(Math.max(0, Math.min(100, Number(value.toFixed(1)))));
  }

  return scores;
}

export function demoTrajectorySeries(
  patientCode: string,
  shape: TrajectoryShape = 'crossing',
  seed = 41,
  baselineQuality: TrajectorySeries['baselineQuality'] = 'established',
): TrajectorySeries {
  const scores = generateTrajectory(shape, seed);

  return {
    patientCode,
    points: scores.map((score, index) => ({
      date: dayIso(29 - index),
      score,
      // One thin day in the middle of the window, because real sensor
      // coverage is never perfect and the chart has to show that honestly.
      sparse: index === 11,
    })),
    baseline: 31,
    warningThreshold: WARNING_THRESHOLD,
    alertThreshold: ALERT_THRESHOLD,
    baselineQuality,
  };
}

interface DemoPatientSpec {
  code: string;
  shape: TrajectoryShape;
  seed: number;
  sdkState: PatientSummary['sdkState'];
  baselineQuality: PatientSummary['baselineQuality'];
  signal: string | null;
  lastSeenHours: number | null;
}

const PANEL: DemoPatientSpec[] = [
  { code: 'TRC-4f21a9c4', shape: 'critical', seed: 11, sdkState: 'active', baselineQuality: 'established', signal: 'mobility_radius_m', lastSeenHours: 1 },
  { code: 'TRC-9b03e7d1', shape: 'crossing', seed: 23, sdkState: 'active', baselineQuality: 'established', signal: 'night_usage_minutes', lastSeenHours: 2 },
  { code: 'TRC-2c88f014', shape: 'crossing', seed: 37, sdkState: 'active', baselineQuality: 'established', signal: 'communication_minutes', lastSeenHours: 3 },
  { code: 'TRC-71de4a6b', shape: 'drifting', seed: 53, sdkState: 'active', baselineQuality: 'established', signal: 'activity_ratio', lastSeenHours: 1 },
  { code: 'TRC-05a7c2ff', shape: 'drifting', seed: 67, sdkState: 'stale', baselineQuality: 'established', signal: 'usage_loop_index', lastSeenHours: 19 },
  { code: 'TRC-3e6b19aa', shape: 'stable', seed: 71, sdkState: 'active', baselineQuality: 'established', signal: null, lastSeenHours: 1 },
  { code: 'TRC-8d40b5e2', shape: 'stable', seed: 83, sdkState: 'active', baselineQuality: 'established', signal: null, lastSeenHours: 2 },
  { code: 'TRC-1a9f7c30', shape: 'stable', seed: 97, sdkState: 'active', baselineQuality: 'established', signal: null, lastSeenHours: 4 },
  { code: 'TRC-6b2e8d55', shape: 'stable', seed: 103, sdkState: 'active', baselineQuality: 'established', signal: null, lastSeenHours: 5 },
  { code: 'TRC-c40a1b77', shape: 'stable', seed: 109, sdkState: 'stale', baselineQuality: 'established', signal: null, lastSeenHours: 26 },
  { code: 'TRC-e17d3f92', shape: 'stable', seed: 127, sdkState: 'enrolling', baselineQuality: 'provisional', signal: null, lastSeenHours: 1 },
  { code: 'TRC-a83c6e10', shape: 'stable', seed: 131, sdkState: 'enrolling', baselineQuality: 'insufficient', signal: null, lastSeenHours: 2 },
  { code: 'TRC-5f92d4b8', shape: 'stable', seed: 149, sdkState: 'inactive', baselineQuality: 'established', signal: null, lastSeenHours: 96 },
];

function severityFor(score: number): Severity {
  if (score >= ALERT_THRESHOLD) return 'critical';
  if (score >= WARNING_THRESHOLD) return 'elevated';
  return 'stable';
}

export function demoPatients(): PatientSummary[] {
  return PANEL.map((spec) => {
    const scores = generateTrajectory(spec.shape, spec.seed);
    const latest = scores[scores.length - 1] as number;
    const hasScore = spec.baselineQuality === 'established' && spec.sdkState !== 'inactive';

    return {
      patientCode: spec.code,
      displayLabel: spec.code,
      riskScore: hasScore ? latest : null,
      severity: hasScore ? severityFor(latest) : 'stable',
      sdkState: spec.sdkState,
      baselineQuality: spec.baselineQuality,
      sparkline: scores.slice(-7),
      lastSeenAt: spec.lastSeenHours === null ? null : hoursAgoIso(spec.lastSeenHours),
      dominantSignal: hasScore && latest >= WARNING_THRESHOLD ? spec.signal : null,
    };
  });
}

const ALERT_SUMMARIES: Record<string, string> = {
  mobility_radius_m:
    'Sustained deviation across 4 windows: movement radius well below their usual range.',
  night_usage_minutes:
    'Deviation across 2 windows: phone use during the night, which is new for them.',
  communication_minutes:
    'Deviation across 3 windows: less time in communication apps than usual.',
  activity_ratio: 'Deviation across 2 windows: a drop in daily physical activity.',
  usage_loop_index: 'Deviation across 2 windows: a repetitive phone checking pattern.',
  location_entropy: 'Deviation across 3 windows: fewer places visited than usual.',
};

export function demoAlerts(): AlertRecord[] {
  const rows: Array<[string, string, Severity, number, number, boolean]> = [
    ['TRC-4f21a9c4', 'mobility_radius_m', 'critical', 82.4, 2, false],
    ['TRC-9b03e7d1', 'night_usage_minutes', 'elevated', 63.1, 6, false],
    ['TRC-2c88f014', 'communication_minutes', 'elevated', 58.7, 11, false],
    ['TRC-4f21a9c4', 'activity_ratio', 'elevated', 61.9, 27, true],
    ['TRC-05a7c2ff', 'usage_loop_index', 'elevated', 57.2, 34, true],
    ['TRC-71de4a6b', 'location_entropy', 'elevated', 56.4, 51, true],
  ];

  return rows.map(([patientCode, signal, severity, score, hoursAgo, acknowledged], index) => ({
    alertId: `demo-alert-${index + 1}`,
    patientCode,
    firedAt: hoursAgoIso(hoursAgo),
    severity,
    riskScore: score,
    triggerSignal: signal,
    summary: ALERT_SUMMARIES[signal] ?? 'Deviation from personal baseline.',
    acknowledged,
  }));
}

export function demoTrajectory(patientCode: string): TrajectorySeries {
  const spec = PANEL.find((entry) => entry.code === patientCode);
  // Baseline quality has to match what the panel reports for the same
  // patient. When these disagree the detail page shows a score for a patient
  // the panel says has no baseline, which is exactly the contradiction the
  // interface exists to avoid.
  return demoTrajectorySeries(
    patientCode,
    spec?.shape ?? 'crossing',
    spec?.seed ?? 41,
    spec?.baselineQuality ?? 'established',
  );
}

export function demoVoiceSample(patientCode: string): VoiceSample {
  return {
    patientCode,
    recordedAt: hoursAgoIso(20),
    sampleSeconds: 18.4,
    quality: 'usable',
    extractor: 'opensmile/eGeMAPSv02.conf',
    features: [
      { feature: 'vocal_energy', value: 0.41, unit: 'loudness', baseline: 0.61 },
      { feature: 'energy_variability', value: 0.58, unit: 'loudness_sd', baseline: 0.74 },
      { feature: 'pitch_mean_semitones', value: 27.9, unit: 'semitones_from_27.5hz', baseline: 28.4 },
      { feature: 'pitch_variance', value: 0.11, unit: 'semitones_sd', baseline: 0.18 },
      { feature: 'speech_rate', value: 2.8, unit: 'syllables_per_second', baseline: 3.9 },
      { feature: 'articulation_rate', value: 2.1, unit: 'voiced_segments_per_second', baseline: 2.7 },
      { feature: 'pause_frequency', value: 126.0, unit: 'pauses_per_minute', baseline: 162.0 },
      { feature: 'mean_pause_seconds', value: 0.52, unit: 'seconds', baseline: 0.31 },
      { feature: 'jitter', value: 0.024, unit: 'ratio', baseline: 0.021 },
      { feature: 'shimmer', value: 1.21, unit: 'ratio', baseline: 1.14 },
      { feature: 'harmonicity', value: 9.1, unit: 'db', baseline: 9.8 },
    ],
  };
}

export function demoTelemetry(patientCode: string): SdkTelemetry {
  const spec = PANEL.find((entry) => entry.code === patientCode);

  return {
    patientCode,
    state: spec?.sdkState ?? 'active',
    sdkVersion: '0.1.0',
    lastWindowAt: hoursAgoIso(spec?.lastSeenHours ?? 1),
    lastUploadAt: hoursAgoIso((spec?.lastSeenHours ?? 1) + 1),
    queuedWindows: 6,
    activeDomains: ['motion', 'mobility', 'usage'],
    recentReadings: [
      { feature: 'activity_ratio', value: 0.14, unit: '', confidence: 0.96 },
      { feature: 'movement_intensity', value: 0.21, unit: 'g', confidence: 0.96 },
      { feature: 'longest_sedentary_minutes', value: 41.2, unit: 'min', confidence: 0.96 },
      { feature: 'mobility_radius_m', value: 184.0, unit: 'm', confidence: 0.72 },
      { feature: 'location_entropy', value: 0.32, unit: 'bits', confidence: 0.72 },
      { feature: 'primary_place_ratio', value: 0.91, unit: '', confidence: 0.72 },
      { feature: 'screen_on_minutes', value: 22.6, unit: 'min', confidence: 1.0 },
      { feature: 'unlock_count', value: 34, unit: '', confidence: 1.0 },
      { feature: 'night_usage_minutes', value: 47.0, unit: 'min', confidence: 1.0 },
      { feature: 'communication_minutes', value: 1.4, unit: 'min', confidence: 1.0 },
    ],
  };
}

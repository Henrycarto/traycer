/** Domain types shared across the Pulse dashboard. */

export type Severity = 'stable' | 'elevated' | 'critical';

export type SdkState = 'active' | 'stale' | 'inactive' | 'enrolling';

export type BaselineQuality = 'insufficient' | 'provisional' | 'established';

/** One day on the trajectory line. */
export interface TrajectoryPoint {
  /** ISO date, day resolution. */
  date: string;
  /** Behavioral risk score for the day, 0 to 100. */
  score: number;
  /** True when the day was too thinly sensed to trust. */
  sparse?: boolean;
}

export interface TrajectorySeries {
  patientCode: string;
  points: TrajectoryPoint[];
  /** The patient's own baseline score. The dashed line on the chart. */
  baseline: number;
  /** Score at which the warning band starts. */
  warningThreshold: number;
  /** Score at which the alert classifier fires. */
  alertThreshold: number;
  baselineQuality: BaselineQuality;
}

export interface PatientSummary {
  patientCode: string;
  displayLabel: string;
  riskScore: number | null;
  severity: Severity;
  sdkState: SdkState;
  baselineQuality: BaselineQuality;
  /** Last seven daily scores, oldest first. Drives the row sparkline. */
  sparkline: number[];
  lastSeenAt: string | null;
  dominantSignal: string | null;
}

export interface AlertRecord {
  alertId: string;
  patientCode: string;
  firedAt: string;
  severity: Severity;
  riskScore: number;
  triggerSignal: string;
  summary: string;
  acknowledged: boolean;
}

export type VoiceFeatureName =
  | 'vocal_energy'
  | 'energy_variability'
  | 'pitch_mean_semitones'
  | 'pitch_variance'
  | 'speech_rate'
  | 'articulation_rate'
  | 'pause_frequency'
  | 'mean_pause_seconds'
  | 'voiced_ratio'
  | 'jitter'
  | 'shimmer'
  | 'harmonicity'
  | 'spectral_slope';

export interface AcousticFeature {
  feature: VoiceFeatureName;
  value: number;
  unit: string;
  /** The patient's own baseline for this feature, when one exists. */
  baseline: number | null;
}

export interface VoiceSample {
  patientCode: string;
  recordedAt: string;
  sampleSeconds: number;
  quality: 'usable' | 'noisy' | 'too_short' | 'no_speech';
  extractor: string;
  features: AcousticFeature[];
}

export interface SensorReading {
  feature: string;
  value: number;
  unit: string;
  confidence: number;
}

export interface SdkTelemetry {
  patientCode: string;
  state: SdkState;
  sdkVersion: string | null;
  lastWindowAt: string | null;
  lastUploadAt: string | null;
  queuedWindows: number;
  activeDomains: Array<'motion' | 'mobility' | 'usage'>;
  recentReadings: SensorReading[];
}

/** Every API response carries this envelope. */
export interface ApiEnvelope<T> {
  data: T | null;
  error: { code: string; message: string; details?: Record<string, unknown> } | null;
  meta: { request_id: string; service: string; version: string; timestamp: string };
}

/**
 * Public type surface for @traycer/sdk.
 *
 * Every type here is part of the package contract. Changing a field name in
 * this file is a breaking change for the host app and for the ingest API,
 * because the wire payload is built directly from these shapes.
 */

/**
 * The complete set of behavioral features the SDK is allowed to emit.
 *
 * This union is deliberately closed. A sensor module cannot invent a feature
 * name at runtime, which means the ingest API can validate against a fixed
 * vocabulary and a reviewer can read one list to know everything that leaves
 * the device.
 */
export type BehavioralFeature =
  // Motion
  | 'activity_ratio'
  | 'movement_intensity'
  | 'longest_sedentary_minutes'
  | 'activity_fragmentation'
  // Mobility
  | 'mobility_radius_m'
  | 'location_entropy'
  | 'place_count'
  | 'primary_place_ratio'
  // Phone usage
  | 'screen_on_minutes'
  | 'unlock_count'
  | 'mean_session_minutes'
  | 'usage_loop_index'
  | 'night_usage_minutes'
  // Social interaction proxies
  | 'communication_session_count'
  | 'communication_minutes';

export const BEHAVIORAL_FEATURES: readonly BehavioralFeature[] = [
  'activity_ratio',
  'movement_intensity',
  'longest_sedentary_minutes',
  'activity_fragmentation',
  'mobility_radius_m',
  'location_entropy',
  'place_count',
  'primary_place_ratio',
  'screen_on_minutes',
  'unlock_count',
  'mean_session_minutes',
  'usage_loop_index',
  'night_usage_minutes',
  'communication_session_count',
  'communication_minutes',
] as const;

export type SensorDomain = 'motion' | 'mobility' | 'usage';

/**
 * A single aggregated feature value for one collection window.
 *
 * There is no raw signal in this shape by design. Raw accelerometer traces,
 * coordinates and app identifiers are reduced inside the sensor module and
 * never reach the queue.
 */
export interface FeatureSample {
  readonly feature: BehavioralFeature;
  readonly value: number;
  /**
   * Fraction of the window during which the underlying sensor was actually
   * readable, from 0 to 1. The scoring engine discounts low-confidence
   * windows rather than dropping them, so a phone left charging overnight
   * does not read as a behavioral change.
   */
  readonly confidence: number;
}

/** One collection window, already reduced to features. */
export interface BehavioralWindow {
  /** Window start, epoch milliseconds, UTC. */
  readonly windowStart: number;
  /** Window length in milliseconds. */
  readonly windowMs: number;
  /**
   * Minutes offset from UTC at the time of collection. The scoring engine
   * needs local time of day to model circadian structure, and this is enough
   * to recover it without sending a timezone name that narrows location.
   */
  readonly utcOffsetMinutes: number;
  readonly samples: readonly FeatureSample[];
}

/** A window packaged for upload. */
export interface QueuedBatch {
  readonly batchId: string;
  readonly createdAt: number;
  readonly sdkVersion: string;
  readonly windows: readonly BehavioralWindow[];
  /** Number of delivery attempts already made. */
  readonly attempts: number;
  /** Epoch milliseconds before which no further attempt should be made. */
  readonly nextAttemptAt: number;
}

export interface EncryptedEnvelope {
  readonly v: 1;
  readonly batchId: string;
  readonly alg: 'A256GCM';
  readonly keyAlg: 'RSA-OAEP-256';
  /** Base64 encoded RSA-OAEP wrapped AES-256 content key. */
  readonly wrappedKey: string;
  /** Base64 encoded 96 bit GCM nonce. */
  readonly iv: string;
  /** Base64 encoded AES-GCM ciphertext with the authentication tag appended. */
  readonly ciphertext: string;
  /** Key id of the server public key used to wrap the content key. */
  readonly keyId: string;
}

/* ---------------------------------------------------------------------- */
/* Host application adapters                                              */
/* ---------------------------------------------------------------------- */

/**
 * Durable key value storage. Backed by AsyncStorage in a normal React Native
 * app. The SDK never assumes the store is fast, so all access is awaited.
 */
export interface StorageAdapter {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  removeItem(key: string): Promise<void>;
}

/** Minimal accelerometer stream contract, satisfied by react-native-sensors. */
export interface AccelerometerReading {
  readonly x: number;
  readonly y: number;
  readonly z: number;
  readonly timestamp: number;
}

export interface AccelerometerAdapter {
  /** Requested sample rate in hertz. Implementations may deliver less. */
  setUpdateInterval(intervalMs: number): void;
  subscribe(onReading: (reading: AccelerometerReading) => void): () => void;
}

/**
 * A coarse position fix. The SDK reduces a sequence of these to a radius and
 * an entropy value, then discards them. Coordinates never enter the queue.
 */
export interface PositionFix {
  readonly latitude: number;
  readonly longitude: number;
  readonly accuracyM: number;
  readonly timestamp: number;
}

export interface LocationAdapter {
  /** Resolve a single coarse fix, or null when location is unavailable. */
  getCurrentPosition(): Promise<PositionFix | null>;
}

export type ScreenEventType = 'screen_on' | 'screen_off' | 'unlock';

export interface ScreenEvent {
  readonly type: ScreenEventType;
  readonly timestamp: number;
}

/**
 * Foreground application session. The SDK receives a category, never a
 * package name, so an app list cannot be reconstructed from the payload.
 */
export type AppCategory = 'communication' | 'other';

export interface AppSession {
  readonly category: AppCategory;
  readonly startedAt: number;
  readonly endedAt: number;
}

export interface UsageAdapter {
  /** Screen events recorded since the given timestamp. */
  getScreenEvents(sinceMs: number): Promise<readonly ScreenEvent[]>;
  /** Foreground sessions since the given timestamp, already categorised. */
  getAppSessions(sinceMs: number): Promise<readonly AppSession[]>;
  /** Whether the host app currently holds usage access permission. */
  hasPermission(): Promise<boolean>;
}

/** Background scheduler contract, satisfied by react-native-background-fetch. */
export interface SchedulerAdapter {
  schedule(taskId: string, intervalMinutes: number, handler: () => Promise<void>): Promise<void>;
  cancel(taskId: string): Promise<void>;
}

/** Network transport for the encrypted envelope. */
export interface TransportAdapter {
  post(url: string, body: string, headers: Record<string, string>): Promise<TransportResponse>;
}

export interface TransportResponse {
  readonly status: number;
  readonly body: string;
}

/**
 * Network reachability, satisfied by @react-native-community/netinfo.
 * Only used to honour the wifiOnly upload setting. When absent the SDK
 * uploads on any connection.
 */
export interface NetworkAdapter {
  isConnected(): Promise<boolean>;
  isUnmetered(): Promise<boolean>;
}

/** Hybrid encryption provider. See internal/crypto.ts for the default. */
export interface CryptoAdapter {
  randomBytes(length: number): Promise<Uint8Array>;
  encryptBatch(plaintext: string, batchId: string): Promise<EncryptedEnvelope>;
}

export type LogLevel = 'debug' | 'info' | 'warn' | 'error' | 'silent';

export interface Logger {
  debug(message: string, context?: Record<string, unknown>): void;
  info(message: string, context?: Record<string, unknown>): void;
  warn(message: string, context?: Record<string, unknown>): void;
  error(message: string, context?: Record<string, unknown>): void;
}

/* ---------------------------------------------------------------------- */
/* Configuration                                                          */
/* ---------------------------------------------------------------------- */

export interface ServerPublicKey {
  /** Identifier echoed in the envelope so the server can select a key. */
  readonly keyId: string;
  /** RSA public key in JWK form, 2048 bit or larger, for RSA-OAEP-256. */
  readonly jwk: JsonWebKey;
}

export interface CollectionConfig {
  /** Length of one aggregation window in minutes. Default 60. */
  readonly windowMinutes: number;
  /** Accelerometer sample rate in hertz during a burst. Default 25. */
  readonly motionSampleHz: number;
  /** Seconds of accelerometer sampling per window. Default 60. */
  readonly motionBurstSeconds: number;
  /** Number of location fixes taken per window. Default 4. */
  readonly locationFixesPerWindow: number;
  /**
   * Skip a motion burst when the battery is below this fraction and the
   * device is not charging. Default 0.15.
   */
  readonly batteryFloor: number;
}

export interface UploadConfig {
  /** Ingest endpoint that accepts the encrypted envelope. */
  readonly endpoint: string;
  /** Windows accumulated before an upload is attempted. Default 24. */
  readonly batchSize: number;
  /** Upload attempt interval in minutes. Default 180. */
  readonly flushIntervalMinutes: number;
  /** Attempts before a batch is dropped. Default 8. */
  readonly maxAttempts: number;
  /** Upper bound on queued batches held on device. Default 64. */
  readonly maxQueuedBatches: number;
  /** Only upload while on unmetered network. Default true. */
  readonly wifiOnly: boolean;
}

export interface TraycerConfig {
  /**
   * Pseudonymous patient code issued by the clinic during enrollment. This is
   * not a name, an email or a medical record number. The SDK will reject a
   * value that looks like direct identity.
   */
  readonly patientCode: string;
  /** Device scoped JWT used as the ingest bearer token. */
  readonly deviceToken: string;
  readonly serverPublicKey: ServerPublicKey;
  readonly upload: Partial<UploadConfig> & Pick<UploadConfig, 'endpoint'>;
  readonly collection?: Partial<CollectionConfig>;
  readonly adapters: {
    readonly storage: StorageAdapter;
    readonly accelerometer?: AccelerometerAdapter;
    readonly location?: LocationAdapter;
    readonly usage?: UsageAdapter;
    readonly scheduler: SchedulerAdapter;
    readonly transport?: TransportAdapter;
    readonly crypto?: CryptoAdapter;
    readonly battery?: BatteryAdapter;
    readonly network?: NetworkAdapter;
  };
  readonly logLevel?: LogLevel;
  readonly logger?: Logger;
}

export interface BatteryAdapter {
  getLevel(): Promise<number>;
  isCharging(): Promise<boolean>;
}

export type SdkState = 'idle' | 'running' | 'paused' | 'stopped';

export interface SdkStatus {
  readonly state: SdkState;
  readonly sdkVersion: string;
  readonly lastWindowAt: number | null;
  readonly lastUploadAt: number | null;
  readonly queuedBatches: number;
  readonly queuedWindows: number;
  readonly activeDomains: readonly SensorDomain[];
  readonly lastError: string | null;
}

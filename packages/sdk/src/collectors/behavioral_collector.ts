import { MotionSensor } from '../sensors/accelerometer';
import { MobilitySensor } from '../sensors/mobility';
import { PhoneUsageSensor } from '../sensors/phone_usage';
import { minimizeWindow } from '../privacy/data_minimizer';
import type {
  AccelerometerAdapter,
  BatteryAdapter,
  BehavioralWindow,
  CollectionConfig,
  FeatureSample,
  LocationAdapter,
  Logger,
  SensorDomain,
  StorageAdapter,
  UsageAdapter,
} from '../types';

/**
 * Runs one collection window across every available sensor domain.
 *
 * The collector is the only place that knows what a window is. Sensors do not
 * schedule themselves and the queue does not know how features are produced,
 * which keeps the three concerns independently testable and lets a host app
 * swap any one of them.
 *
 * Partial windows are the normal case, not an error case. A patient may have
 * granted motion but not location, or may be on iOS where usage statistics
 * are unavailable. Every domain that fails contributes its features at zero
 * confidence so the scoring engine sees an explicit gap rather than an
 * implicit zero.
 */

const LAST_WINDOW_KEY = 'traycer.collector.lastWindow.v1';

export const DEFAULT_COLLECTION_CONFIG: CollectionConfig = {
  windowMinutes: 60,
  motionSampleHz: 25,
  motionBurstSeconds: 60,
  locationFixesPerWindow: 4,
  batteryFloor: 0.15,
};

export interface CollectorAdapters {
  readonly storage: StorageAdapter;
  readonly accelerometer?: AccelerometerAdapter;
  readonly location?: LocationAdapter;
  readonly usage?: UsageAdapter;
  readonly battery?: BatteryAdapter;
}

export interface CollectionOutcome {
  readonly window: BehavioralWindow;
  readonly domains: readonly SensorDomain[];
  readonly skipped: readonly { domain: SensorDomain; reason: string }[];
}

export class BehavioralCollector {
  private readonly config: CollectionConfig;
  private readonly storage: StorageAdapter;
  private readonly logger: Logger;
  private readonly motion: MotionSensor | null;
  private readonly mobility: MobilitySensor | null;
  private readonly usage: PhoneUsageSensor | null;
  private readonly now: () => number;

  constructor(
    adapters: CollectorAdapters,
    config: CollectionConfig,
    logger: Logger,
    now: () => number = () => Date.now(),
  ) {
    this.config = config;
    this.storage = adapters.storage;
    this.logger = logger;
    this.now = now;

    this.motion = adapters.accelerometer
      ? new MotionSensor(adapters.accelerometer, logger, adapters.battery)
      : null;
    this.mobility = adapters.location
      ? new MobilitySensor(adapters.location, adapters.storage, logger)
      : null;
    this.usage = adapters.usage ? new PhoneUsageSensor(adapters.usage, logger) : null;
  }

  get activeDomains(): readonly SensorDomain[] {
    const domains: SensorDomain[] = [];
    if (this.motion) domains.push('motion');
    if (this.mobility) domains.push('mobility');
    if (this.usage) domains.push('usage');
    return domains;
  }

  /**
   * Collect one window ending now.
   *
   * The three domains run concurrently. The motion burst dominates wall clock
   * at roughly sixty seconds, and running location fixes inside that same
   * period costs nothing extra because the GPS warm up overlaps the burst.
   */
  async collectWindow(): Promise<CollectionOutcome> {
    const windowEnd = this.now();
    const windowMs = this.config.windowMinutes * 60 * 1000;
    const windowStart = windowEnd - windowMs;
    const utcOffsetMinutes = -new Date(windowEnd).getTimezoneOffset();

    const samples: FeatureSample[] = [];
    const domains: SensorDomain[] = [];
    const skipped: { domain: SensorDomain; reason: string }[] = [];

    const fixSpacingMs =
      this.config.locationFixesPerWindow > 1
        ? Math.floor((this.config.motionBurstSeconds * 1000) / (this.config.locationFixesPerWindow - 1))
        : 0;

    const [motionResult, mobilityResult, usageResult] = await Promise.all([
      this.motion
        ? this.motion.collect({
            sampleHz: this.config.motionSampleHz,
            burstSeconds: this.config.motionBurstSeconds,
            batteryFloor: this.config.batteryFloor,
          })
        : Promise.resolve(null),
      this.mobility
        ? this.mobility.collect(
            { fixesPerWindow: this.config.locationFixesPerWindow, fixSpacingMs },
            windowEnd,
          )
        : Promise.resolve(null),
      this.usage
        ? this.usage.collect(windowStart, windowEnd, utcOffsetMinutes)
        : Promise.resolve(null),
    ]);

    if (motionResult) {
      samples.push(...motionResult.samples);
      if (motionResult.skipped) {
        skipped.push({ domain: 'motion', reason: motionResult.skipReason ?? 'unknown' });
      } else {
        domains.push('motion');
      }
    }

    if (mobilityResult) {
      samples.push(...mobilityResult.samples);
      if (mobilityResult.skipped) {
        skipped.push({ domain: 'mobility', reason: mobilityResult.skipReason ?? 'unknown' });
      } else {
        domains.push('mobility');
      }
    }

    if (usageResult) {
      samples.push(...usageResult.samples);
      if (usageResult.skipped) {
        skipped.push({ domain: 'usage', reason: usageResult.skipReason ?? 'unknown' });
      } else {
        domains.push('usage');
      }
    }

    const window = minimizeWindow({ windowStart, windowMs, utcOffsetMinutes, samples });

    await this.storage.setItem(LAST_WINDOW_KEY, String(windowEnd));

    if (skipped.length > 0) {
      this.logger.debug('window collected with gaps', { skipped });
    }

    return { window, domains, skipped };
  }

  async lastWindowAt(): Promise<number | null> {
    const raw = await this.storage.getItem(LAST_WINDOW_KEY);
    if (!raw) return null;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : null;
  }
}

export function resolveCollectionConfig(input?: Partial<CollectionConfig>): CollectionConfig {
  const merged = { ...DEFAULT_COLLECTION_CONFIG, ...(input ?? {}) };

  // A burst longer than the window means continuous sampling, which is the
  // one configuration that will get the host app removed from the app store
  // for battery use. Refuse it rather than honour it.
  const maxBurstSeconds = Math.floor(merged.windowMinutes * 60 * 0.2);
  if (merged.motionBurstSeconds > maxBurstSeconds) {
    throw new Error(
      `motionBurstSeconds ${merged.motionBurstSeconds} exceeds 20 percent of the ` +
        `${merged.windowMinutes} minute window. Maximum is ${maxBurstSeconds}.`,
    );
  }

  if (merged.motionSampleHz < 1 || merged.motionSampleHz > 50) {
    throw new Error('motionSampleHz must be between 1 and 50.');
  }

  return merged;
}

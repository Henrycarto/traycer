import { clamp, mean, percentile, round, standardDeviation } from '../internal/statistics';
import type {
  AccelerometerAdapter,
  AccelerometerReading,
  BatteryAdapter,
  FeatureSample,
  Logger,
} from '../types';

/**
 * Motion sensing.
 *
 * The accelerometer is the most expensive sensor in the SDK by a wide margin,
 * so it is sampled in a short burst once per window rather than streamed. A
 * 60 second burst at 25 hertz is enough to separate rest from movement and to
 * measure how fragmented the movement is, which is the part that matters
 * clinically. Psychomotor retardation shows up as long unbroken still periods
 * and low intensity when movement does happen. Agitation shows up as high
 * fragmentation without a matching rise in mobility radius.
 *
 * Raw readings never leave this module. The burst array is reduced to four
 * numbers and released before the function returns.
 */

/** Gravity in metres per second squared. Readings are normalised against it. */
const GRAVITY = 9.80665;

/**
 * Vector magnitude above gravity that counts as movement.
 *
 * 0.12 g was chosen because it sits above the noise floor of the cheapest
 * accelerometers we tested on and below the magnitude of a phone being picked
 * up off a table. A lower threshold makes every device look active.
 */
const MOVEMENT_THRESHOLD_G = 0.12;

/** Consecutive still seconds before a period counts as a sedentary bout. */
const SEDENTARY_BOUT_SECONDS = 60;

export interface MotionBurstOptions {
  readonly sampleHz: number;
  readonly burstSeconds: number;
  readonly batteryFloor: number;
}

export interface MotionResult {
  readonly samples: readonly FeatureSample[];
  /** True when the burst was skipped, for status reporting. */
  readonly skipped: boolean;
  readonly skipReason?: string;
}

const EMPTY_MOTION_FEATURES: readonly FeatureSample[] = [
  { feature: 'activity_ratio', value: 0, confidence: 0 },
  { feature: 'movement_intensity', value: 0, confidence: 0 },
  { feature: 'longest_sedentary_minutes', value: 0, confidence: 0 },
  { feature: 'activity_fragmentation', value: 0, confidence: 0 },
];

export class MotionSensor {
  private readonly adapter: AccelerometerAdapter;
  private readonly battery: BatteryAdapter | undefined;
  private readonly logger: Logger;

  constructor(adapter: AccelerometerAdapter, logger: Logger, battery?: BatteryAdapter) {
    this.adapter = adapter;
    this.logger = logger;
    this.battery = battery;
  }

  /**
   * Run one burst and reduce it to motion features.
   *
   * Resolves rather than rejects on sensor failure. A background task that
   * throws on one bad window loses the other sensors in the same window, and
   * a gap in one domain is far less damaging than a gap in all three.
   */
  async collect(options: MotionBurstOptions): Promise<MotionResult> {
    const allowed = await this.batteryAllows(options.batteryFloor);
    if (!allowed) {
      this.logger.debug('motion burst skipped, battery below floor');
      return { samples: EMPTY_MOTION_FEATURES, skipped: true, skipReason: 'battery' };
    }

    let readings: AccelerometerReading[];
    try {
      readings = await this.runBurst(options);
    } catch (error) {
      this.logger.warn('motion burst failed', { reason: describeError(error) });
      return { samples: EMPTY_MOTION_FEATURES, skipped: true, skipReason: 'sensor_error' };
    }

    const expected = Math.max(1, Math.round(options.sampleHz * options.burstSeconds));
    const confidence = clamp(readings.length / expected, 0, 1);

    if (readings.length < expected * 0.25) {
      this.logger.debug('motion burst returned too few readings', {
        received: readings.length,
        expected,
      });
      return {
        samples: EMPTY_MOTION_FEATURES.map((sample) => ({ ...sample, confidence })),
        skipped: true,
        skipReason: 'insufficient_samples',
      };
    }

    return { samples: reduceMotion(readings, options.sampleHz, confidence), skipped: false };
  }

  private async batteryAllows(floor: number): Promise<boolean> {
    if (!this.battery) return true;
    try {
      if (await this.battery.isCharging()) return true;
      return (await this.battery.getLevel()) >= floor;
    } catch {
      // A battery adapter that throws should not stop collection entirely.
      return true;
    }
  }

  private runBurst(options: MotionBurstOptions): Promise<AccelerometerReading[]> {
    return new Promise((resolve, reject) => {
      const readings: AccelerometerReading[] = [];
      const intervalMs = Math.max(10, Math.round(1000 / options.sampleHz));
      let unsubscribe: (() => void) | null = null;
      let timer: ReturnType<typeof setTimeout> | null = null;
      let settled = false;

      const finish = (): void => {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        try {
          unsubscribe?.();
        } catch {
          // Adapter teardown failures are not worth losing the burst over.
        }
        resolve(readings);
      };

      try {
        this.adapter.setUpdateInterval(intervalMs);
        unsubscribe = this.adapter.subscribe((reading) => {
          readings.push(reading);
        });
      } catch (error) {
        settled = true;
        reject(error);
        return;
      }

      timer = setTimeout(finish, options.burstSeconds * 1000);
    });
  }
}

/**
 * Reduce a burst to four features.
 *
 * Exported for tests and for host apps that want to run the same reduction
 * over their own accelerometer pipeline.
 */
export function reduceMotion(
  readings: readonly AccelerometerReading[],
  sampleHz: number,
  confidence: number,
): readonly FeatureSample[] {
  const magnitudes: number[] = new Array(readings.length);

  for (let i = 0; i < readings.length; i += 1) {
    const reading = readings[i] as AccelerometerReading;
    const magnitude = Math.sqrt(
      reading.x * reading.x + reading.y * reading.y + reading.z * reading.z,
    );
    // Some platforms report in g, others in metres per second squared. A
    // magnitude near 9.8 at rest means the reading is not normalised.
    const inG = magnitude > 3 ? magnitude / GRAVITY : magnitude;
    magnitudes[i] = Math.abs(inG - 1);
  }

  let activeCount = 0;
  let transitions = 0;
  let currentStillRun = 0;
  let longestStillRun = 0;
  let previousActive: boolean | null = null;

  for (let i = 0; i < magnitudes.length; i += 1) {
    const active = (magnitudes[i] as number) >= MOVEMENT_THRESHOLD_G;

    if (active) {
      activeCount += 1;
      currentStillRun = 0;
    } else {
      currentStillRun += 1;
      if (currentStillRun > longestStillRun) longestStillRun = currentStillRun;
    }

    if (previousActive !== null && previousActive !== active) transitions += 1;
    previousActive = active;
  }

  const activityRatio = magnitudes.length > 0 ? activeCount / magnitudes.length : 0;
  const longestStillSeconds = longestStillRun / sampleHz;

  // Intensity uses the 90th percentile rather than the mean. The mean is
  // dominated by the still portion of the burst for almost every patient,
  // which flattens the difference between a slow walk and a brisk one.
  const intensity = percentile(magnitudes, 0.9);

  // Fragmentation is transitions per minute of burst. Two patients can share
  // an activity ratio of 0.3 while one took a single walk and the other moved
  // in dozens of short restless bursts. That difference is the signal.
  const burstMinutes = magnitudes.length / sampleHz / 60;
  const fragmentation = burstMinutes > 0 ? transitions / burstMinutes : 0;

  return [
    { feature: 'activity_ratio', value: round(activityRatio, 4), confidence },
    { feature: 'movement_intensity', value: round(intensity, 4), confidence },
    {
      feature: 'longest_sedentary_minutes',
      value: round(longestStillSeconds >= SEDENTARY_BOUT_SECONDS ? longestStillSeconds / 60 : 0, 3),
      confidence,
    },
    { feature: 'activity_fragmentation', value: round(fragmentation, 3), confidence },
  ];
}

/** Exposed so tests can assert the noise floor behaviour directly. */
export function magnitudeStatistics(readings: readonly AccelerometerReading[]): {
  mean: number;
  sd: number;
} {
  const magnitudes = readings.map((r) => Math.sqrt(r.x * r.x + r.y * r.y + r.z * r.z));
  return { mean: mean(magnitudes), sd: standardDeviation(magnitudes) };
}

function describeError(error: unknown): string {
  if (error instanceof Error) return error.name;
  return 'unknown';
}

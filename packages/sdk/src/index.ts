import {
  BehavioralCollector,
  DEFAULT_COLLECTION_CONFIG,
  resolveCollectionConfig,
} from './collectors/behavioral_collector';
import { DEFAULT_UPLOAD_CONFIG, UploadQueue, resolveUploadConfig } from './collectors/upload_queue';
import { assertPseudonymousPatientCode } from './privacy/data_minimizer';
import { createWebCryptoAdapter } from './internal/crypto';
import { createFetchTransport } from './adapters/react-native';
import { createLogger, silentLogger } from './internal/logger';
import type {
  BehavioralWindow,
  CollectionConfig,
  Logger,
  SdkState,
  SdkStatus,
  TraycerConfig,
  UploadConfig,
} from './types';

/**
 * @traycer/sdk
 *
 * Passive behavioral sensing for mental health applications. The SDK collects
 * aggregated behavioral features on a schedule, encrypts them on device and
 * uploads them in batches. It never reads message content, contact lists, app
 * inventories, call recipients, or location coordinates.
 *
 * The scoring model that turns these features into a relapse trajectory is
 * not part of this package. What is here is the sensing and privacy layer,
 * and it is complete.
 */

export const SDK_VERSION = '0.1.0';

const COLLECT_TASK_ID = 'com.traycer.collect';
const FLUSH_TASK_ID = 'com.traycer.flush';

export class TraycerNotStartedError extends Error {
  constructor(method: string) {
    super(`Traycer.${method} was called before start().`);
    this.name = 'TraycerNotStartedError';
  }
}

interface RuntimeState {
  config: TraycerConfig;
  collection: CollectionConfig;
  upload: UploadConfig;
  collector: BehavioralCollector;
  queue: UploadQueue;
  logger: Logger;
  deviceToken: string;
  state: SdkState;
  lastError: string | null;
}

let runtime: RuntimeState | null = null;

function requireRuntime(method: string): RuntimeState {
  if (!runtime) throw new TraycerNotStartedError(method);
  return runtime;
}

/**
 * Start passive collection.
 *
 * Idempotent. Calling start twice with the same configuration is a no op,
 * which matters because React Native remounts the root component on a fast
 * refresh and host apps commonly call start from an effect.
 */
export async function start(config: TraycerConfig): Promise<void> {
  if (runtime && runtime.state === 'running') {
    runtime.logger.debug('start called while already running, ignoring');
    return;
  }

  assertPseudonymousPatientCode(config.patientCode);

  const logger =
    config.logger ?? (config.logLevel === 'silent' ? silentLogger : createLogger(config.logLevel));

  const collection = resolveCollectionConfig(config.collection);
  const upload = resolveUploadConfig(config.upload);

  if (!/^https:\/\//i.test(upload.endpoint) && !upload.endpoint.startsWith('http://localhost')) {
    throw new Error('upload.endpoint must use https outside of localhost development.');
  }

  const crypto = config.adapters.crypto ?? createWebCryptoAdapter(config.serverPublicKey);
  const transport = config.adapters.transport ?? createFetchTransport();

  const collector = new BehavioralCollector(
    {
      storage: config.adapters.storage,
      accelerometer: config.adapters.accelerometer,
      location: config.adapters.location,
      usage: config.adapters.usage,
      battery: config.adapters.battery,
    },
    collection,
    logger,
  );

  if (collector.activeDomains.length === 0) {
    throw new Error(
      'No sensor adapters were supplied. Provide at least one of accelerometer, location or usage.',
    );
  }

  const queue = new UploadQueue({
    storage: config.adapters.storage,
    crypto,
    transport,
    logger,
    config: upload,
    patientCode: config.patientCode,
    deviceToken: config.deviceToken,
    sdkVersion: SDK_VERSION,
    network: config.adapters.network,
  });

  runtime = {
    config,
    collection,
    upload,
    collector,
    queue,
    logger,
    deviceToken: config.deviceToken,
    state: 'running',
    lastError: null,
  };

  await config.adapters.scheduler.schedule(COLLECT_TASK_ID, collection.windowMinutes, async () => {
    await runCollectionTask();
  });

  await config.adapters.scheduler.schedule(FLUSH_TASK_ID, upload.flushIntervalMinutes, async () => {
    await runFlushTask();
  });

  logger.info('traycer started', {
    domains: collector.activeDomains,
    windowMinutes: collection.windowMinutes,
    batchSize: upload.batchSize,
  });
}

/** Stop collection and cancel the background tasks. Queued data is kept. */
export async function stop(): Promise<void> {
  const current = runtime;
  if (!current) return;

  await current.config.adapters.scheduler.cancel(COLLECT_TASK_ID);
  await current.config.adapters.scheduler.cancel(FLUSH_TASK_ID);
  current.state = 'stopped';
  current.logger.info('traycer stopped');
}

/**
 * Suspend collection without cancelling the scheduled tasks.
 *
 * Used when a patient temporarily withdraws. The tasks keep firing and return
 * immediately, which is cheaper than tearing down and rebuilding the OS level
 * schedule, and it avoids the Android limit on frequent task registration.
 */
export function pause(): void {
  const current = requireRuntime('pause');
  current.state = 'paused';
  current.logger.info('traycer paused');
}

export function resume(): void {
  const current = requireRuntime('resume');
  current.state = 'running';
  current.logger.info('traycer resumed');
}

/** Replace an expired device JWT without restarting collection. */
export function updateDeviceToken(token: string): void {
  const current = requireRuntime('updateDeviceToken');
  current.deviceToken = token;

  // The queue captured the old token at construction, so it is rebuilt with
  // the same storage and therefore the same persisted batches.
  runtime = {
    ...current,
    deviceToken: token,
    queue: new UploadQueue({
      storage: current.config.adapters.storage,
      crypto: current.config.adapters.crypto ?? createWebCryptoAdapter(current.config.serverPublicKey),
      transport: current.config.adapters.transport ?? createFetchTransport(),
      logger: current.logger,
      config: current.upload,
      patientCode: current.config.patientCode,
      deviceToken: token,
      sdkVersion: SDK_VERSION,
      network: current.config.adapters.network,
    }),
  };
}

/** Collect one window immediately. Exposed for integration testing. */
export async function collectNow(): Promise<BehavioralWindow> {
  const current = requireRuntime('collectNow');
  const outcome = await current.collector.collectWindow();
  await current.queue.enqueue(outcome.window);
  return outcome.window;
}

/** Attempt an upload immediately. */
export async function flushNow(): Promise<void> {
  const current = requireRuntime('flushNow');
  const result = await current.queue.flush();
  current.logger.debug('manual flush complete', { ...result });
}

/**
 * Delete every queued window on the device.
 *
 * Called when a patient withdraws consent. Data already delivered to the
 * platform is deleted through the clinician dashboard, not from here, because
 * the device cannot prove which server side records belong to it.
 */
export async function purgeLocalData(): Promise<void> {
  const current = requireRuntime('purgeLocalData');
  await current.queue.purge();
  current.logger.info('local queue purged');
}

export async function getStatus(): Promise<SdkStatus> {
  if (!runtime) {
    return {
      state: 'idle',
      sdkVersion: SDK_VERSION,
      lastWindowAt: null,
      lastUploadAt: null,
      queuedBatches: 0,
      queuedWindows: 0,
      activeDomains: [],
      lastError: null,
    };
  }

  const [stats, lastWindowAt] = await Promise.all([
    runtime.queue.stats(),
    runtime.collector.lastWindowAt(),
  ]);

  return {
    state: runtime.state,
    sdkVersion: SDK_VERSION,
    lastWindowAt,
    lastUploadAt: stats.lastUploadAt,
    queuedBatches: stats.batches,
    queuedWindows: stats.windows,
    activeDomains: runtime.collector.activeDomains,
    lastError: runtime.lastError,
  };
}

async function runCollectionTask(): Promise<void> {
  const current = runtime;
  if (!current || current.state !== 'running') return;

  try {
    const outcome = await current.collector.collectWindow();
    await current.queue.enqueue(outcome.window);
    current.lastError = null;
  } catch (error) {
    // A background task that throws is killed by the OS and, on Android, can
    // be dropped from the schedule entirely. Swallowing here keeps the
    // schedule alive and records the failure for the status call.
    current.lastError = error instanceof Error ? error.message : 'collection failed';
    current.logger.error('collection window failed', { reason: current.lastError });
  }
}

async function runFlushTask(): Promise<void> {
  const current = runtime;
  if (!current || current.state !== 'running') return;

  try {
    const result = await current.queue.flush();
    current.logger.debug('scheduled flush complete', { ...result });
  } catch (error) {
    current.lastError = error instanceof Error ? error.message : 'flush failed';
    current.logger.error('scheduled flush failed', { reason: current.lastError });
  }
}

/** Reset module state. Test only. */
export function __resetForTests(): void {
  runtime = null;
}

export const Traycer = {
  start,
  stop,
  pause,
  resume,
  collectNow,
  flushNow,
  purgeLocalData,
  updateDeviceToken,
  getStatus,
  SDK_VERSION,
};

export default Traycer;

export { DEFAULT_COLLECTION_CONFIG, DEFAULT_UPLOAD_CONFIG };
export { BehavioralCollector } from './collectors/behavioral_collector';
export { UploadQueue } from './collectors/upload_queue';
export { MotionSensor, reduceMotion } from './sensors/accelerometer';
export { MobilitySensor, radiusOfGyration, reduceMobility } from './sensors/mobility';
export { PhoneUsageSensor, reduceUsage, usageLoopIndex } from './sensors/phone_usage';
export {
  assertNoIdentifiers,
  assertPseudonymousPatientCode,
  buildUploadPayload,
  isKnownFeature,
  minimizeWindow,
  InvalidPatientCodeError,
  PiiLeakError,
} from './privacy/data_minimizer';
export { createWebCryptoAdapter, decodeJwkFromBase64 } from './internal/crypto';
export { createLogger, silentLogger } from './internal/logger';
export * from './adapters/react-native';
export * from './types';

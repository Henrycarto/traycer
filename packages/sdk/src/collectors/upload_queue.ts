import { createBatchId } from '../internal/ids';
import { buildUploadPayload } from '../privacy/data_minimizer';
import { PiiLeakError } from '../privacy/data_minimizer';
import type {
  BehavioralWindow,
  CryptoAdapter,
  Logger,
  NetworkAdapter,
  QueuedBatch,
  StorageAdapter,
  TransportAdapter,
  UploadConfig,
} from '../types';

/**
 * Durable, batched, encrypted upload.
 *
 * Three constraints shape this file. The device is often offline. Background
 * execution time is granted in short unpredictable slices. And a patient in
 * relapse is precisely the patient most likely to leave the phone off the
 * charger, so the queue has to survive process death without duplicating or
 * losing windows.
 *
 * The design that follows from those constraints: windows accumulate in a
 * persisted buffer, a full buffer is sealed into an immutable batch with a
 * stable id, the batch is encrypted at send time and retried with exponential
 * backoff, and the server deduplicates on batch id so a retry after a lost
 * response is harmless.
 */

const QUEUE_KEY = 'traycer.queue.v1';

/** First retry delay. Doubles per attempt up to the ceiling. */
const BACKOFF_BASE_MS = 60 * 1000;
const BACKOFF_CEILING_MS = 6 * 60 * 60 * 1000;

export const DEFAULT_UPLOAD_CONFIG: Omit<UploadConfig, 'endpoint'> = {
  batchSize: 24,
  flushIntervalMinutes: 180,
  maxAttempts: 8,
  maxQueuedBatches: 64,
  wifiOnly: true,
};

interface QueueState {
  version: 1;
  pending: BehavioralWindow[];
  batches: QueuedBatch[];
  lastUploadAt: number | null;
}

export interface FlushResult {
  readonly attempted: number;
  readonly delivered: number;
  readonly deferred: number;
  readonly dropped: number;
  readonly reason?: string;
}

export interface UploadQueueOptions {
  readonly storage: StorageAdapter;
  readonly crypto: CryptoAdapter;
  readonly transport: TransportAdapter;
  readonly logger: Logger;
  readonly config: UploadConfig;
  readonly patientCode: string;
  readonly deviceToken: string;
  readonly sdkVersion: string;
  readonly network?: NetworkAdapter;
  /** Injectable for tests. Defaults to Date.now. */
  readonly now?: () => number;
  /** Injectable for tests. Defaults to Math.random. */
  readonly random?: () => number;
}

export class UploadQueue {
  private readonly options: UploadQueueOptions;
  private readonly now: () => number;
  private readonly random: () => number;
  private state: QueueState | null = null;
  /**
   * Serialises every storage mutation. Two background tasks can overlap on
   * Android, and a read modify write race here silently loses windows.
   */
  private chain: Promise<unknown> = Promise.resolve();

  constructor(options: UploadQueueOptions) {
    this.options = options;
    this.now = options.now ?? (() => Date.now());
    this.random = options.random ?? (() => Math.random());
  }

  /** Add one collected window. Seals a batch when the buffer is full. */
  async enqueue(window: BehavioralWindow): Promise<void> {
    await this.withLock(async () => {
      const state = await this.load();
      state.pending.push(window);

      if (state.pending.length >= this.options.config.batchSize) {
        await this.sealLocked(state);
      }

      await this.save(state);
    });
  }

  /**
   * Seal whatever is buffered into a batch even if it is short.
   * Called before a flush so a patient who generates few windows still gets
   * data uploaded on schedule.
   */
  async seal(): Promise<void> {
    await this.withLock(async () => {
      const state = await this.load();
      if (state.pending.length === 0) return;
      await this.sealLocked(state);
      await this.save(state);
    });
  }

  async flush(): Promise<FlushResult> {
    const gate = await this.networkAllows();
    if (!gate.allowed) {
      return { attempted: 0, delivered: 0, deferred: 0, dropped: 0, reason: gate.reason };
    }

    await this.seal();

    const due = await this.withLock(async () => {
      const state = await this.load();
      const nowMs = this.now();
      return state.batches.filter((batch) => batch.nextAttemptAt <= nowMs).map((b) => b.batchId);
    });

    let delivered = 0;
    let deferred = 0;
    let dropped = 0;

    for (const batchId of due) {
      const outcome = await this.sendOne(batchId);
      if (outcome === 'delivered') delivered += 1;
      else if (outcome === 'dropped') dropped += 1;
      else if (outcome === 'deferred') deferred += 1;
    }

    return { attempted: due.length, delivered, deferred, dropped };
  }

  async stats(): Promise<{ batches: number; windows: number; lastUploadAt: number | null }> {
    const state = await this.withLock(() => this.load());
    let windows = state.pending.length;
    for (const batch of state.batches) windows += batch.windows.length;
    return { batches: state.batches.length, windows, lastUploadAt: state.lastUploadAt };
  }

  /** Remove every queued window. Used when a patient withdraws consent. */
  async purge(): Promise<void> {
    await this.withLock(async () => {
      this.state = { version: 1, pending: [], batches: [], lastUploadAt: null };
      await this.options.storage.removeItem(QUEUE_KEY);
    });
  }

  /* ------------------------------------------------------------------ */

  private async sendOne(batchId: string): Promise<'delivered' | 'deferred' | 'dropped' | 'gone'> {
    const batch = await this.withLock(async () => {
      const state = await this.load();
      return state.batches.find((b) => b.batchId === batchId) ?? null;
    });

    if (!batch) return 'gone';

    let body: string;
    try {
      const payload = buildUploadPayload({
        patientCode: this.options.patientCode,
        sdkVersion: this.options.sdkVersion,
        batchId: batch.batchId,
        createdAt: batch.createdAt,
        windows: batch.windows,
      });

      const envelope = await this.options.crypto.encryptBatch(
        JSON.stringify(payload),
        batch.batchId,
      );
      body = JSON.stringify(envelope);
    } catch (error) {
      if (error instanceof PiiLeakError) {
        // The payload failed its own privacy check. Retrying will produce the
        // same payload, so the batch is destroyed rather than retried, and
        // the failure is logged loudly enough to be caught in integration.
        this.options.logger.error('batch dropped by the data minimizer', {
          batchId,
          path: error.path,
        });
        await this.removeBatch(batchId);
        return 'dropped';
      }

      this.options.logger.warn('batch encryption failed, will retry', {
        batchId,
        reason: error instanceof Error ? error.name : 'unknown',
      });
      await this.deferBatch(batchId);
      return 'deferred';
    }

    try {
      const response = await this.options.transport.post(this.options.config.endpoint, body, {
        'content-type': 'application/json',
        authorization: `Bearer ${this.options.deviceToken}`,
        'x-traycer-sdk-version': this.options.sdkVersion,
        'x-traycer-batch-id': batch.batchId,
      });

      // 409 means the server already has this batch id. That happens when a
      // response was lost after the server committed, and it is a success
      // from the device side.
      if ((response.status >= 200 && response.status < 300) || response.status === 409) {
        await this.removeBatch(batchId, true);
        return 'delivered';
      }

      if (response.status === 401 || response.status === 403) {
        // The device token has expired or been revoked. Keep the data and
        // back off. The host app is expected to refresh the token and call
        // updateDeviceToken.
        this.options.logger.warn('ingest rejected the device token', { status: response.status });
        await this.deferBatch(batchId);
        return 'deferred';
      }

      if (response.status >= 400 && response.status < 500 && response.status !== 408 && response.status !== 429) {
        this.options.logger.error('ingest rejected the batch permanently', {
          batchId,
          status: response.status,
        });
        await this.removeBatch(batchId);
        return 'dropped';
      }

      await this.deferBatch(batchId);
      return 'deferred';
    } catch (error) {
      this.options.logger.debug('upload attempt failed', {
        batchId,
        reason: error instanceof Error ? error.name : 'unknown',
      });
      await this.deferBatch(batchId);
      return 'deferred';
    }
  }

  private async removeBatch(batchId: string, markUploaded = false): Promise<void> {
    await this.withLock(async () => {
      const state = await this.load();
      state.batches = state.batches.filter((b) => b.batchId !== batchId);
      if (markUploaded) state.lastUploadAt = this.now();
      await this.save(state);
    });
  }

  private async deferBatch(batchId: string): Promise<void> {
    await this.withLock(async () => {
      const state = await this.load();
      const index = state.batches.findIndex((b) => b.batchId === batchId);
      if (index < 0) return;

      const batch = state.batches[index] as QueuedBatch;
      const attempts = batch.attempts + 1;

      if (attempts >= this.options.config.maxAttempts) {
        this.options.logger.warn('batch exhausted its retries and was dropped', {
          batchId,
          attempts,
        });
        state.batches.splice(index, 1);
        await this.save(state);
        return;
      }

      state.batches[index] = {
        ...batch,
        attempts,
        nextAttemptAt: this.now() + this.backoffMs(attempts),
      };
      await this.save(state);
    });
  }

  /**
   * Exponential backoff with full jitter. Without jitter a clinic that
   * onboards a cohort on the same afternoon produces a synchronised retry
   * storm against the ingest endpoint every time it has an outage.
   */
  private backoffMs(attempts: number): number {
    const ceiling = Math.min(BACKOFF_CEILING_MS, BACKOFF_BASE_MS * 2 ** (attempts - 1));
    return Math.floor(ceiling * (0.5 + this.random() * 0.5));
  }

  private async sealLocked(state: QueueState): Promise<void> {
    if (state.pending.length === 0) return;

    const batchId = await createBatchId((n) => this.options.crypto.randomBytes(n));
    const batch: QueuedBatch = {
      batchId,
      createdAt: this.now(),
      sdkVersion: this.options.sdkVersion,
      windows: state.pending,
      attempts: 0,
      nextAttemptAt: 0,
    };

    state.pending = [];
    state.batches.push(batch);

    // Oldest first when over the cap. Recent behavior is what the scoring
    // engine needs most, and a device this far behind is offline enough that
    // the old windows have already lost their clinical value.
    const overflow = state.batches.length - this.options.config.maxQueuedBatches;
    if (overflow > 0) {
      state.batches.splice(0, overflow);
      this.options.logger.warn('upload queue over capacity, oldest batches discarded', {
        discarded: overflow,
      });
    }
  }

  private async networkAllows(): Promise<{ allowed: boolean; reason?: string }> {
    const network = this.options.network;
    if (!network) return { allowed: true };

    try {
      if (!(await network.isConnected())) return { allowed: false, reason: 'offline' };
      if (this.options.config.wifiOnly && !(await network.isUnmetered())) {
        return { allowed: false, reason: 'metered_connection' };
      }
      return { allowed: true };
    } catch {
      // A failing reachability adapter should not stall uploads forever.
      return { allowed: true };
    }
  }

  private async load(): Promise<QueueState> {
    if (this.state) return this.state;

    const raw = await this.options.storage.getItem(QUEUE_KEY);
    if (!raw) {
      this.state = { version: 1, pending: [], batches: [], lastUploadAt: null };
      return this.state;
    }

    try {
      const parsed = JSON.parse(raw) as QueueState;
      this.state = {
        version: 1,
        pending: Array.isArray(parsed.pending) ? parsed.pending : [],
        batches: Array.isArray(parsed.batches) ? parsed.batches : [],
        lastUploadAt: typeof parsed.lastUploadAt === 'number' ? parsed.lastUploadAt : null,
      };
    } catch {
      // A truncated write from a process kill is recoverable only by
      // discarding. Losing a queue is acceptable, crashing on every start is
      // not.
      this.options.logger.error('upload queue was unreadable and has been reset');
      this.state = { version: 1, pending: [], batches: [], lastUploadAt: null };
    }

    return this.state;
  }

  private async save(state: QueueState): Promise<void> {
    this.state = state;
    await this.options.storage.setItem(QUEUE_KEY, JSON.stringify(state));
  }

  private withLock<T>(work: () => Promise<T>): Promise<T> {
    const run = this.chain.then(work, work);
    this.chain = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }
}

export function resolveUploadConfig(
  input: Partial<UploadConfig> & Pick<UploadConfig, 'endpoint'>,
): UploadConfig {
  return { ...DEFAULT_UPLOAD_CONFIG, ...input };
}

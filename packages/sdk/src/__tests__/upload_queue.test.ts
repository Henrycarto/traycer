import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UploadQueue, resolveUploadConfig } from '../collectors/upload_queue';
import { silentLogger } from '../internal/logger';
import type {
  BehavioralWindow,
  CryptoAdapter,
  EncryptedEnvelope,
  StorageAdapter,
  TransportAdapter,
  TransportResponse,
} from '../types';

class MemoryStorage implements StorageAdapter {
  readonly map = new Map<string, string>();

  async getItem(key: string): Promise<string | null> {
    return this.map.get(key) ?? null;
  }

  async setItem(key: string, value: string): Promise<void> {
    this.map.set(key, value);
  }

  async removeItem(key: string): Promise<void> {
    this.map.delete(key);
  }
}

/**
 * Records what it was asked to encrypt so a test can assert that a payload
 * containing an identifier never reaches the transport.
 */
const fakeCrypto: CryptoAdapter & { lastPlaintext: string | null } = {
  lastPlaintext: null,
  async randomBytes(length: number) {
    const out = new Uint8Array(length);
    for (let i = 0; i < length; i += 1) out[i] = (i * 37 + 11) % 256;
    return out;
  },
  async encryptBatch(plaintext: string, batchId: string): Promise<EncryptedEnvelope> {
    fakeCrypto.lastPlaintext = plaintext;
    return {
      v: 1,
      batchId,
      alg: 'A256GCM',
      keyAlg: 'RSA-OAEP-256',
      wrappedKey: 'd2s=',
      iv: 'aXY=',
      ciphertext: 'Y3Q=',
      keyId: 'test-key',
    };
  },
};

function makeTransport(responses: readonly TransportResponse[]): TransportAdapter & {
  calls: { url: string; body: string; headers: Record<string, string> }[];
} {
  const calls: { url: string; body: string; headers: Record<string, string> }[] = [];
  let index = 0;

  return {
    calls,
    async post(url, body, headers) {
      calls.push({ url, body, headers });
      const response = responses[Math.min(index, responses.length - 1)];
      index += 1;
      if (!response) throw new Error('network unreachable');
      return response;
    },
  };
}

function makeWindow(startOffsetMs: number): BehavioralWindow {
  return {
    windowStart: 1_700_000_000_000 + startOffsetMs,
    windowMs: 3_600_000,
    utcOffsetMinutes: 0,
    samples: [{ feature: 'activity_ratio', value: 0.3, confidence: 1 }],
  };
}

function makeQueue(options: {
  transport: TransportAdapter;
  storage?: MemoryStorage;
  batchSize?: number;
  maxAttempts?: number;
  maxQueuedBatches?: number;
  now?: () => number;
}): { queue: UploadQueue; storage: MemoryStorage } {
  const storage = options.storage ?? new MemoryStorage();

  const queue = new UploadQueue({
    storage,
    crypto: fakeCrypto,
    transport: options.transport,
    logger: silentLogger,
    config: resolveUploadConfig({
      endpoint: 'https://ingest.traycer.example/v1/batches',
      batchSize: options.batchSize ?? 2,
      maxAttempts: options.maxAttempts ?? 3,
      maxQueuedBatches: options.maxQueuedBatches ?? 8,
      wifiOnly: false,
    }),
    patientCode: 'TRC-2f81a9c4',
    deviceToken: 'device.jwt.token',
    sdkVersion: '0.1.0',
    now: options.now ?? (() => 1_700_000_000_000),
    random: () => 0.5,
  });

  return { queue, storage };
}

beforeEach(() => {
  fakeCrypto.lastPlaintext = null;
});

describe('UploadQueue', () => {
  it('holds windows until the batch is full', async () => {
    const transport = makeTransport([{ status: 200, body: '{}' }]);
    const { queue } = makeQueue({ transport, batchSize: 3 });

    await queue.enqueue(makeWindow(0));
    await queue.enqueue(makeWindow(1));

    expect((await queue.stats()).batches).toBe(0);
    expect((await queue.stats()).windows).toBe(2);

    await queue.enqueue(makeWindow(2));
    expect((await queue.stats()).batches).toBe(1);
  });

  it('sends an encrypted envelope with the device bearer token', async () => {
    const transport = makeTransport([{ status: 202, body: '{}' }]);
    const { queue } = makeQueue({ transport });

    await queue.enqueue(makeWindow(0));
    await queue.enqueue(makeWindow(1));
    const result = await queue.flush();

    expect(result.delivered).toBe(1);
    expect(transport.calls).toHaveLength(1);

    const call = transport.calls[0];
    expect(call?.headers.authorization).toBe('Bearer device.jwt.token');

    const sent = JSON.parse(call?.body ?? '{}') as EncryptedEnvelope;
    expect(sent.alg).toBe('A256GCM');
    expect(sent.keyAlg).toBe('RSA-OAEP-256');
    // The behavioral values must not be readable in the transmitted body.
    expect(call?.body).not.toContain('activity_ratio');
  });

  it('treats 409 as delivered because the server already has the batch', async () => {
    const transport = makeTransport([{ status: 409, body: 'duplicate' }]);
    const { queue } = makeQueue({ transport });

    await queue.enqueue(makeWindow(0));
    await queue.enqueue(makeWindow(1));

    expect((await queue.flush()).delivered).toBe(1);
    expect((await queue.stats()).batches).toBe(0);
  });

  it('keeps the batch and backs off on a server error', async () => {
    let clock = 1_700_000_000_000;
    const transport = makeTransport([{ status: 503, body: 'unavailable' }]);
    const { queue } = makeQueue({ transport, now: () => clock });

    await queue.enqueue(makeWindow(0));
    await queue.enqueue(makeWindow(1));

    expect((await queue.flush()).deferred).toBe(1);
    expect((await queue.stats()).batches).toBe(1);

    // The retry is not due yet, so a second flush attempts nothing.
    expect((await queue.flush()).attempted).toBe(0);

    clock += 10 * 60 * 1000;
    expect((await queue.flush()).attempted).toBe(1);
  });

  it('drops a batch the server rejects permanently', async () => {
    const transport = makeTransport([{ status: 422, body: 'schema mismatch' }]);
    const { queue } = makeQueue({ transport });

    await queue.enqueue(makeWindow(0));
    await queue.enqueue(makeWindow(1));

    expect((await queue.flush()).dropped).toBe(1);
    expect((await queue.stats()).batches).toBe(0);
  });

  it('keeps the batch when the token is rejected so data survives a refresh', async () => {
    const transport = makeTransport([{ status: 401, body: 'expired' }]);
    const { queue } = makeQueue({ transport });

    await queue.enqueue(makeWindow(0));
    await queue.enqueue(makeWindow(1));
    await queue.flush();

    expect((await queue.stats()).batches).toBe(1);
  });

  it('gives up after maxAttempts', async () => {
    let clock = 1_700_000_000_000;
    const transport = makeTransport([{ status: 500, body: 'boom' }]);
    const { queue } = makeQueue({ transport, maxAttempts: 3, now: () => clock });

    await queue.enqueue(makeWindow(0));
    await queue.enqueue(makeWindow(1));

    for (let i = 0; i < 3; i += 1) {
      await queue.flush();
      clock += 24 * 60 * 60 * 1000;
    }

    expect((await queue.stats()).batches).toBe(0);
  });

  it('discards the oldest batches when the queue is over capacity', async () => {
    const transport = makeTransport([{ status: 500, body: 'boom' }]);
    const { queue } = makeQueue({ transport, batchSize: 1, maxQueuedBatches: 3 });

    for (let i = 0; i < 6; i += 1) {
      await queue.enqueue(makeWindow(i));
    }

    expect((await queue.stats()).batches).toBe(3);
  });

  it('survives a process restart with the same batch id', async () => {
    const storage = new MemoryStorage();
    const transport = makeTransport([{ status: 500, body: 'boom' }]);

    const first = makeQueue({ transport, storage });
    await first.queue.enqueue(makeWindow(0));
    await first.queue.enqueue(makeWindow(1));
    await first.queue.flush();

    const firstBatchId = transport.calls[0]?.headers['x-traycer-batch-id'];
    expect(firstBatchId).toBeTruthy();

    // A new queue over the same storage models the app being killed.
    const okTransport = makeTransport([{ status: 200, body: '{}' }]);
    const second = makeQueue({
      transport: okTransport,
      storage,
      now: () => 1_700_000_000_000 + 60 * 60 * 1000,
    });

    await second.queue.flush();
    expect(okTransport.calls[0]?.headers['x-traycer-batch-id']).toBe(firstBatchId);
  });

  it('purges everything on consent withdrawal', async () => {
    const transport = makeTransport([{ status: 200, body: '{}' }]);
    const { queue } = makeQueue({ transport, batchSize: 1 });

    await queue.enqueue(makeWindow(0));
    await queue.purge();

    const stats = await queue.stats();
    expect(stats.batches).toBe(0);
    expect(stats.windows).toBe(0);
  });

  it('does not upload on a metered connection when wifiOnly is set', async () => {
    const transport = makeTransport([{ status: 200, body: '{}' }]);
    const storage = new MemoryStorage();

    const queue = new UploadQueue({
      storage,
      crypto: fakeCrypto,
      transport,
      logger: silentLogger,
      config: resolveUploadConfig({
        endpoint: 'https://ingest.traycer.example/v1/batches',
        batchSize: 1,
        wifiOnly: true,
      }),
      patientCode: 'TRC-2f81a9c4',
      deviceToken: 'device.jwt.token',
      sdkVersion: '0.1.0',
      network: {
        isConnected: async () => true,
        isUnmetered: async () => false,
      },
      now: () => 1_700_000_000_000,
    });

    await queue.enqueue(makeWindow(0));
    const result = await queue.flush();

    expect(result.reason).toBe('metered_connection');
    expect(transport.calls).toHaveLength(0);
  });

  it('serialises concurrent enqueues without losing windows', async () => {
    const transport = makeTransport([{ status: 200, body: '{}' }]);
    const { queue } = makeQueue({ transport, batchSize: 100 });

    await Promise.all(Array.from({ length: 25 }, (_, i) => queue.enqueue(makeWindow(i))));

    expect((await queue.stats()).windows).toBe(25);
  });

  it('encrypts a payload that carries no identifier beyond the patient code', async () => {
    const transport = makeTransport([{ status: 200, body: '{}' }]);
    const { queue } = makeQueue({ transport, batchSize: 1 });

    await queue.enqueue(makeWindow(0));
    await queue.flush();

    const payload = JSON.parse(fakeCrypto.lastPlaintext ?? '{}') as Record<string, unknown>;
    expect(Object.keys(payload).sort()).toEqual([
      'batchId',
      'createdAt',
      'patientCode',
      'sdkVersion',
      'v',
      'windows',
    ]);
  });
});

describe('resolveUploadConfig', () => {
  it('fills defaults around the required endpoint', () => {
    const config = resolveUploadConfig({ endpoint: 'https://example.test/ingest' });
    expect(config.batchSize).toBe(24);
    expect(config.wifiOnly).toBe(true);
  });
});

describe('vi sanity', () => {
  it('has fake timers available for future scheduler tests', () => {
    expect(typeof vi.useFakeTimers).toBe('function');
  });
});

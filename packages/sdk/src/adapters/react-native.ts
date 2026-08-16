import type {
  AccelerometerAdapter,
  BatteryAdapter,
  LocationAdapter,
  NetworkAdapter,
  SchedulerAdapter,
  StorageAdapter,
  TransportAdapter,
  TransportResponse,
} from '../types';

/**
 * Prebuilt adapters for a standard React Native bare workflow app.
 *
 * Every factory in this file resolves its dependency lazily. Importing
 * @traycer/sdk therefore pulls in none of the native modules, and a host app
 * that supplies its own adapters never has to install packages it does not
 * use. The peer dependencies are all declared optional for the same reason.
 */

type ModuleShape = Record<string, unknown>;

function loadModule(name: string): ModuleShape {
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires, global-require
    return require(name) as ModuleShape;
  } catch {
    throw new Error(
      `@traycer/sdk needs "${name}" for this adapter. Install it, or pass your own ` +
        'implementation through the adapters option.',
    );
  }
}

/** AsyncStorage backed durable storage. */
export function createAsyncStorageAdapter(): StorageAdapter {
  const mod = loadModule('@react-native-async-storage/async-storage');
  const storage = (mod.default ?? mod) as {
    getItem(key: string): Promise<string | null>;
    setItem(key: string, value: string): Promise<void>;
    removeItem(key: string): Promise<void>;
  };

  return {
    getItem: (key) => storage.getItem(key),
    setItem: (key, value) => storage.setItem(key, value),
    removeItem: (key) => storage.removeItem(key),
  };
}

/** react-native-sensors accelerometer stream. */
export function createSensorsAccelerometerAdapter(): AccelerometerAdapter {
  const mod = loadModule('react-native-sensors');
  const accelerometer = mod.accelerometer as {
    subscribe(next: (value: { x: number; y: number; z: number; timestamp: number }) => void): {
      unsubscribe(): void;
    };
  };
  const setUpdateIntervalForType = mod.setUpdateIntervalForType as (
    type: string,
    intervalMs: number,
  ) => void;
  const sensorTypes = mod.SensorTypes as Record<string, string>;

  return {
    setUpdateInterval(intervalMs: number) {
      setUpdateIntervalForType(sensorTypes.accelerometer as string, intervalMs);
    },
    subscribe(onReading) {
      const subscription = accelerometer.subscribe((value) => {
        onReading({
          x: value.x,
          y: value.y,
          z: value.z,
          timestamp: value.timestamp,
        });
      });
      return () => subscription.unsubscribe();
    },
  };
}

/**
 * react-native-geolocation-service coarse fixes.
 *
 * Balanced accuracy on purpose. High accuracy mode wakes the GPS chip, which
 * costs an order of magnitude more battery and buys nothing for a life space
 * measure computed at 100 metre resolution.
 */
export function createGeolocationAdapter(timeoutMs = 15000): LocationAdapter {
  const mod = loadModule('react-native-geolocation-service');
  const geo = (mod.default ?? mod) as {
    getCurrentPosition(
      success: (position: {
        coords: { latitude: number; longitude: number; accuracy: number };
        timestamp: number;
      }) => void,
      failure: (error: unknown) => void,
      options: Record<string, unknown>,
    ): void;
  };

  return {
    getCurrentPosition() {
      return new Promise((resolve) => {
        let settled = false;
        const finish = (value: Parameters<typeof resolve>[0]): void => {
          if (settled) return;
          settled = true;
          resolve(value);
        };

        geo.getCurrentPosition(
          (position) =>
            finish({
              latitude: position.coords.latitude,
              longitude: position.coords.longitude,
              accuracyM: position.coords.accuracy,
              timestamp: position.timestamp,
            }),
          () => finish(null),
          {
            accuracy: { android: 'balanced', ios: 'hundredMeters' },
            enableHighAccuracy: false,
            timeout: timeoutMs,
            maximumAge: 60000,
            showLocationDialog: false,
            forceRequestLocation: false,
          },
        );
      });
    },
  };
}

/** react-native-background-fetch scheduler. */
export function createBackgroundFetchScheduler(): SchedulerAdapter {
  const mod = loadModule('react-native-background-fetch');
  const backgroundFetch = (mod.default ?? mod) as {
    configure(
      options: Record<string, unknown>,
      onEvent: (taskId: string) => void,
      onTimeout: (taskId: string) => void,
    ): Promise<number>;
    finish(taskId: string): void;
    stop(taskId?: string): Promise<void>;
    scheduleTask(options: Record<string, unknown>): Promise<void>;
  };

  return {
    async schedule(taskId, intervalMinutes, handler) {
      await backgroundFetch.configure(
        {
          minimumFetchInterval: Math.max(15, Math.round(intervalMinutes)),
          stopOnTerminate: false,
          startOnBoot: true,
          enableHeadless: true,
          requiredNetworkType: 0,
          requiresCharging: false,
          requiresDeviceIdle: false,
          requiresBatteryNotLow: true,
        },
        async (firedTaskId: string) => {
          try {
            await handler();
          } finally {
            // The OS kills the app if finish is not called. It must run even
            // when the handler throws, which is why it lives in a finally.
            backgroundFetch.finish(firedTaskId);
          }
        },
        (timedOutTaskId: string) => {
          backgroundFetch.finish(timedOutTaskId);
        },
      );

      await backgroundFetch.scheduleTask({
        taskId,
        delay: intervalMinutes * 60 * 1000,
        periodic: true,
        enableHeadless: true,
        stopOnTerminate: false,
        startOnBoot: true,
      });
    },
    async cancel(taskId) {
      await backgroundFetch.stop(taskId);
    },
  };
}

/** Fetch based transport with a hard timeout. */
export function createFetchTransport(timeoutMs = 30000): TransportAdapter {
  return {
    async post(url, body, headers): Promise<TransportResponse> {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);

      try {
        const response = await fetch(url, {
          method: 'POST',
          headers,
          body,
          signal: controller.signal,
        });
        const text = await response.text();
        return { status: response.status, body: text };
      } finally {
        clearTimeout(timer);
      }
    },
  };
}

/** @react-native-community/netinfo reachability. */
export function createNetInfoAdapter(): NetworkAdapter {
  const mod = loadModule('@react-native-community/netinfo');
  const netInfo = (mod.default ?? mod) as {
    fetch(): Promise<{ isConnected: boolean | null; details?: { isConnectionExpensive?: boolean } | null; type: string }>;
  };

  return {
    async isConnected() {
      const state = await netInfo.fetch();
      return state.isConnected === true;
    },
    async isUnmetered() {
      const state = await netInfo.fetch();
      if (state.type === 'wifi' || state.type === 'ethernet') {
        return state.details?.isConnectionExpensive !== true;
      }
      return false;
    },
  };
}

/** react-native-device-info battery state. */
export function createDeviceInfoBatteryAdapter(): BatteryAdapter {
  const mod = loadModule('react-native-device-info');
  const deviceInfo = (mod.default ?? mod) as {
    getBatteryLevel(): Promise<number>;
    isBatteryCharging(): Promise<boolean>;
  };

  return {
    getLevel: () => deviceInfo.getBatteryLevel(),
    isCharging: () => deviceInfo.isBatteryCharging(),
  };
}

# @traycer/sdk

Passive behavioral sensing for React Native mental health apps.

The SDK runs in the background, samples the phone's own sensors on a schedule, reduces each window to a short list of numbers, encrypts them on device and uploads them in batches. Your users do not journal, log moods, or answer prompts. They carry the phone they already carry.

This package is the sensing and privacy layer. The scoring model that turns these features into a relapse trajectory is a separate hosted service and is not part of this package.

## What it collects

Fifteen aggregated numbers per window. That is the whole list.

| Domain | Features |
| --- | --- |
| Motion | `activity_ratio`, `movement_intensity`, `longest_sedentary_minutes`, `activity_fragmentation` |
| Mobility | `mobility_radius_m`, `location_entropy`, `place_count`, `primary_place_ratio` |
| Phone usage | `screen_on_minutes`, `unlock_count`, `mean_session_minutes`, `usage_loop_index`, `night_usage_minutes` |
| Social proxy | `communication_session_count`, `communication_minutes` |

## What it never collects

- No GPS coordinates. Location fixes are reduced to a radius and a distribution inside the sensor module and dropped in the same function call.
- No message, notification, or call content.
- No contact list, phone numbers, or call recipients.
- No app names or package identifiers. The usage adapter reports a category, not an app.
- No microphone access. Voice analysis is a separate optional service the patient triggers by hand.
- No advertising identifier, IMEI, MAC address, or device serial.

The enforcement point is `src/privacy/data_minimizer.ts`. Every upload is rebuilt from a fixed feature whitelist and then scanned for identifier shaped keys and values. A payload that fails the scan throws instead of uploading. Read that file first if you are evaluating this package for a privacy review.

## Install

```sh
npm install @traycer/sdk
```

Then add the native modules for the adapters you plan to use. All of them are optional peer dependencies, so install only what you need.

```sh
npm install \
  @react-native-async-storage/async-storage \
  react-native-background-fetch \
  react-native-sensors \
  react-native-geolocation-service \
  react-native-device-info \
  @react-native-community/netinfo

cd ios && pod install
```

The SDK also needs WebCrypto for the upload envelope. React Native does not ship it.

```sh
npm install react-native-quick-crypto
```

```ts
// index.js, before anything imports @traycer/sdk
import { install } from 'react-native-quick-crypto';
install();
```

If you already have your own crypto stack, skip that and pass an `adapters.crypto` implementation instead.

## Permissions

### iOS

`Info.plist`:

```xml
<key>NSLocationAlwaysAndWhenInUseUsageDescription</key>
<string>Your care team uses movement patterns to spot early changes in how you are doing.</string>
<key>NSMotionUsageDescription</key>
<string>Movement patterns help your care team notice changes before they become a crisis.</string>
<key>UIBackgroundModes</key>
<array>
  <string>fetch</string>
  <string>processing</string>
</array>
```

Screen usage statistics are not available on iOS without a Screen Time entitlement. The usage adapter reports no permission and the SDK emits those features at zero confidence. Motion and mobility still work.

### Android

`AndroidManifest.xml`:

```xml
<uses-permission android:name="android.permission.ACCESS_COARSE_LOCATION" />
<uses-permission android:name="android.permission.ACCESS_BACKGROUND_LOCATION" />
<uses-permission android:name="android.permission.ACTIVITY_RECOGNITION" />
<uses-permission android:name="android.permission.PACKAGE_USAGE_STATS"
                 tools:ignore="ProtectedPermissions" />
<uses-permission android:name="android.permission.RECEIVE_BOOT_COMPLETED" />
```

`PACKAGE_USAGE_STATS` is a special access grant. Send the user to `Settings.ACTION_USAGE_ACCESS_SETTINGS` and let them turn it on. Do not block enrollment on it.

Ask for background location as a second step, after the user has been running the app for a while. Asking on first launch is the fastest way to get denied.

## Start it

```ts
import Traycer, {
  createAsyncStorageAdapter,
  createBackgroundFetchScheduler,
  createDeviceInfoBatteryAdapter,
  createGeolocationAdapter,
  createNetInfoAdapter,
  createSensorsAccelerometerAdapter,
} from '@traycer/sdk';

await Traycer.start({
  // Issued by the clinic at enrollment. Not a name, an email, or a record
  // number. The SDK rejects values that look like identity.
  patientCode: 'TRC-2f81a9c4',

  // Short lived JWT from your backend, scoped to this device.
  deviceToken: await fetchDeviceToken(),

  serverPublicKey: {
    keyId: 'traycer-ingest-2026-01',
    jwk: {
      kty: 'RSA',
      n: '...',
      e: 'AQAB',
      alg: 'RSA-OAEP-256',
      ext: true,
    },
  },

  upload: {
    endpoint: 'https://ingest.your-clinic.example/v1/batches',
  },

  adapters: {
    storage: createAsyncStorageAdapter(),
    scheduler: createBackgroundFetchScheduler(),
    accelerometer: createSensorsAccelerometerAdapter(),
    location: createGeolocationAdapter(),
    battery: createDeviceInfoBatteryAdapter(),
    network: createNetInfoAdapter(),
  },

  logLevel: __DEV__ ? 'debug' : 'warn',
});
```

`start` is idempotent. Calling it again while running is a no op, which matters because fast refresh remounts your root component.

## Android headless task

Background fetch fires when the app is terminated. Register the headless handler in `index.js` or the collection stops the moment the user swipes the app away.

```js
import BackgroundFetch from 'react-native-background-fetch';
import Traycer from '@traycer/sdk';

const headlessTask = async (event) => {
  await Traycer.start(traycerConfig);
  await Traycer.collectNow();
  BackgroundFetch.finish(event.taskId);
};

BackgroundFetch.registerHeadlessTask(headlessTask);
```

## The rest of the API

```ts
Traycer.pause();                  // Stop collecting, keep the OS schedule
Traycer.resume();
await Traycer.stop();             // Cancel the schedule, keep queued data
await Traycer.collectNow();       // Force one window
await Traycer.flushNow();         // Force an upload attempt
await Traycer.purgeLocalData();   // Consent withdrawal
Traycer.updateDeviceToken(jwt);   // After a token refresh
const status = await Traycer.getStatus();
```

`getStatus` returns the state, the active sensor domains, queue depth, and the last window and upload timestamps. Show it in your settings screen. Patients who can see what is being collected keep the SDK enabled far longer than patients who cannot.

## Battery

The accelerometer is the expensive part, so it is sampled in one short burst per window rather than streamed. Defaults are a 60 second burst at 25 hertz inside a 60 minute window, which is about 1.7 percent duty cycle. Location uses balanced accuracy, which stays on network and sensor fusion rather than waking the GPS chip.

Battery cost varies enough across devices and OS versions that you should measure it on your own fleet before rollout rather than trusting a number in a README. What the SDK gives you to control it: `collection.motionBurstSeconds` and `collection.windowMinutes` set the duty cycle directly, and `collection.batteryFloor` skips motion bursts below a charge level when the device is not charging. The default floor is 0.15.

`resolveCollectionConfig` refuses a burst longer than 20 percent of the window. That limit exists because a continuously streaming accelerometer is the fastest way to get your app one star reviews about battery drain.

## Upload behavior

Windows accumulate in a persisted buffer. When the buffer reaches `batchSize` it is sealed into an immutable batch with a random UUID. At flush time the batch is encrypted and posted.

- Content key: fresh AES-256-GCM per batch, wrapped with your RSA-OAEP-256 public key. The device holds no decryption secret, so a stolen phone yields nothing readable from the queue.
- The batch id is authenticated as additional data and sent in the clear so your server can deduplicate a retried upload before decrypting anything.
- Retries use exponential backoff with full jitter, from one minute up to six hours, giving up after `maxAttempts`.
- A `409` response counts as delivered. It means your server already committed this batch and the response was lost.
- `401` and `403` defer rather than drop, so no data is lost while you refresh the token.
- Defaults to unmetered connections only.

Your ingest endpoint receives:

```json
{
  "v": 1,
  "batchId": "b6f8e0f2-3a4c-4d5e-8f90-1a2b3c4d5e6f",
  "alg": "A256GCM",
  "keyAlg": "RSA-OAEP-256",
  "wrappedKey": "base64",
  "iv": "base64",
  "ciphertext": "base64",
  "keyId": "traycer-ingest-2026-01"
}
```

Decrypting gives you:

```json
{
  "v": 1,
  "patientCode": "TRC-2f81a9c4",
  "sdkVersion": "0.1.0",
  "batchId": "b6f8e0f2-3a4c-4d5e-8f90-1a2b3c4d5e6f",
  "createdAt": 1700000600000,
  "windows": [
    {
      "windowStart": 1700000000000,
      "windowMs": 3600000,
      "utcOffsetMinutes": 60,
      "samples": [
        { "feature": "activity_ratio", "value": 0.42, "confidence": 1 },
        { "feature": "mobility_radius_m", "value": 1834.2, "confidence": 0.75 }
      ]
    }
  ]
}
```

## Confidence, and why gaps are not zeros

Every sample carries a confidence from 0 to 1. It is the fraction of the window the sensor was actually readable.

This matters more than it looks. A patient whose phone sat on a charger overnight produces `activity_ratio` of 0. So does a patient who did not get out of bed. The first is a sensor gap at low confidence, the second is a real reading at full confidence, and treating them the same produces false alerts. Do not collapse confidence when you store these.

## Writing your own adapters

Every adapter is a small interface. If you already have a sensor pipeline, wire it in and skip the prebuilt ones.

```ts
import type { UsageAdapter } from '@traycer/sdk';

const myUsageAdapter: UsageAdapter = {
  async hasPermission() {
    return NativeUsage.hasAccess();
  },
  async getScreenEvents(sinceMs) {
    return NativeUsage.screenEvents(sinceMs);
  },
  async getAppSessions(sinceMs) {
    const raw = await NativeUsage.foregroundSessions(sinceMs);
    // Category, never a package name. This mapping is the privacy boundary
    // for the usage domain, so keep it here rather than passing packages in.
    return raw.map((session) => ({
      category: COMMUNICATION_PACKAGES.has(session.pkg) ? 'communication' : 'other',
      startedAt: session.start,
      endedAt: session.end,
    }));
  },
};
```

The full set is `StorageAdapter`, `AccelerometerAdapter`, `LocationAdapter`, `UsageAdapter`, `SchedulerAdapter`, `TransportAdapter`, `CryptoAdapter`, `BatteryAdapter`, and `NetworkAdapter`. Only storage and scheduler are required, plus at least one sensor.

## Testing without a device

The pure reducers are exported, so you can drive them from fixtures in a normal Node test run.

```ts
import { reduceMotion, reduceUsage, radiusOfGyration } from '@traycer/sdk';
```

The package's own suite runs in Node with no native modules and no emulator. `npm test` in `packages/sdk`.

## Consent

The SDK does not manage consent. That is your job and your regulator's. Two things worth building before you ship:

1. A screen that shows the feature list above, in the patient's language, before collection starts.
2. A control that calls `Traycer.stop()` and `Traycer.purgeLocalData()` and reaches your server to delete what was already uploaded. The device cannot prove which server records are its own, so the deletion request has to come from your backend.

Passive sensing without informed consent is surveillance. The technical controls in this package do not substitute for the conversation.

## License

MIT.

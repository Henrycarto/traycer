# SDK privacy model

This document is written for someone deciding whether to allow this SDK onto a
patient's phone. That is usually an information security reviewer, a data
protection officer, or an IRB. It states what leaves the device, what does
not, and where each claim is enforced in code.

## The claim

The SDK emits fifteen aggregated numbers per collection window and nothing
else. No coordinates, no content, no identifiers, no app inventory.

## What is collected

| Feature | Meaning | Source |
| --- | --- | --- |
| `activity_ratio` | Fraction of the motion burst above the movement threshold | Accelerometer |
| `movement_intensity` | 90th percentile magnitude above gravity | Accelerometer |
| `longest_sedentary_minutes` | Longest unbroken still period in the burst | Accelerometer |
| `activity_fragmentation` | Movement state transitions per minute | Accelerometer |
| `mobility_radius_m` | Radius of gyration of the window's position fixes | Coarse location |
| `location_entropy` | Shannon entropy over grid cells visited | Coarse location |
| `place_count` | Distinct grid cells visited | Coarse location |
| `primary_place_ratio` | Share of fixes at the patient's most frequented place | Coarse location |
| `screen_on_minutes` | Total screen on time | Screen events |
| `unlock_count` | Device unlocks | Screen events |
| `mean_session_minutes` | Mean length of a real session | Screen events |
| `usage_loop_index` | Unlocks per minute weighted by session brevity | Screen events |
| `night_usage_minutes` | Screen on time between 00:00 and 06:00 local | Screen events |
| `communication_session_count` | Foreground sessions in communication apps | Usage stats |
| `communication_minutes` | Time in communication apps | Usage stats |

Each value carries a confidence from 0 to 1: the fraction of the window during
which the sensor was actually readable.

## What is not collected

- **No coordinates.** Position fixes exist only inside `MobilitySensor.collect`.
  They are reduced to a radius and a distribution and go out of scope in the
  same function call. No coordinate is written to storage or to the queue.
- **No content.** The SDK does not read messages, notifications, call
  recipients, clipboard, or any text.
- **No contacts.** No access to the address book or to phone numbers.
- **No app inventory.** The usage adapter reports a category, never a package
  name. The mapping from package to category happens in the host app's native
  layer and the package name never crosses the SDK boundary.
- **No microphone.** Voice analysis is a separate service triggered by the
  patient by hand.
- **No device identifiers.** No advertising id, IMEI, MAC address, or serial.
- **No identity.** The patient code is a pseudonym issued by the clinic. The
  SDK rejects a value that looks like an email address, a person name, or a
  bare medical record number.

## Where the claims are enforced

Not in a policy. In four places in code.

### 1. A closed feature vocabulary

`src/types/index.ts` defines `BehavioralFeature` as a closed union and
`BEHAVIORAL_FEATURES` as the runtime array. A sensor module cannot invent a
feature name, and the ingest API validates against the same fixed list. One
list to read to know everything that can leave.

### 2. Whitelist rebuild

`minimizeWindow` in `src/privacy/data_minimizer.ts` does not filter the
payload. It rebuilds it, keeping only known features, clamping each value to a
per feature ceiling, and turning non finite readings into zero at zero
confidence. Anything a sensor attached to the window object is discarded
because it is never copied across.

### 3. Identifier scan

`assertNoIdentifiers` walks the rebuilt payload depth first and throws on:

- Any key matching a forbidden identifier name, normalised for case and
  separators, so `device_ID`, `deviceId` and `DEVICE-ID` all fail.
- Any string containing an email address or a fifteen digit device identifier.
- Any string with whitespace that also matches a phone number shape.
- Any non finite number.
- Any string longer than 256 characters, which is never a feature name.

It throws rather than redacting. A silent redaction hides the fact that
something upstream is putting identity into the pipeline, and that is a bug
worth surfacing during integration rather than after deployment.

### 4. Patient code validation at start

`assertPseudonymousPatientCode` runs inside `Traycer.start` and refuses to
begin collection with an identified code. Clinics do reach for a name or an
email when wiring up an integration, and catching it at start beats catching
it in an audit six months later.

The test suite exercises all four. See
`packages/sdk/src/__tests__/data_minimizer.test.ts`.

## Place identity without coordinates

`primary_place_ratio` needs to know whether the patient is at their usual
place, which means places have to persist across windows. Storing coordinates
to do that would give back everything the design avoids.

Instead, each fix is quantised to a roughly 110 metre grid cell, combined with
a device local random salt, and hashed. Only the hash and a decayed visit
weight are stored. A dump of application storage contains no readable
coordinate.

To be exact about the guarantee: an attacker holding both the device salt and
the hash table could enumerate a grid and recover approximate cells. This is
data minimisation rather than a cryptographic boundary. The boundary is the OS
application sandbox. What the design does buy unconditionally is that the
platform operator never receives a coordinate at all, in any form, at any
point.

The salt is never uploaded. Regenerating it resets place identity and shows up
as a one window entropy discontinuity.

## Transport

Batches are encrypted on device before upload.

- A fresh AES-256-GCM content key per batch.
- The content key is wrapped with the clinic's RSA-OAEP-256 public key.
- The batch id is authenticated as additional data and sent in the clear, so
  the server can deduplicate a retried upload before decrypting anything.
- The device holds no decryption secret. A stolen phone yields nothing
  readable from the persisted queue.

The SDK refuses a server public key under 2048 bits and refuses a non HTTPS
endpoint outside localhost.

## Retention on device

The queue holds at most `maxQueuedBatches` batches, defaulting to 64. Over
capacity, the oldest are discarded: a device that far behind is offline enough
that the old windows have lost their clinical value, and recent behavior is
what the scoring engine needs.

`Traycer.purgeLocalData()` deletes every queued window. It does not and cannot
delete server side records, because the device cannot prove which server
records belong to it. Consent withdrawal has to reach the platform through the
host app's backend.

## Permissions the host app must handle

The SDK never prompts. The host app owns every permission dialogue, which is
correct: the app knows its own onboarding and its own language, and a library
that raises a background location prompt on import would be a defect.

On iOS, screen usage statistics require a Screen Time entitlement most apps do
not have. The usage adapter reports no permission and those five features are
emitted at zero confidence. Motion and mobility continue to work.

## Consent

The SDK does not manage consent and does not pretend to. It provides:

- A documented, short, readable list of what is collected.
- `getStatus()` so the host app can show the patient what is running, when the
  last window was collected, and how much is queued.
- `pause()`, `stop()` and `purgeLocalData()`.

Passive sensing without informed consent is surveillance. Everything in this
document describes technical controls, and technical controls do not
substitute for the conversation between a clinician and a patient about what
is being measured and why.

## For a reviewer in a hurry

Read these three files in order. They are the whole privacy story.

1. `packages/sdk/src/types/index.ts`, the closed vocabulary
2. `packages/sdk/src/privacy/data_minimizer.ts`, the enforcement
3. `packages/sdk/src/sensors/mobility.ts`, the hardest case

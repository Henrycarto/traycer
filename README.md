# Traycer

Psychiatric relapse follows a detectable behavioral pattern up to three weeks
before clinical deterioration. Traycer reads it from the phone already in your
patient's pocket.

No journaling, no mood scales, no check-ins. The phone's own sensors produce
fifteen aggregated numbers per hour. Those numbers are compared against what
that specific patient normally does, and when the difference holds, a clinician
is told.

## Why passive

Self report is where relapse detection usually goes wrong. Adherence to mood
diaries collapses exactly when a patient is deteriorating, which is when the
data matters most. Insight into one's own state is itself an early casualty of
both depressive and manic episodes. The measurement most likely to fail is the
one that requires the patient to notice something is wrong.

Passive sensing has the opposite property. It works best in the patients who
would report least.

## Three components

**Traycer SDK** is a React Native background sensing library, published as
`@traycer/sdk`. Third party mental health apps embed it. It collects
accelerometer patterns, mobility radius, phone usage loops, screen on duration
and social interaction proxies, aggregates them on device, and uploads them
encrypted in batches. It reads no personal content of any kind. This is the
open source layer.

**Traycer Voice** is an openSMILE microservice. A patient can optionally record
a short sample; the service extracts vocal energy, speech rate, pitch variance
and pause frequency, returns an acoustic vector, and destroys the audio. It
never transcribes, and a content guard in the pipeline enforces that rather
than promising it.

**Traycer Pulse** is the clinician dashboard. Trajectory scores across a patient
panel, anomaly alerts ranked by severity, and push notification when a
patient's behavioral signature moves away from their own baseline and stays
there.

## Repository layout

```
apps/web/           Traycer Pulse dashboard, Next.js 14
packages/sdk/       @traycer/sdk, the published sensing library
services/voice/     openSMILE acoustic extraction, FastAPI
services/scoring/   Personal baseline and deviation scoring, FastAPI
services/pulse-api/ Alert classification and clinician notification, FastAPI
infra/              docker-compose, Terraform, SQL schemas
docs/               Architecture and methodology
```

## Running it

```sh
cp .env.example .env
docker compose -f infra/docker-compose.yml up --build
```

That brings up PostgreSQL, TimescaleDB, Redis, all three services and the
dashboard on port 3000. The voice image compiles openSMILE from source on the
first build and takes several minutes.

The dashboard alone, against generated data:

```sh
npm install
npm run web:dev
```

The SDK:

```sh
npm run sdk:test
npm run sdk:build
```

## Stack

Next.js 14 with the App Router, TypeScript, Tailwind and Radix for the
dashboard. React Native bare workflow for the SDK. FastAPI and async Python
throughout the services, with numpy, scipy, pandas and scikit-learn for
scoring. PostgreSQL for relational data, TimescaleDB for the behavioral metric
stream, Redis for the alert queue. Firebase Cloud Messaging for clinician push.
SMART on FHIR OAuth 2.0 for clinician authentication, device scoped JWTs for
SDK upload. AWS ECS Fargate, RDS, ElastiCache and S3, described in Terraform.

## Public and private

The sensing layer is open source. The scoring model is not.

Committed:

- The full SDK implementation, including the encrypted upload queue and the
  data minimizer
- The openSMILE integration and the acoustic feature pipeline
- The baseline builder and the anomaly detector architecture
- The alert classification and threshold system
- Infrastructure and container configuration

Withheld, and marked in place:

- `services/scoring/app/engine/lstm_predictor.py`, the relapse trajectory model
  and its weights. The interface is public and integrated; the model is a
  private package.
- The anomaly scoring coefficients that define a clinically significant
  deviation. The architecture is public, the numbers are not. Without them the
  service runs on a neutral placeholder set and says so in every response.

Reasoning in `docs/scoring-pipeline.md`.

## Build status

Stated honestly, because a portfolio that overstates readiness is worth less
than one that does not.

### Ready

- **Traycer SDK.** Complete and publishable. Sensors, batched encrypted
  upload, retry with backoff, queue persistence across process death, and the
  data minimizer are implemented and covered by 49 tests that run in Node with
  no emulator.
- **Scoring architecture.** Personal baseline construction and deviation
  scoring are implemented and tested against synthetic patients with a known
  prodromal signature.
- **Alert classification.** Confirmation windows, cooldown, per patient
  thresholds, muted signals and quiet hours are implemented and tested.
- **Dashboard.** TrajectoryChart, RiskScoreBadge, AlertFeed, PatientRiskPanel,
  VoiceBiomarkerPanel and the SDK status views are built. The alert feed and
  alert acknowledgement read from the live pulse API. The panel, trajectory,
  telemetry and voice views currently render generated data with a visible
  banner, because the read endpoints behind them are not implemented yet. See
  the gaps below.
- **Infrastructure.** docker-compose runs the whole stack locally. Terraform
  describes the AWS deployment.

### Demo ready

- **Traycer Voice.** openSMILE integration, the acoustic vector and the content
  guard all work and are tested adversarially. The acoustic thresholds are not
  clinically validated, and sample quality gating is heuristic.
- **SMART on FHIR authentication.** The full authorization code flow with PKCE
  is implemented, including discovery and JWKS verification. It has been
  exercised against the SMART Health IT sandbox, not against a production EHR.

### Not ready

- **Trajectory prediction.** The LSTM interface is finished and integrated. The
  model is untrained. Activating it requires a labelled clinical corpus with
  confirmed relapse dates, and that corpus does not exist yet. Until it does the
  service reports `prediction_available: false` and nothing downstream invents a
  number.
- **Clinical validation.** Nothing here has been validated against confirmed
  relapse outcomes. The behavioral features and their directions come from the
  digital phenotyping literature. The thresholds that would turn them into a
  clinical instrument do not exist yet.
- **Ingest service.** The SDK uploads to a documented endpoint contract. The
  service that terminates it is not in this repository. This is the one thing
  standing between the repo and an end to end system.
- **Four dashboard read endpoints.** The panel list, per patient trajectory,
  telemetry and voice endpoints are called by the dashboard and are not
  implemented server side. The frontend falls back to generated data and says
  so on screen. Response shapes are specified in `apps/web/lib/types.ts`.
- **Alert classifier state.** Confirmation counters live in process memory and
  the container runs two workers. This holds today and stops holding the moment
  the service scales horizontally. It belongs in Redis, which is already
  configured.

`docs/handover.md` covers all of this in the order it should be closed.

## A note on what this is

Traycer is a monitoring and research tool. It does not diagnose and it does not
replace clinical judgement. Passive sensing without informed consent is
surveillance, and every technical control in the SDK is there to support a
conversation between a clinician and a patient rather than to replace one.

## License

MIT.

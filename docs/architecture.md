# Architecture

## The shape of the system

A phone collects behavioral features and uploads them encrypted. A scoring
service compares those features to what that patient normally does. When the
difference holds across consecutive windows, an alert reaches a clinician.

Four moving parts.

```
Patient phone                 Platform                      Clinician
-------------                 --------                      ---------

@traycer/sdk
  motion burst      \
  location fixes     >  15 features/hour
  usage events      /          |
                               v
                        ingest endpoint
                               |
                     decrypt, validate, store
                               |
                    +----------+----------+
                    |                     |
              TimescaleDB            PostgreSQL
         behavioral samples       profiles, panels,
         trajectory scores        thresholds, alerts
                    |                     |
                    v                     |
             scoring service              |
        baseline builder                  |
        anomaly detector                  |
        trajectory model (private)        |
                    |                     |
                    v                     v
                      pulse-api
              alert classifier, thresholds
                          |
              +-----------+-----------+
              |                       |
        Firebase push            Traycer Pulse
        clinician device         Next.js dashboard

  Traycer Voice sits alongside: the patient records an optional
  short sample, openSMILE extracts an acoustic vector, the audio
  is destroyed, and the vector joins the same pipeline.
```

## Why the split is where it is

### The SDK is a separate package, not part of an app

The sensing layer has to run inside somebody else's application. A digital
therapeutics company already has a React Native app on a patient's phone;
asking that patient to install a second app to be monitored defeats the point
of passive sensing. So the sensing layer ships as an npm package with a small
surface, injectable adapters, and no opinion about the host app's navigation,
state management or backend.

That constraint is also why the SDK holds no business logic. It produces
fifteen numbers per window and knows nothing about risk, baselines or alerts.
A host app can adopt it, read its own data, and never speak to Traycer at all.

### Scoring is a service, not a library

The baseline builder needs thirty days of history and the trajectory model
needs a trained artefact measured in tens of megabytes. Neither belongs on a
phone. Running scoring server side also means the model can be retrained and
redeployed without shipping an app update to every patient, which matters when
the model is the part most likely to change.

### Alerting is separate from scoring

The scoring service answers a numerical question: how far is this patient from
their own baseline. The pulse API answers a clinical one: does this deserve a
clinician's attention right now. Those change for different reasons. A
clinician adjusting a threshold, muting a signal, or setting quiet hours
should not touch anything in the scoring path, and a model retrain should not
be able to change alerting behaviour as a side effect.

### Voice is its own service

Audio handling has a different risk profile from everything else in the
platform. It runs a subprocess over patient supplied bytes, and it is the one
place where a mistake produces something legally and ethically heavier than a
number. Isolating it means it can have its own container, its own read only
config mount, its own resource limits, and its own audit story.

## Data stores

| Store | Holds | Why |
| --- | --- | --- |
| PostgreSQL | Patient profiles, clinician panels, thresholds, alert history, access log | Transactional, small, long retention for medical record rules |
| TimescaleDB | Behavioral samples, voice vectors, trajectory scores | Append heavy time series with compression and continuous aggregates |
| Redis | Alert queue, notification dedup state | Ephemeral and recoverable from Postgres |
| S3 | Encrypted batch landing area | Objects expire after 30 days; the durable copy is the hypertable |

Two Postgres instances rather than one. They have different retention rules,
different backup windows and different write patterns, and merging them would
force the stricter policy onto both.

## The request path, end to end

1. The SDK collects a window and appends it to a persisted buffer.
2. At `batchSize` windows the buffer seals into a batch with a random UUID.
3. At flush time the batch is encrypted with a fresh AES-256-GCM key, wrapped
   with the clinic's RSA public key, and posted with the device JWT.
4. Ingest verifies the token, deduplicates on batch id, decrypts, validates
   against the feature vocabulary, and writes to the hypertable.
5. A scheduled job scores each patient's most recent window against their
   baseline and writes a trajectory score.
6. Scores go to the pulse API, which applies the patient's thresholds, the
   confirmation requirement and the cooldown.
7. A surviving alert is stored, ranked into the feed, and pushed to the
   clinician's registered devices unless quiet hours apply.

## API conventions

Every response from every service uses one envelope.

```json
{
  "data": { },
  "error": null,
  "meta": { "request_id": "...", "service": "...", "version": "...", "timestamp": "..." }
}
```

Failures use the same shape with `data` null and `error` populated. A client
that has to branch between two response shapes will eventually get the branch
wrong, and this is a system where getting it wrong means an alert is not shown.

All three Python services are async throughout. The voice service in
particular would otherwise block a worker for the several seconds an
extraction takes.

## Authentication

Three kinds of caller, three verification paths.

**Devices** carry a short lived JWT signed by the enrollment service, scoped
to one patient code. The voice service checks the scope against the submitted
patient code, so a valid token from one device cannot submit for another
patient.

**Internal services** carry a platform signed token with a service audience.

**Clinicians** never get a Traycer password. They authenticate through their
hospital's SMART on FHIR authorization server using authorization code with
PKCE. Traycer verifies the resulting token against the issuer's published
JWKS, discovered from `.well-known/smart-configuration` rather than
configured. Access ends when the EHR account does.

The dashboard keeps the access token in an httpOnly cookie and performs every
FHIR read server side, so an XSS bug in the dashboard cannot exfiltrate a
token carrying patient read scope.

## What is not in this repository

Two things, both marked in place:

- `services/scoring/app/engine/lstm_predictor.py` is an interface with no
  model behind it. The trained weights live in a private package.
- The coefficients in `anomaly_detector.py` that define a clinically
  significant deviation. The architecture runs on a neutral placeholder set
  and reports that it is doing so, in the API response, in the health
  endpoint, and in the dashboard.

See `docs/scoring-pipeline.md` for the reasoning.

## Local development

```sh
cp .env.example .env
docker compose -f infra/docker-compose.yml up --build
```

That brings up both databases with their schemas applied, Redis, all three
services and the dashboard. The voice image compiles openSMILE from source on
first build and takes several minutes.

Without Docker, the dashboard runs standalone against generated data:

```sh
npm install
npm run web:dev
```

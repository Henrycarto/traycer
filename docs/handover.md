# Handover

For the engineer picking this up. Written to get you productive in an hour and
to stop you from rediscovering things the hard way.

Read `docs/architecture.md` for the shape of the system and
`docs/scoring-pipeline.md` for why the scoring works the way it does. This
document covers what those two do not: how to run it, what will bite you, and
what to build next.

## Get it running

Prerequisites: Node 20 or newer, Python 3.12, Docker.

```sh
git clone https://github.com/Henrycarto/traycer.git
cd traycer
cp .env.example .env
npm install
```

The dashboard on its own, against generated data:

```sh
npm run web:dev          # http://localhost:3000
```

The whole stack:

```sh
docker compose -f infra/docker-compose.yml up --build
```

First build takes ten to fifteen minutes because the voice image compiles
openSMILE from source. Subsequent builds hit the layer cache. If you only want
the other services while you work, `docker compose up postgres timescale redis
scoring pulse-api web`.

Tests:

```sh
npm run sdk:test                                    # 49 tests, no emulator needed
cd services/voice && python -m pytest -q            # 32
cd services/scoring && python -m pytest -q          # 44
cd services/pulse-api && python -m pytest -q        # 53
python scripts/check_copy.py                        # writing rules, also runs in CI
```

Each service has its own `requirements.txt`. A single virtualenv with all
three installed works fine locally; they do not conflict.

## Repo map

| Path | What it is |
| --- | --- |
| `packages/sdk/` | `@traycer/sdk`, the published sensing library. The most finished thing here. |
| `apps/web/` | Traycer Pulse dashboard, Next.js 14 App Router |
| `services/ingest/` | Terminates the SDK upload contract, the write path |
| `services/voice/` | openSMILE acoustic extraction |
| `services/scoring/` | Personal baseline and deviation scoring |
| `services/pulse-api/` | Alert classification, thresholds, clinician push |
| `infra/sql/` | Schemas, mounted by compose at first start |
| `infra/terraform/` | AWS deployment. Never applied. See gaps below. |
| `scripts/check_copy.py` | Writing rules, enforced in CI |

## Where the logic actually lives

If you are looking for the part that matters, it is one of these five files.

- `packages/sdk/src/privacy/data_minimizer.ts` is the privacy boundary. Every
  upload is rebuilt from a whitelist and then scanned. If you are evaluating
  the privacy claims, start here and nowhere else.
- `packages/sdk/src/collectors/upload_queue.ts` is the durability story:
  persisted buffer, sealed batches with stable ids, backoff with jitter,
  survives process death.
- `services/scoring/app/engine/baseline_builder.py` builds the personal
  baseline. Median and median absolute deviation, confidence weighted, with a
  scale floor and a 24 hour circadian profile.
- `services/scoring/app/engine/anomaly_detector.py` is the seven step scoring
  pipeline. The architecture is here; the coefficients are not.
- `services/pulse-api/app/engine/alert_classifier.py` decides what reaches a
  clinician. Four filters: confirmation, cooldown, muted signals, calibration.

On the frontend, `apps/web/components/pulse/TrajectoryChart.tsx` is the
signature component and the one to understand first.

## Invariants

These are not style preferences. Breaking one of them changes clinical
behaviour, usually silently.

**Confidence is not a weight you can drop.** Every sample carries a confidence
from 0 to 1. A patient whose phone sat on a charger overnight produces
`activity_ratio` of 0. So does a patient who did not get out of bed. The first
is a gap at low confidence, the second is a real reading at full confidence,
and collapsing them produces false alerts. This distinction is preserved from
the sensor through the queue, the schema, the baseline and the score.

**The feature vocabulary is defined twice and must stay in sync.** Once as a
TypeScript union in `packages/sdk/src/types/index.ts`, once as a Python enum in
`services/scoring/app/schemas.py`. `services/scoring/tests/test_feature_parity.py`
reads the TypeScript source and compares. If you add a feature, add it in both
places or CI fails. This is deliberate; a feature added on the device that the
scoring service does not know about would be dropped without a trace, and the
dashboard would still look fine.

**An uncalibrated score must never fire a clinical alert.** The alert
classifier checks `clinically_calibrated` and refuses. Do not add a bypass for
testing convenience. If you need alerts in a dev environment, inject a test
coefficient set the way `tests/test_anomaly_detector.py` does.

**An insufficient baseline produces no score, not a zero.** A number produced
against an unbuilt baseline looks identical to a real one on a dashboard and a
clinician has no way to tell. `RiskScoreBadge` renders `n/a` for a null score
rather than a number.

**Coordinates never leave `MobilitySensor.collect`.** Position fixes are
reduced to a radius and a distribution inside that function and go out of
scope before it returns. If you need place identity across windows, use the
salted grid cell hashes that are already there. Do not persist a coordinate.

**No transcript, enforced not promised.** The four checks in
`services/voice/app/engine/content_guard.py` are the reason the claim holds.
The tests for them are written as attacks. If you change the openSMILE config
or the argument vector, expect the guard to stop you, and treat that as the
guard working.

## The private boundary

Two things are not in this repository, both marked in place.

`services/scoring/app/engine/lstm_predictor.py` is an interface with no model.
When the private package is absent, `predict` returns None and the service
reports `prediction_available: false`. Nothing downstream invents a number, and
`tests/test_lstm_predictor.py` pins that.

The coefficients in `anomaly_detector.py` load from `traycer_coefficients`.
Absent it, a neutral placeholder set is used and every response says so. The
deploy workflow fails the release if a production deployment comes up on
placeholder coefficients, which is the failure mode most likely to put a
meaningless number in front of a clinician while looking entirely normal.

Both packages install from a private index at image build time via the
`PRIVATE_INDEX_URL` build argument. See `services/scoring/Dockerfile`.

## Gotchas found while building this

Real ones, in the order they cost time.

**React 18 is pinned by a root override, and it has to stay.** Recharts pulls
`react-smooth`, whose peer range allows React 19, so npm hoisted React 19 to
the workspace root while `apps/web` kept 18. The Next build then crashed inside
styled-jsx with `Cannot read properties of null (reading 'useContext')`, which
tells you nothing about the cause. The `overrides` block in the root
`package.json` pins both `react` and `react-dom` to 18. If you remove it or
upgrade Recharts, expect this back.

**The baseline window cutoff is exclusive.** An inclusive bound at start plus
fourteen days pulls in the first observation of day fifteen and reports
coverage the patient does not have. There is a test.

**`resource` is not importable on Windows.** `opensmile_client.py` guards the
import and passes `preexec_fn=None` when it is absent, so the test suite runs
on a Windows dev machine. The service itself targets Linux containers only, and
the child process resource limits are a real containment control, not
decoration.

**Pydantic reserves the `model_` prefix.** `ScoreResult.model_version` needs
`protected_namespaces=()` in its config. Without it you get a warning on every
import and eventually a rename you do not want, because `model_version` is part
of the API contract.

**`FcmClient.configured` returns true when an account is injected.** That is a
test seam so the delivery tests can run without a service account file on disk.
It is documented at the property.

**The writing rules are enforced in CI.** `scripts/check_copy.py` fails the
build on em dashes and on a list of filler words. Two judgment calls are baked
in: `unlock` is allowed as a domain term because `unlock_count` is a real
sensor feature, but the marketing constructions are not; the statistical sense
of the word for outlier resistance is banned along with the rest, which is why
the deviation field is called `deviation_z`.

## Gaps, in the order I would close them

**1. Four dashboard read endpoints do not exist server side.**
`apps/web/lib/api.ts` calls six endpoints. Two are implemented in the pulse
API: the alert feed and alert acknowledgement. Four are not:

- `GET /v1/patients` on the pulse API, the panel summary
- `GET /v1/patients/{code}/telemetry` on the pulse API, SDK state
- `GET /v1/patients/{code}/voice` on the pulse API, latest voice vector
- `GET /v1/patients/{code}/trajectory` on the scoring service, the 30 day series

The dashboard is not broken by this. `withFallback` catches the failure and
serves generated data, and `isDemoMode()` puts a visible banner on the page
saying the numbers are synthetic. That is the right behaviour for a portfolio
deployment and the wrong behaviour to leave in once real data exists. The
response shapes the frontend expects are in `apps/web/lib/types.ts`.

**2. The alert classifier holds state in process memory.** Confirmation
counters and cooldown timestamps live in a dict inside `AlertClassifier`, and
the container runs two uvicorn workers. Today that is survivable because the
scoring pipeline routes a given patient's windows through one call and the
counters rebuild from history on restart. It stops being survivable the moment
you scale the service horizontally or put a load balancer in front of more than
one task. Move it to Redis; the connection is already configured and unused.

**3. Terraform has never been applied.** It describes a VPC with three tiers,
two RDS instances, ElastiCache, ECS Fargate services behind one load balancer,
KMS, and secrets. It is written carefully but it has not been run once. Expect
to spend a day on `terraform validate` and a first `plan` before trusting it.
No state file exists yet, and the S3 backend it references does not exist
either.

**4. SMART on FHIR has only met the sandbox.** The authorization code flow with
PKCE, discovery and JWKS verification are complete and correct as far as the
SMART Health IT sandbox is concerned. Production EHRs vary in ways sandboxes do
not, particularly around the `aud` parameter, client authentication and launch
context. Budget real time for the first live integration.

**5. Nothing is clinically validated.** The behavioral features and the
directions they move in come from the digital phenotyping literature. The
thresholds that would turn them into a clinical instrument do not exist. The
scoring tests establish that the pipeline responds to the right shape of
change against synthetic patients, which is a weaker claim and is not presented
as more.

## Things I would not change without a reason

The chart border colour reads the most recent point rather than the maximum
across the window. Showing red for a threshold crossing three weeks ago would
train clinicians to ignore the colour, and the colour is the whole message.

The panel sparkline is a hand rolled polyline rather than a chart component.
Forty Recharts instances on one page is measurable work for the browser, and
the sparkline needs to be a shape rather than a chart.

The alert feed sorts by rank, not by time. A critical alert from this morning
outranks an elevated one from ten minutes ago, and unacknowledged alerts climb
as they age so nothing sits unread for a week.

Quiet hours suppress the push, never the alert. The row stays in the feed with
its real timestamp so a clinician arriving in the morning sees what happened
overnight.

## Contact points in the code

Every non obvious decision has a comment explaining why, not what. If you find
something that looks arbitrary and has no comment, it probably is arbitrary and
you should change it.

# Traycer

Passive behavioral biomarkers for early detection of psychiatric relapse.

Repository: https://github.com/Henrycarto/traycer

---

## 1. Product thesis

Psychiatric relapse is not sudden. Weeks before a patient meets criteria for a
depressive or manic episode, their behavior has already changed: they leave the
house less, their sleep shifts, they contact fewer people, their movement slows
or fragments. Clinicians know this and have known it for decades. What they
lack is a way to observe it. A patient is seen once a month for twenty minutes
and asked to recall a period they are least equipped to recall accurately,
because impaired insight is itself an early feature of both poles of the
illness. The gap between when behavior changes and when a clinician can see it
is where hospital admissions come from. Passive sensing closes that gap without
asking anything of the patient, because the phone is already recording the
relevant signal as a byproduct of being carried: how far someone moved, how
their sleep and screen patterns shifted, how often they reached for
communication. Active reporting fails precisely where it is needed most.
Adherence to mood diaries and check-in prompts collapses during deterioration,
so the measurement most likely to be missing is the one taken during the
episode you were trying to catch. Passive sensing has the opposite failure
profile. It works best in the patients who would report least.

## 2. Architecture

Three components, each existing because it solves a problem the others cannot.

**Traycer SDK** is the sensing layer, published on npm as `@traycer/sdk`. It is
a React Native library that a third party mental health app embeds. Once a
window, it takes a short accelerometer burst, a handful of coarse position
fixes and a read of screen and app session events, reduces all of it on device
to fifteen aggregated numbers, and holds those numbers in a persisted queue
until it can upload them encrypted. It reads no message content, no contact
list, no app names and no coordinates. It exists as a separate package because
the patient already has a mental health app on their phone, and asking them to
install a second one to be monitored defeats the purpose of passive sensing.

**Traycer Voice** is an openSMILE microservice. A patient can optionally record
a short sample reading a fixed prompt. The service extracts acoustic
descriptors, vocal energy, speech rate, pitch variance, pause frequency and
voice quality, returns a numeric vector, and destroys the audio before the
response is written. It never transcribes. It exists as a separate service
because audio has a different risk profile from every other input in the
platform, and isolating it means it gets its own container, its own resource
limits and its own audit story.

**Traycer Pulse** is the clinician dashboard. It shows a patient panel where
each row is one patient with a risk score, a seven day sparkline and their SDK
status, so a psychiatrist managing forty patients can triage the whole panel in
one scroll. It shows a thirty day trajectory chart per patient, an alert feed
ranked by what needs attention next, and it pushes a notification when a
patient's behavioral signature moves away from their own baseline and stays
there.

The SDK is open source and the scoring model is not, and that split is
deliberate rather than incidental. The sensing layer has no defensible moat.
Any competent mobile engineer can read an accelerometer and compute a radius of
gyration; what is hard is doing it without destroying battery life or privacy,
and that work is worth more as a standard other people adopt than as a secret.
The scoring model is the opposite. It encodes labelled relapse episodes, it is
reproducible only by someone who has run a clinical study of comparable size,
and it is the part a buyer is actually paying for. Publishing the sensing layer
lowers the cost of collecting the signal for everyone. Retaining the model
means the signal remains worth something to us.

## 3. Key engineering decisions

**openSMILE in our own container rather than a cloud speech API.** Cloud speech
services are transcription engines. Sending psychiatric patient audio to one to
obtain prosodic features means the transcript exists, on someone else's
infrastructure, whether or not it is returned. That is not a configuration
choice defensible to an IRB. openSMILE runs locally, has no ASR component, and
is the reference implementation the acoustic literature is built on, so values
stay comparable to published work. The claim that nothing is transcribed is
enforced by a content guard with four checks: the extractor config is inspected
and refused if it references any component capable of writing audio or symbolic
output, the subprocess argument vector is checked against a deny list, every
returned field must parse as a finite number, and the audio file is overwritten
and unlinked with removal verified rather than assumed. A transcript cannot
survive those checks by any route. The tests are written adversarially, as
attempts to defeat each one.

**TimescaleDB for behavioral metrics, PostgreSQL for everything else.** These
two stores have different shapes. The relational data is small, transactional
and kept under medical record retention rules. The behavioral stream is append
heavy and large: a forty patient panel at hourly windows across fifteen
features is roughly five million rows a year, and the scoring engine reads a
thirty day slice per patient on every panel refresh. Timescale gives
hypertables, continuous aggregates for the daily rollup the trajectory chart
reads, native compression after thirty days and a retention policy at two
years. Running both on one instance would force the stricter backup schedule
and the longer retention onto data that does not need either.

**Batched encrypted upload in the SDK.** Three constraints shaped this. The
device is often offline. Background execution arrives in short unpredictable
slices. And the patient in relapse is exactly the patient most likely to leave
their phone off the charger. So windows accumulate in a persisted buffer, a
full buffer seals into an immutable batch with a stable UUID, and the batch is
encrypted at send time with a fresh AES-256-GCM key wrapped under the clinic's
RSA public key. The device holds no decryption secret, so a stolen phone yields
nothing readable from the queue on disk. Retries use exponential backoff with
full jitter, because a clinic that onboards a cohort on the same afternoon
would otherwise produce a synchronised retry storm on every outage. The server
deduplicates on batch id, so a retry after a lost response is harmless.
Everything survives process death, which is tested by constructing a second
queue over the same storage and confirming the batch id is unchanged.

**Personal baseline rather than population baseline.** This is the most
consequential decision in the system. A shift worker who sleeps at 09:00, a
retiree who leaves the house twice a week and a graduate student who walks
eleven kilometres a day have nothing in common on a population distribution.
All three sit in some tail. All three are unremarkable against their own
fourteen day baseline, and all three produce a clear signal when their own
pattern breaks. A population model spends its capacity learning that people
differ, which takes tens of thousands of patients before it learns anything
clinically useful. A personal model spends its capacity learning that this
person changed, which takes two weeks of that person. The commercial
consequence is that the platform works on a panel of forty, which means a
single clinic is a viable first customer rather than a pilot that has to wait
for scale. The cost is a fourteen day cold start during which the platform
observes but cannot alert, and that is stated to clinicians in the interface
rather than concealed behind a provisional number. The statistics are chosen to
match: median and median absolute deviation rather than mean and standard
deviation, because one week away from home would otherwise inflate the scale
estimate enough to mask a genuine change for the following month.

**The LSTM predictor is a marked stub.** The trajectory model is not in the
public repository, and its absence is visible rather than disguised. The
interface is complete and integrated, the input and output contracts are
documented, and the failure behaviour is pinned by tests: when the private
package is absent, `predict` returns None and the service reports
`prediction_available: false`. Nothing downstream fabricates a number to fill
the gap. The same treatment applies to the anomaly scoring coefficients that
define what counts as a clinically significant deviation. The architecture is
public and runnable; the numbers load from a private package, and without it
the service scores on a neutral placeholder set and says so in the API
response, in the health endpoint and in the alert classifier, which refuses to
fire a clinical alert on an uncalibrated score at all. The IP protection and
the honesty are the same mechanism.

## 4. What the TrajectoryChart communicates

The chart is thirty days of one patient's behavioral score, a dashed horizontal
line at their own baseline, and a shaded band starting where deviation from
that baseline stops being ordinary week to week variation. When the score line
enters the band, the chart border turns amber. When it crosses the alert
threshold, the border turns red.

To a psychiatrist, the border colour is the entire message. Scrolling a panel
of forty patients, they are not reading axis values. They are looking for the
one chart that changed colour, and that colour is a clinical statement: this
patient's own pattern has moved, and it has stayed moved. Everything else on
the chart supports the reading rather than competing with it. There is no
gradient fill, no dot on every day, no animation. The chart is boring until it
is not, which is the correct behaviour for something checked the way vitals are
checked.

What the chart tells a clinician that a number cannot is the shape and the
duration of the change. A single elevated score is a bad night. A line that has
been climbing for nine days and entered the band on day twenty two is a
trajectory, and the difference is visible in one glance without any copy.

The interval is the clinical argument for the product. At the point a patient
presents with a recognisable episode, the intervention options are narrow:
crisis contact, medication change under time pressure, sometimes admission. At
three weeks earlier, when the only observable is that they have stopped leaving
the house and their phone use has moved into the night, the options are an
earlier appointment, a medication review and a conversation with the family.
The interventions available at three weeks are cheap, low risk and outpatient.
The interventions available at zero weeks are expensive, higher risk and often
inpatient. Moving detection earlier does not make the treatment better. It
changes which treatments are still on the table.

## 5. The SDK as distribution

The sensing layer ships as a permissively licensed npm package that a third
party mental health application embeds, so the patient installs nothing new and
consents once, inside a product they already use. The scoring service is what is
retained: features without the baseline builder, the calibrated coefficients and
the trained model are just numbers. The commercial detail of that arrangement is
not published here.

## 6. Public and private split

The repository is public in full apart from two things, both marked in place
with the reasoning attached.

Public and committed: the entire SDK implementation, including the sensor
reducers, the encrypted upload queue and the data minimizer; the openSMILE
integration and the acoustic feature pipeline including the content guard; the
baseline builder and the full anomaly detector architecture; the alert
classification, threshold and notification system; the dashboard; and the
Docker and Terraform configuration.

Private: the trained relapse trajectory model and its weights, and the anomaly
scoring coefficients that define a clinically significant deviation.

The reasoning for the model is both commercial and ethical. Commercially it is
the asset, reproducible only from a labelled clinical corpus that took a study
to assemble. Ethically, published weights trained on a psychiatric cohort of
this size are a re-identification surface, and membership inference against
sequence models trained on small clinical cohorts is a documented result rather
than a hypothetical concern. Releasing them would be a defensible commercial
mistake and an indefensible one toward the patients whose episodes are in the
training set.

The reasoning for the coefficients is narrower. The architecture around them is
ordinary engineering that any competent team would write similarly. The
specific weights, the direction asymmetries and the threshold parameters are
the clinical result. Keeping the architecture public and the numbers private
means the pipeline stays auditable by a reviewer who needs to understand what
the system does, while the part that took clinical data to produce stays with
the people who produced it.

The split also keeps the repository honest. Every stub reports itself at
runtime rather than failing silently or, worse, returning a plausible number.
That property was built for IP reasons and turned out to matter more for safety
reasons: a scoring service that quietly fell back to uncalibrated coefficients
while looking entirely normal is exactly the failure mode that puts a wrong
number in front of a clinician, so the deployment pipeline fails the release if
the calibrated set did not load.

## 7. Build status

Stated as it is, because a case file that overstates readiness is worth less
than one that does not.

**Production ready and publishable.** The Traycer SDK. Sensor reducers, batched
encrypted upload with retry and backoff, queue persistence across process
death, and the data minimizer are complete and covered by 49 tests that run in
Node with no emulator and no native modules. The package builds clean under
TypeScript strict mode, the publishable tarball contents are asserted in CI,
and the README is written for a third party engineer integrating it rather than
for us. This can be published to npm as it stands.

**Complete and tested, pending clinical calibration.** The scoring
architecture. Personal baseline construction and deviation scoring are
implemented and tested against synthetic patients carrying a known prodromal
signature, with assertions that the score rises monotonically with the
intensity of the change and that each deviation records the correct direction.
That is a weaker claim than clinical validation and is not presented as more.
The alert classifier, per patient thresholds, confirmation windows, cooldown
and quiet hours are complete and tested. The dashboard is built and renders
against either the live API or generated data, and says which.

**Demo ready.** Traycer Voice. The openSMILE integration, the acoustic vector
construction and the content guard all work and are tested adversarially. What
is missing is clinical validation of the acoustic thresholds and a proper
handling of prompt and recording condition variance. It is a working
demonstration of the extraction pipeline, not a clinical instrument. The SMART
on FHIR authentication is a complete authorization code flow with PKCE,
discovery and JWKS verification, exercised against the SMART Health IT sandbox
rather than a production EHR.

**Requires clinical data to activate.** The LSTM trajectory predictor. The
interface is finished and wired into the scoring service. The model is
untrained, and training it requires a labelled corpus with confirmed relapse
dates, which does not exist yet. That corpus is the next real milestone, and it
is a clinical partnership problem rather than an engineering one. The
engineering is waiting for it, not the other way around.

**Not built.** The ingest service that terminates the SDK upload contract is
not in this repository. The contract is documented and the client half is
tested, but the server half is deployment specific.

Nothing in this system has been validated against confirmed relapse outcomes.
The behavioral features and the directions they move in come from the digital
phenotyping literature. The thresholds that would turn them into a clinical
instrument do not exist yet, and no part of the interface implies otherwise.

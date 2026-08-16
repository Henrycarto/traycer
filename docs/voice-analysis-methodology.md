# Voice analysis methodology

## What the service does

A patient records a short sample, reading a fixed prompt. The service extracts
acoustic descriptors, returns a vector, and destroys the audio. It never
produces a transcript. It never stores audio past a single request.

Voice is optional and patient initiated. An absent sample is not a gap in
monitoring, because the behavioral pipeline runs independently.

## Why acoustic and not linguistic

Two reasons, one clinical and one about trust.

The clinical one: acoustic features of speech change in psychiatric illness in
ways that are measurable and that precede what a patient will report. Reduced
loudness and reduced pitch variance are among the oldest described features of
psychomotor retardation. Slowed speech rate and lengthened pauses track
depressive severity. Manic speech moves in the opposite direction on the same
parameters. None of that requires knowing a single word.

The one about trust: a psychiatric monitoring tool that transcribes patient
speech will not be adopted, and should not be. A patient who suspects their
words are being read will change what they say, which destroys the measurement
and damages the relationship the tool exists to support. Acoustic only is a
better product and a smaller liability at the same time.

## Why openSMILE rather than a cloud speech API

A cloud speech API is a transcription engine. Sending psychiatric patient
audio to a third party ASR service to obtain prosodic features means the
transcript exists, on someone else's infrastructure, whether or not it is
returned. That is not a configuration decision that can be defended to an IRB.

openSMILE runs in our own container, produces numeric descriptors only, and
has no ASR component at all. It is also the reference implementation the
acoustic literature is built on, which keeps values comparable to published
work rather than to a vendor's undocumented pipeline.

The feature set is eGeMAPSv02, the extended Geneva minimalistic acoustic
parameter set. It was designed for exactly this purpose: a small, theory
driven set of parameters with documented relationships to affective and
clinical state, chosen over the thousands of brute force features that older
challenge sets used. A small parameter set also matters for a model trained on
hundreds rather than millions of samples.

## The vector

Thirteen descriptors, reduced from the 88 functionals eGeMAPSv02 emits.

| Feature | Unit | Clinical relevance |
| --- | --- | --- |
| `vocal_energy` | loudness | Falls in depression, one of the oldest described features of retardation |
| `energy_variability` | loudness sd | Flattens with affect |
| `pitch_mean_semitones` | semitones | Baseline pitch, mostly a covariate |
| `pitch_variance` | semitones sd | The monotone measure; narrows in depression, widens in mania |
| `speech_rate` | syllables per second | Slows with retardation |
| `articulation_rate` | voiced segments per second | Separates slowed articulation from pausing |
| `pause_frequency` | pauses per minute | Rises with slowed thought |
| `mean_pause_seconds` | seconds | The acoustic surface of latency |
| `voiced_ratio` | ratio | Coverage measure, also a quality check |
| `jitter` | ratio | Voice quality, moves with autonomic arousal |
| `shimmer` | ratio | Voice quality |
| `harmonicity` | dB | Harmonics to noise ratio, doubles as a recording quality check |
| `spectral_slope` | dB per kHz | Vocal effort |

Speech rate and articulation rate are kept separate on purpose. Depression
tends to affect both. Anxiety tends to affect pausing more than articulation.
Collapsing them into one rate loses that distinction.

`pause_frequency` is derived rather than read directly, because eGeMAPS reports
mean unvoiced segment length but not a pause count. Unvoiced segments shorter
than 150 milliseconds are excluded as stop consonants rather than pauses,
which is what keeps the feature tracking slowed thought instead of tracking
phonetics.

The mapping from eGeMAPS parameter names to Traycer feature names lives in one
place, `engine/feature_extractor.py`. When the extractor config changes, that
is the only file that moves.

## The content guard

The claim that no transcript is produced is worth exactly as much as the
mechanism enforcing it, so the mechanism is code and it is tested
adversarially. `engine/content_guard.py`, four checks in order:

1. **Config inspection.** The openSMILE configuration is read before every
   extraction and refused if it references any component capable of writing
   audio or symbolic output: `cWaveSink`, `cTranscriptSink`, `cAsrSink` and
   others. The process never starts. This runs per extraction rather than once
   at startup because configs are mounted and can change without a restart.
2. **Argument inspection.** The subprocess argument vector is checked against
   a deny list of flags, and any audio path outside the input flag is refused.
   A future contributor cannot quietly add an audio output.
3. **Output filtering.** Every field the extractor returns must parse as a
   finite number, and any field whose name resembles symbolic output is
   rejected outright. A transcript is text. It cannot survive this check by
   either route.
4. **Destruction.** The audio file is overwritten and unlinked in a `finally`
   block on every path including the failure paths, and removal is verified
   rather than assumed. A silent unlink failure on a read only mount would
   otherwise leave patient audio on disk while the logs claimed otherwise.

There is no degraded mode that keeps the audio for inspection. That mode is
how audio ends up on a disk that someone later subpoenas.

Tests in `services/voice/tests/test_content_guard.py` are written as
adversarial cases: a config with an audio sink, an argument vector with
`-wavout`, a field named `transcript`, text in a numeric column, a stray
`.txt` artefact in the workspace.

## Containment

The extractor is a child process, not a library binding, and that boundary is
used:

- Read only config mount, made unwritable in the image.
- One input file, one output file, no network.
- Wall clock timeout, killed on expiry.
- Address space, core dump and process count limits applied between fork and
  exec.
- The service runs unprivileged with a read only root filesystem and a tmpfs
  scratch mount, so nothing survives the task.

## Input validation

Only PCM WAV is accepted. Accepting compressed formats would mean running a
decoder over patient supplied bytes, and a decoder is a far larger attack
surface than a header parser. Sample rate, channel count, sample width and
frame count are checked before the extractor is invoked. Uploads stream to
disk with a hard byte cap rather than being read into memory, so a malformed
content length header does not decide how much memory the process allocates.

## Sample quality

A bad sample scored anyway produces a spurious deviation, and a spurious
deviation costs a clinician a phone call. The service classifies each sample
before it goes anywhere:

- `too_short`: under four seconds, too little for a stable functional estimate
- `no_speech`: voiced segment rate below the floor, so silence or background
- `noisy`: jitter above the clipping threshold, or harmonics to noise ratio
  under 2 dB, meaning room or handling noise dominates
- `usable`: everything else

Only `usable` samples should feed the scoring pipeline. The quality label
travels with the vector so the decision is explicit rather than implicit.

## Limitations, stated plainly

- **Not clinically validated.** The vector is well grounded in the literature.
  The thresholds that would turn it into a clinical instrument are not
  established, and this service is demo ready rather than deployment ready.
- **Prompt dependence.** Acoustic features vary with what is read. The
  platform sends a prompt id and expects the client to use a fixed set of
  prompts, but nothing enforces that the patient actually read the prompt.
- **Recording conditions.** Microphone quality, room acoustics and handset
  position all move these features. Comparison is against the patient's own
  prior samples, which absorbs some of this, and the quality gate rejects the
  worst of it, but neither eliminates it.
- **Medication effects.** Several psychotropics affect voice quality directly.
  A change in jitter, shimmer or harmonicity after a prescription change is
  more likely pharmacological than psychiatric. Voice quality features are
  reported separately rather than folded into a single score for this reason.
- **Language and accent.** eGeMAPS parameters are not language specific, but
  speech rate norms are. Personal baselining handles this. Cross patient
  comparison would not.

# Scoring pipeline

How fifteen numbers per hour become a risk score, and what is deliberately
missing from this repository.

## Personal baseline, not population baseline

Traycer compares every patient to themselves. This is the single most
consequential design decision in the platform.

A shift worker who sleeps at 09:00, a retiree who leaves the house twice a
week, and a graduate student who walks eleven kilometres a day have nothing in
common on a population distribution. Any of them would sit in a tail. All
three are unremarkable against their own fourteen day baseline, and all three
produce a clear signal when their own pattern breaks.

The practical consequence is that the platform works on a panel of forty. A
population model spends its capacity learning that people differ, which needs
tens of thousands of patients before it starts learning anything clinically
useful. A personal model spends its capacity learning that this person
changed, which needs two weeks of that person.

The cost is a fourteen day cold start during which the platform observes but
cannot alert. That is stated to clinicians in the dashboard rather than
concealed behind a provisional number.

## Building the baseline

`services/scoring/app/engine/baseline_builder.py`.

Input is the patient's earliest window of SDK data, fourteen days by default.
Output is, per feature: a median, a scale estimate, an interquartile range,
5th and 95th percentiles, a 24 hour circadian profile, and separate weekday
and weekend medians.

Four decisions worth stating:

**Median and median absolute deviation, not mean and standard deviation.** One
week away from home would inflate a standard deviation enough to mask a
genuine change for the following month. The MAD absorbs two anomalous days out
of fourteen. There is a test for exactly this.

**Confidence weighted.** Samples below 0.2 confidence are excluded outright.
The rest are weighted by confidence in the median calculation. A window where
the sensor was readable for a fifth of the period is closer to a gap than to a
measurement.

**A scale floor.** A feature that was perfectly constant during the baseline
window produces a MAD of zero, and every later observation would divide by
zero into an infinite deviation. The floor is five percent of the median, with
an absolute floor for features whose median is itself zero, such as night
usage for a patient who slept through the whole window.

**A circadian profile, not just a daily aggregate.** Sleep disruption is the
most reliable prodrome in bipolar disorder, and it is invisible in a daily
total. Twenty four hourly medians preserve it. Hours with no observation
inherit the overall median so the profile has no holes for downstream code to
special case.

Quality is reported honestly: `insufficient` under seven days, `provisional`
under fourteen, `established` at fourteen or more.

## Scoring a window

`services/scoring/app/engine/anomaly_detector.py`. Seven steps.

1. **Reduce.** The scoring window collapses to one value per feature, weighted
   by sensor confidence.
2. **Compare against the right hour.** Each observation is compared to the
   expected value for its own hour of day, then the residuals are averaged.
   Comparing a daily aggregate to a daily median instead would lose the
   circadian structure.
3. **Scale.** Divide by the baseline scale to get a deviation in units of that
   patient's own variability. The sign is preserved, because a mobility radius
   collapse and a mobility radius spike are different clinical pictures.
4. **Weight by direction.** Each feature has separate weights for rising and
   falling. Mobility radius falling is life space contraction; mobility radius
   rising is usually a holiday.
5. **Saturate.** Each feature's contribution is capped, so one broken sensor
   cannot carry the whole score.
6. **Combine with a power mean.** The exponent is what makes this different
   from an average. At an exponent of one, a single severe signal is diluted
   by every stable feature around it. As the exponent rises the largest
   deviations dominate. Relapse usually shows in two or three features before
   it shows in ten, so the combination has to be sensitive to a narrow signal
   without ignoring a broad one.
7. **Map to a score.** A logistic curve to 0 through 100, then a discount for
   sensor coverage and a further reduction if the baseline is still
   provisional. Linear scaling was tried first and was wrong in both tails:
   small deviations are noise and should stay near zero rather than crawling
   upward, and past a point more deviation does not mean more risk, it means
   the sensor broke.

An insufficient baseline produces no score at all. Refusing is the correct
answer, because a number produced against an unbuilt baseline looks exactly
like a real one on a dashboard and a clinician has no way to tell them apart.

## What is private, and why

### The coefficients

The weights, the direction asymmetries, the saturation cap, the power mean
exponent and the logistic parameters came out of labelled relapse episodes.
They are what defines a clinically significant deviation, and they are not
reconstructible from the code by anyone who has not run a clinical study of
comparable size.

The architecture above is public and complete. The numbers are loaded from a
private package, `traycer_coefficients`, which exposes one function returning
the `CoefficientSet` shape defined in the public file. The shape is public so
the interface is auditable. Only the values are withheld.

When the private package is absent, the detector runs on a placeholder set:
every weight 1.0, every direction symmetric, round logistic parameters. It
produces a mathematically valid score with no clinical meaning. This is
deliberate. The pipeline stays runnable end to end for anyone evaluating the
repository, and the fact that it is uncalibrated is reported in the API
response, in the health endpoint, in `/v1/model-status`, and in the alert
classifier, which refuses to fire on an uncalibrated score at all.

### The trajectory model

`services/scoring/app/engine/lstm_predictor.py` is an interface with no model
behind it.

The anomaly detector answers "is this patient different from themselves right
now". The trajectory model answers a harder question: "where is this patient
heading over the next seven to twenty one days". That second answer is the
core intellectual property.

Two independent reasons it stays private. Commercially, the weights are the
asset, and they are reproducible only by someone with a labelled clinical
corpus of comparable size. Ethically, published weights trained on a
psychiatric cohort of this size are a re-identification surface. Membership
inference against sequence models trained on small clinical cohorts is a
documented result, not a hypothetical one.

What is public: the input contract, the output contract, the failure
behaviour, and the integration point. A reviewer can see exactly what the model
receives and exactly what it returns.

Model shape, stated without giving the model away:

```
Input   30 timesteps of daily behavioral feature vectors, standardised
        against the patient's own baseline, dense with a coverage channel
Core    stacked LSTM, patient embedding concatenated at the final hidden state
Output  scalar risk at a forecast horizon, plus a calibrated interval
```

The interval matters. A point estimate alone invites a clinician to treat a 61
and a 64 as different, which the model cannot support.

### Current state

The interface is finished and integrated. The model is untrained. Activating
it needs a labelled clinical corpus with confirmed relapse dates, and that
corpus does not exist yet.

Until it does, `TrajectoryPredictor.available` is False, `predict` returns
None, and the scoring service reports `prediction_available: false`. Nothing
downstream fabricates a prediction to fill the gap, and the test suite pins
that behaviour, because the real risk with a stubbed component is that its
absence gets papered over somewhere and a made up number reaches a clinician.

## From score to alert

Scoring is deliberately separate from alerting. The scoring service answers a
numerical question. The pulse API answers a clinical one.
`services/pulse-api/app/engine/alert_classifier.py`, four filters:

1. **Confirmation.** The score must hold above threshold for a configured
   number of consecutive windows, two by default. One bad night is not a
   trajectory.
2. **Cooldown.** The same patient does not fire again at the same severity
   inside the cooldown window. An escalation from elevated to critical
   bypasses it, because that is new information.
3. **Muted signals.** A clinician who knows a patient is housebound for
   physical reasons can mute mobility for that patient without muting them.
4. **Calibration.** An uncalibrated score cannot fire a clinical alert at all.

Thresholds are per patient and clinician editable, because one global
threshold does not survive contact with a real panel. A patient six weeks out
of an inpatient stay needs a hair trigger. A patient stable on clozapine for
four years needs a threshold that does not fire every time they visit family.

The threshold validator refuses configurations that would silently turn
alerting off: a critical threshold at or below the elevated one, an elevated
threshold at 95 or above, more than seven confirmation windows, or quiet hours
covering most of the day. Each of those rules exists because it is a way a
well meaning clinician can disable monitoring without realising it.

Quiet hours suppress the notification, never the alert. The row stays in the
feed with its real timestamp, so a clinician opening the dashboard in the
morning sees exactly what happened overnight.

## Ranking

The feed is not chronological. Every open alert gets a rank combining severity,
score and age, so a critical alert from this morning outranks an elevated one
from ten minutes ago, and an unacknowledged alert climbs as it ages rather than
sinking out of view. Acknowledged alerts stay visible for context and drop
below everything still needing a decision.

## Testing

There is no real patient data in this repository and there will not be.
`services/scoring/tests/conftest.py` generates synthetic patients with a stable
daily rhythm and controllable noise, and `apply_relapse_signature` bends a
window in the directions the literature describes for a depressive prodrome:
life space contracts, routine variety drops, time at the primary place rises,
activity falls, phone use moves into the night, social contact drops.

The tests then assert that the detector notices the bend, that the score rises
monotonically with its intensity, that the dominant signal is one of the bent
features, and that the sign of each deviation records the correct direction.
That is a weaker claim than clinical validation and it is not presented as
more. It establishes that the pipeline responds to the right shape of change,
which is what a test suite can establish without patients.

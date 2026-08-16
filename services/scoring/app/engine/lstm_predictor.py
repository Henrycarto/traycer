"""Trajectory prediction.

=============================================================================
STUB. The model in this module is not in this repository.
=============================================================================

The anomaly detector answers "is this patient different from themselves right
now". This module answers a different and harder question: "where is this
patient's trajectory heading over the next seven to twenty one days". That
second answer is the core intellectual property of Traycer and it lives in a
private package, `traycer-relapse-model`, installed from a private index at
image build time and absent from the public tree.

What is public here is the interface. The input contract, the output contract,
the failure behavior and the integration point are all visible, so a reviewer
can see exactly what the model receives and exactly what it returns. What is
withheld is the architecture detail, the trained weights and the training
corpus.

Why it is withheld:

The weights encode labelled relapse episodes from consenting patients. Two
separate reasons keep them private. Commercially they are the asset, and they
are reproducible only by someone who has run a clinical study of comparable
size. Ethically, published weights trained on a psychiatric cohort of this
size are a re-identification surface, and membership inference against
sequence models trained on small clinical cohorts is a documented result, not
a hypothetical one.

Model shape, stated for reviewers without giving the model away:

    Input   sequence of daily behavioral feature vectors, 30 timesteps,
            standardised against the patient's own baseline
    Core    stacked LSTM over the sequence, patient embedding concatenated at
            the final hidden state
    Output  scalar risk at a forecast horizon, plus a calibrated interval

Deployment state:

The interface is finished and integrated. The model is not trained. Activating
it requires a labelled clinical corpus with confirmed relapse dates, and that
corpus does not exist yet. Until it does, `TrajectoryPredictor.available` is
False everywhere, `predict` returns None, and the scoring service reports
`prediction_available: false` in its responses. Nothing downstream fabricates
a prediction to fill the gap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence, runtime_checkable

from ..schemas import BehavioralFeatureName

logger = logging.getLogger("traycer.scoring.predictor")

PRIVATE_PACKAGE = "traycer_relapse_model"

# Timesteps the model consumes. Thirty days is the shortest window that
# contains a full monthly cycle of routine for most patients.
SEQUENCE_LENGTH = 30

# Forecast horizon in days.
DEFAULT_HORIZON_DAYS = 7


@dataclass(frozen=True)
class TrajectoryInput:
    """One patient's standardised behavioral sequence.

    `sequence` is ordered oldest first and is dense: a day with no data is a
    row of zeros with its `coverage` entry set low, rather than a missing row.
    A sequence model that receives an irregular time axis learns the sampling
    schedule instead of the behavior.
    """

    patient_code: str
    as_of: datetime
    features: Sequence[BehavioralFeatureName]
    sequence: Sequence[Sequence[float]]
    coverage: Sequence[float]

    def validate(self) -> None:
        if len(self.sequence) != SEQUENCE_LENGTH:
            raise ValueError(
                f"sequence must have {SEQUENCE_LENGTH} timesteps, got {len(self.sequence)}"
            )
        if len(self.coverage) != len(self.sequence):
            raise ValueError("coverage must have one entry per timestep")
        width = len(self.features)
        for index, row in enumerate(self.sequence):
            if len(row) != width:
                raise ValueError(
                    f"timestep {index} has {len(row)} values, expected {width}"
                )


@dataclass(frozen=True)
class TrajectoryPrediction:
    patient_code: str
    horizon_days: int
    predicted_risk: float
    # Calibrated interval. A point estimate alone invites a clinician to treat
    # a 61 and a 64 as different, which the model cannot support.
    lower_bound: float
    upper_bound: float
    model_version: str


@runtime_checkable
class TrajectoryModel(Protocol):
    """Contract the private package satisfies.

    Declared here rather than in the private package so the public repository
    documents the full boundary. The private package imports nothing from this
    module, which keeps the dependency direction one way.
    """

    version: str

    def predict(self, payload: TrajectoryInput, horizon_days: int) -> TrajectoryPrediction: ...


class TrajectoryPredictor:
    """Loads the private model if it is present, degrades cleanly if not."""

    def __init__(self) -> None:
        self._model: TrajectoryModel | None = self._load()

    @staticmethod
    def _load() -> TrajectoryModel | None:
        try:
            from traycer_relapse_model import load_model  # type: ignore[import-not-found]
        except ImportError:
            logger.info(
                "%s is not installed, trajectory prediction is disabled", PRIVATE_PACKAGE
            )
            return None

        try:
            model = load_model()
        except Exception:
            # A model that fails to load must not take the scoring service
            # down with it. Anomaly detection against the personal baseline is
            # independently useful and keeps working without a prediction.
            logger.exception("failed to load the trajectory model, prediction disabled")
            return None

        logger.info("trajectory model loaded, version %s", getattr(model, "version", "unknown"))
        return model

    @property
    def available(self) -> bool:
        return self._model is not None

    @property
    def version(self) -> str | None:
        return getattr(self._model, "version", None) if self._model else None

    def predict(
        self, payload: TrajectoryInput, horizon_days: int = DEFAULT_HORIZON_DAYS
    ) -> TrajectoryPrediction | None:
        """Forecast risk, or return None when the model is unavailable.

        Returning None rather than raising is deliberate. The caller composes a
        response that is complete and useful without a prediction, and an
        exception here would make the absence of a private package look like a
        service fault in every log and every dashboard.
        """
        if self._model is None:
            return None

        payload.validate()
        return self._model.predict(payload, horizon_days)


def describe_stub() -> dict[str, object]:
    """Machine readable statement of what is missing and why.

    Surfaced through the health endpoint so an operator can tell a model that
    failed to load from a model that was never installed.
    """
    return {
        "component": "lstm_predictor",
        "status": "stub",
        "private_package": PRIVATE_PACKAGE,
        "sequence_length": SEQUENCE_LENGTH,
        "default_horizon_days": DEFAULT_HORIZON_DAYS,
        "reason": (
            "trained weights encode labelled psychiatric relapse episodes and are "
            "withheld for both commercial and re-identification reasons"
        ),
        "activation_requirement": (
            "labelled clinical corpus with confirmed relapse dates, not yet collected"
        ),
    }

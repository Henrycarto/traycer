"""The stub has to fail in a specific way, so the way it fails is tested.

The risk with a stubbed component is that its absence gets papered over
somewhere downstream and a fabricated number reaches a clinician. These tests
pin the contract: unavailable means None, never a default, never a zero.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.engine.lstm_predictor import (
    DEFAULT_HORIZON_DAYS,
    SEQUENCE_LENGTH,
    TrajectoryInput,
    TrajectoryPredictor,
    describe_stub,
)
from app.schemas import BehavioralFeatureName as F

FEATURES = (F.ACTIVITY_RATIO, F.MOBILITY_RADIUS_M, F.NIGHT_USAGE_MINUTES)


def make_input(timesteps: int = SEQUENCE_LENGTH, width: int = len(FEATURES)) -> TrajectoryInput:
    return TrajectoryInput(
        patient_code="TRC-2f81a9c4",
        as_of=datetime(2026, 2, 1, tzinfo=timezone.utc),
        features=FEATURES,
        sequence=[[0.1] * width for _ in range(timesteps)],
        coverage=[1.0] * timesteps,
    )


class TestAvailability:
    def test_the_predictor_is_unavailable_without_the_private_package(self) -> None:
        predictor = TrajectoryPredictor()

        assert predictor.available is False
        assert predictor.version is None

    def test_predict_returns_none_rather_than_a_placeholder_number(self) -> None:
        predictor = TrajectoryPredictor()
        assert predictor.predict(make_input()) is None

    def test_predict_does_not_raise_when_the_model_is_absent(self) -> None:
        # Raising here would turn a documented gap into a service fault in
        # every log line and every dashboard.
        TrajectoryPredictor().predict(make_input(), horizon_days=21)


class TestInputContract:
    def test_a_correctly_shaped_sequence_validates(self) -> None:
        make_input().validate()

    def test_a_short_sequence_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=f"{SEQUENCE_LENGTH} timesteps"):
            make_input(timesteps=12).validate()

    def test_a_ragged_sequence_is_rejected(self) -> None:
        payload = TrajectoryInput(
            patient_code="TRC-2f81a9c4",
            as_of=datetime(2026, 2, 1, tzinfo=timezone.utc),
            features=FEATURES,
            sequence=[[0.1, 0.2, 0.3] for _ in range(SEQUENCE_LENGTH - 1)] + [[0.1]],
            coverage=[1.0] * SEQUENCE_LENGTH,
        )

        with pytest.raises(ValueError, match="expected 3"):
            payload.validate()

    def test_coverage_must_match_the_sequence_length(self) -> None:
        payload = TrajectoryInput(
            patient_code="TRC-2f81a9c4",
            as_of=datetime(2026, 2, 1, tzinfo=timezone.utc),
            features=FEATURES,
            sequence=[[0.1, 0.2, 0.3] for _ in range(SEQUENCE_LENGTH)],
            coverage=[1.0] * 5,
        )

        with pytest.raises(ValueError, match="one entry per timestep"):
            payload.validate()


class TestStubDescription:
    def test_the_stub_states_what_is_missing_and_why(self) -> None:
        described = describe_stub()

        assert described["status"] == "stub"
        assert described["private_package"] == "traycer_relapse_model"
        assert described["sequence_length"] == SEQUENCE_LENGTH
        assert described["default_horizon_days"] == DEFAULT_HORIZON_DAYS
        assert "re-identification" in str(described["reason"])
        assert "clinical corpus" in str(described["activation_requirement"])

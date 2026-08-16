from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.engine.threshold_config import (
    InMemoryThresholdStore,
    ThresholdManager,
    ThresholdValidationError,
    default_thresholds,
    validate,
)
from app.schemas import ThresholdUpdate


@pytest.fixture
def store() -> InMemoryThresholdStore:
    return InMemoryThresholdStore()


@pytest.fixture
def manager(store: InMemoryThresholdStore) -> ThresholdManager:
    return ThresholdManager(store)


class TestDefaults:
    async def test_an_unconfigured_patient_gets_the_defaults(self, manager) -> None:
        thresholds = await manager.get("TRC-2f81a9c4")

        assert thresholds.elevated_score == 55.0
        assert thresholds.critical_score == 75.0
        assert thresholds.min_consecutive == 2
        assert thresholds.cooldown_hours == 24

    async def test_the_defaults_are_a_valid_policy(self) -> None:
        validate(default_thresholds("TRC-2f81a9c4"))


class TestUpdates:
    async def test_a_partial_update_leaves_the_rest_alone(self, manager) -> None:
        updated = await manager.update("TRC-2f81a9c4", ThresholdUpdate(elevated_score=45.0))

        assert updated.elevated_score == 45.0
        assert updated.critical_score == 75.0

    async def test_an_update_persists(self, manager) -> None:
        await manager.update("TRC-2f81a9c4", ThresholdUpdate(cooldown_hours=6))
        assert (await manager.get("TRC-2f81a9c4")).cooldown_hours == 6

    async def test_an_empty_update_is_a_no_op(self, manager, store) -> None:
        before = await manager.get("TRC-2f81a9c4")
        after = await manager.update("TRC-2f81a9c4", ThresholdUpdate())

        # Nothing was persisted, so the patient still has no stored row and is
        # still running on defaults.
        assert await store.get("TRC-2f81a9c4") is None

        # Comparing updated_at directly would compare two independently
        # generated default timestamps, which differ by microseconds on a
        # platform with a fine grained clock.
        assert after.model_dump(exclude={"updated_at"}) == before.model_dump(
            exclude={"updated_at"}
        )

    async def test_reset_returns_to_the_defaults(self, manager) -> None:
        await manager.update("TRC-2f81a9c4", ThresholdUpdate(elevated_score=40.0))
        reset = await manager.reset("TRC-2f81a9c4")

        assert reset.elevated_score == 55.0


class TestValidation:
    async def test_critical_below_elevated_is_rejected(self, manager) -> None:
        with pytest.raises(ThresholdValidationError, match="must sit above"):
            await manager.update("TRC-2f81a9c4", ThresholdUpdate(critical_score=40.0))

    async def test_an_unreachable_elevated_threshold_is_rejected(self, manager) -> None:
        with pytest.raises(ThresholdValidationError, match="never fire"):
            await manager.update(
                "TRC-2f81a9c4", ThresholdUpdate(elevated_score=96.0, critical_score=99.0)
            )

    async def test_too_many_confirmation_windows_is_rejected(self, manager) -> None:
        with pytest.raises(ThresholdValidationError, match="seven consecutive"):
            await manager.update("TRC-2f81a9c4", ThresholdUpdate(min_consecutive=10))

    async def test_half_configured_quiet_hours_are_rejected(self, manager) -> None:
        with pytest.raises(ThresholdValidationError, match="both a start and an end"):
            await manager.update("TRC-2f81a9c4", ThresholdUpdate(quiet_hours_start=22))

    async def test_quiet_hours_covering_the_whole_day_are_rejected(self, manager) -> None:
        with pytest.raises(ThresholdValidationError, match="whole day"):
            await manager.update(
                "TRC-2f81a9c4", ThresholdUpdate(quiet_hours_start=8, quiet_hours_end=8)
            )

    async def test_an_excessively_long_quiet_window_is_rejected(self, manager) -> None:
        with pytest.raises(ThresholdValidationError, match="span"):
            await manager.update(
                "TRC-2f81a9c4", ThresholdUpdate(quiet_hours_start=6, quiet_hours_end=23)
            )


class TestQuietHours:
    async def test_no_quiet_hours_means_never_quiet(self, manager) -> None:
        thresholds = await manager.get("TRC-2f81a9c4")
        at = datetime(2026, 3, 1, 3, 0, tzinfo=timezone.utc)

        assert await manager.is_quiet_hour(thresholds, at) is False

    async def test_a_window_that_wraps_past_midnight(self, manager) -> None:
        thresholds = await manager.update(
            "TRC-2f81a9c4", ThresholdUpdate(quiet_hours_start=22, quiet_hours_end=7)
        )

        assert await manager.is_quiet_hour(
            thresholds, datetime(2026, 3, 1, 23, 30, tzinfo=timezone.utc)
        )
        assert await manager.is_quiet_hour(
            thresholds, datetime(2026, 3, 1, 3, 0, tzinfo=timezone.utc)
        )
        assert not await manager.is_quiet_hour(
            thresholds, datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        )

    async def test_a_window_inside_one_day(self, manager) -> None:
        thresholds = await manager.update(
            "TRC-2f81a9c4", ThresholdUpdate(quiet_hours_start=13, quiet_hours_end=15)
        )

        assert await manager.is_quiet_hour(
            thresholds, datetime(2026, 3, 1, 14, 0, tzinfo=timezone.utc)
        )
        assert not await manager.is_quiet_hour(
            thresholds, datetime(2026, 3, 1, 15, 0, tzinfo=timezone.utc)
        )

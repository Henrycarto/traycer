"""Contract test between the SDK and the scoring service.

The feature vocabulary is defined twice: once as a TypeScript union in the SDK
and once as a Python enum here. Two definitions of the same list drift, and
the drift is silent. A feature added on the device that the scoring service
does not know about is dropped without a trace, which is the worst kind of bug
in a monitoring system because the dashboard still looks fine.

This test reads the TypeScript source and compares. It fails the build rather
than hoping someone notices.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.schemas import BehavioralFeatureName

SDK_TYPES = (
    Path(__file__).resolve().parents[3] / "packages" / "sdk" / "src" / "types" / "index.ts"
)

# Matches the runtime array rather than the type union, because the array is
# what the SDK actually validates against at runtime.
ARRAY_BLOCK = re.compile(
    r"BEHAVIORAL_FEATURES:\s*readonly BehavioralFeature\[\]\s*=\s*\[(.*?)\]\s*as const;",
    re.DOTALL,
)


def sdk_features() -> set[str]:
    source = SDK_TYPES.read_text(encoding="utf-8")
    match = ARRAY_BLOCK.search(source)
    if match is None:
        pytest.fail(f"could not find BEHAVIORAL_FEATURES in {SDK_TYPES}")
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


@pytest.mark.skipif(not SDK_TYPES.exists(), reason="SDK source is not checked out")
class TestFeatureParity:
    def test_the_two_vocabularies_are_identical(self) -> None:
        from_sdk = sdk_features()
        from_service = {feature.value for feature in BehavioralFeatureName}

        assert from_sdk == from_service, (
            f"only in the SDK: {sorted(from_sdk - from_service)}, "
            f"only in the scoring service: {sorted(from_service - from_sdk)}"
        )

    def test_the_vocabulary_is_not_empty(self) -> None:
        assert len(sdk_features()) == 15

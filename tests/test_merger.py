"""Basic tests for fit_merger.core."""

import os
import pytest

from fit_merger.core.merger import _safe_add16, _safe_add32, fmt_time
from fit_merger.core.parser import FitParser


# ── Unit tests ────────────────────────────────────────────────────


def test_safe_add16_normal():
    assert _safe_add16(100, 200) == 300


def test_safe_add16_invalid_sentinel():
    assert _safe_add16(0xFFFF, 100) == 100


def test_safe_add16_cap():
    assert _safe_add16(0xFFFE, 0xFFFE) == 0xFFFE


def test_safe_add32_normal():
    assert _safe_add32(1000, 2000) == 3000


def test_safe_add32_invalid_sentinel():
    assert _safe_add32(0xFFFFFFFF, 500) == 500


def test_fmt_time_minutes():
    assert fmt_time(90_000) == "1:30"


def test_fmt_time_hours():
    assert fmt_time(3_661_000) == "1:01:01"


# ── Integration test (requires sample files) ─────────────────────


SAMPLES = os.path.join(os.path.dirname(__file__), "..", "data", "samples")


@pytest.mark.skipif(
    not any(f.endswith(".fit") for f in os.listdir(SAMPLES))
    if os.path.isdir(SAMPLES) else True,
    reason="No sample .fit files in data/samples/",
)
def test_merge_sample_files():
    from fit_merger.core.merger import merge

    fit_files = sorted(
        f for f in os.listdir(SAMPLES) if f.endswith(".fit")
    )
    assert len(fit_files) >= 2, "Need at least two .fit files in data/samples/"

    with open(os.path.join(SAMPLES, fit_files[0]), "rb") as f:
        data1 = f.read()
    with open(os.path.join(SAMPLES, fit_files[1]), "rb") as f:
        data2 = f.read()

    result = merge(data1, data2)
    assert result[:4] == b'\x0e'[:1] + b'\x10'[:0]  # header size sanity
    assert b'.FIT' in result[:16]

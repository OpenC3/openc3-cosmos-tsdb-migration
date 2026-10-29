# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.

import pytest
from tsdb_migration import configure_log_level, format_bytes, format_duration, format_progress


class StubLogger:
    DEBUG = 0
    INFO = 1
    WARN = 2
    ERROR = 3
    FATAL = 4

    def __init__(self):
        self.level = self.INFO


@pytest.mark.parametrize(
    ("configured_level", "expected_level"),
    [
        ("debug", StubLogger.DEBUG),
        ("INFO", StubLogger.INFO),
        ("WARN", StubLogger.WARN),
        ("ERROR", StubLogger.ERROR),
        ("FATAL", StubLogger.FATAL),
    ],
)
def test_configure_log_level(configured_level, expected_level):
    logger = StubLogger()

    configure_log_level(logger, configured_level)

    assert logger.level == expected_level


def test_configure_log_level_rejects_unknown_value():
    with pytest.raises(ValueError, match="Invalid LOG_LEVEL"):
        configure_log_level(StubLogger(), "trace")


@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536 * 1024, "1.5 MiB"),
        (3 * 1024**3, "3.0 GiB"),
        (2048 * 1024**4, "2048.0 TiB"),
    ],
)
def test_format_bytes(num_bytes, expected):
    assert format_bytes(num_bytes) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(45, "45s"), (125, "2m 5s"), (3 * 3600 + 120, "3h 2m"), (90000, "1d 1h 0m")],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_format_progress_includes_previous_runs_and_eta():
    gib = 1024**3
    # 4 GiB done in earlier runs, 1 GiB this run in 100s, 5 GiB left -> 500s
    progress = format_progress(5 * gib, 10 * gib, 1 * gib, 100)
    assert progress == "50.00% (5.0 GiB / 10.0 GiB), ETA 8m 20s"


def test_format_progress_without_rate_omits_eta():
    assert format_progress(0, 2048, 0, 0) == "0.00% (0 B / 2.0 KiB)"


def test_format_progress_empty_total_is_complete():
    assert format_progress(0, 0, 0, 0) == "100.00% (0 B / 0 B)"

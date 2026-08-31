# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.

import pytest
from tsdb_migration import configure_log_level


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

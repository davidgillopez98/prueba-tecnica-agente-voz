import logging

from src.api.common.logger import LOGGER_NAME, configure_logger, get_logger


def test_named_logger_is_reused_without_duplicate_handlers():
    first = configure_logger("DEBUG")
    assert first is get_logger()
    assert first.name == LOGGER_NAME
    assert first.level == logging.DEBUG
    assert len(first.handlers) == 1

    second = configure_logger("INFO")
    assert second is first
    assert second.level == logging.INFO
    assert len(second.handlers) == 1

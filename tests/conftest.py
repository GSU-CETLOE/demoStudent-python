"""Shared test fixtures."""

import logging

import pytest

from demostudent.logs import LOGGER_NAME


@pytest.fixture(autouse=True)
def resetFileLogger():
    """Detach file handlers between tests so log output never crosses tests."""
    yield
    logger = logging.getLogger(LOGGER_NAME)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()

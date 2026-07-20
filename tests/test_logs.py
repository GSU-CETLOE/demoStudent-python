"""Tests for file logging into the working directory's logs folder."""

import logging
from datetime import datetime

from demostudent.logs import LOGGER_NAME, setupFileLogging


def test_setupCreatesLogsDirAndMonthlyFile(tmp_path):
    logPath = setupFileLogging(tmp_path / "working")
    assert logPath.parent == tmp_path / "working" / "logs"
    assert logPath.name == f"demoStudent_{datetime.now():%Y%m}.log"

    logging.getLogger(LOGGER_NAME).error("boom happened")
    content = logPath.read_text()
    assert "boom happened" in content
    assert "ERROR" in content


def test_infoAndWarningAreAlsoLogged(tmp_path):
    logPath = setupFileLogging(tmp_path / "working")
    logger = logging.getLogger(LOGGER_NAME)
    logger.info("run started")
    logger.warning("something odd")
    content = logPath.read_text()
    assert "INFO run started" in content
    assert "WARNING something odd" in content


def test_setupTwiceDoesNotDuplicateEntries(tmp_path):
    setupFileLogging(tmp_path / "working")
    logPath = setupFileLogging(tmp_path / "working")
    logging.getLogger(LOGGER_NAME).error("only once")
    assert logPath.read_text().count("only once") == 1


def test_logLinesAreTimestamped(tmp_path):
    logPath = setupFileLogging(tmp_path / "working")
    logging.getLogger(LOGGER_NAME).error("timed entry")
    line = logPath.read_text().splitlines()[0]
    assert line.startswith(str(datetime.now().year))


def test_customLogDirRelativeToWorkingDir(tmp_path):
    logPath = setupFileLogging(tmp_path / "working", logDir="audit")
    assert logPath.parent == tmp_path / "working" / "audit"


def test_customLogDirAbsolutePathRespected(tmp_path):
    absoluteLogDir = tmp_path / "elsewhere" / "logs"
    logPath = setupFileLogging(tmp_path / "working", logDir=str(absoluteLogDir))
    assert logPath.parent == absoluteLogDir


def test_customPrefixNamesTheLogFile(tmp_path):
    logPath = setupFileLogging(tmp_path / "working", prefix="myApp")
    assert logPath.name == f"myApp_{datetime.now():%Y%m}.log"

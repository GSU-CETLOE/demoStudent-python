"""File logging for errors, issues, and run lifecycle events.

Log files live in {workingDir}/logs/ and roll monthly
(demoStudent_YYYYMM.log), mirroring the legacy cron's log naming. The
file log complements the SQLite errors table: the database feeds
dashboards, the log files give a plain-text audit trail. Console output
is not routed through this logger.
"""

import logging
from datetime import datetime
from pathlib import Path

LOGGER_NAME = "demostudent"

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def setupFileLogging(workingDir: Path) -> Path:
    """Attach a monthly file handler to the application logger.

    Safe to call repeatedly: existing handlers are replaced, never stacked.
    Returns the path of the active log file.
    """
    logsDir = workingDir / "logs"
    logsDir.mkdir(parents=True, exist_ok=True)
    logPath = logsDir / f"demoStudent_{datetime.now():%Y%m}.log"

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()

    fileHandler = logging.FileHandler(logPath, encoding="utf-8")
    fileHandler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(fileHandler)
    return logPath

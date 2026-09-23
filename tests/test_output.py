"""Tests for timestamped console output.

Every line that reports something happening carries the time it happened,
in the same format the log files use, so console output and
working/logs/demoStudent_YYYYMM.log line up when comparing a run against
its record.
"""

import re

import pytest

from demostudent.output import emit, emitBlock, stamp

# The log files use "%(asctime)s" from logging, e.g. 2026-09-22 18:45:17,376.
# The console drops the milliseconds.
TIMESTAMP = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"


def test_stampPrefixesTheCurrentTime():
    assert re.match(rf"^{TIMESTAMP}  hello$", stamp("hello"))


def test_stampMatchesTheLogFileFormat():
    """Console and log file should be comparable line by line."""
    from datetime import datetime

    stamped = stamp("x")
    timePart = stamped[:19]
    # Parses with the same format logging writes, milliseconds aside
    assert datetime.strptime(timePart, "%Y-%m-%d %H:%M:%S")


def test_emitStampsAndFlushes(capsys):
    emit("something happened")
    out = capsys.readouterr().out
    assert re.match(rf"^{TIMESTAMP}  something happened\n$", out)


def test_emitLeavesBlankLinesBlank(capsys):
    """A spacer line should stay a spacer, not become a bare timestamp."""
    emit("")
    assert capsys.readouterr().out == "\n"


def test_emitBlockStampsOnlyTheHeader(capsys):
    """Aligned blocks keep their alignment; the header carries the time."""
    emitBlock("Run summary", [
        "  Status:               completed",
        "  Errors:               0",
    ])
    lines = capsys.readouterr().out.splitlines()
    assert re.match(rf"^{TIMESTAMP}  Run summary$", lines[0])
    assert lines[1] == "  Status:               completed"
    assert lines[2] == "  Errors:               0"


def test_emitBlockIndentsUnderTheStamp(capsys):
    """Block rows line up with the stamped header's text, not column zero."""
    emitBlock("Header", ["  key: value"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith("  ")


def test_timestampsAreMonotonicAcrossCalls(capsys):
    emit("first")
    emit("second")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0][:19] <= lines[1][:19]


@pytest.mark.parametrize("count, expected", [
    (0, "0 courses"),
    (1, "1 course"),
    (2, "2 courses"),
])
def test_pluralAgreesInNumber(count, expected):
    from demostudent.output import plural
    assert plural(count, "course") == expected

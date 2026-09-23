"""Timestamped console output.

Every line reporting something happening carries the time it happened,
in the same format the log files use, so a console session and
working/logs/{prefix}_YYYYMM.log can be read side by side.

Output is flushed on every line. Python block-buffers stdout when it is
not a terminal, so a redirected run would otherwise show nothing for
minutes and then everything at once, which defeats the point of progress
output entirely.
"""

import sys
from datetime import datetime

# Matches logging's "%(asctime)s" without the milliseconds, which are
# noise at the console but useful in a log file.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# Two spaces after the stamp, so block rows can be indented to align
# under the message rather than under the time.
SEPARATOR = "  "


def timestamp() -> str:
    """The current local time, formatted as the log files write it."""
    return datetime.now().strftime(TIMESTAMP_FORMAT)


def stamp(line: str) -> str:
    """One line with the current time in front of it."""
    return f"{timestamp()}{SEPARATOR}{line}"


def emit(line: str = "") -> None:
    """Print a timestamped line and flush it.

    An empty line stays empty: spacers separate sections, and a bare
    timestamp on its own would read as an event that never happened.
    """
    if not line:
        print(flush=True)
        return
    print(stamp(line), flush=True)


def emitBlock(header: str, rows: list[str]) -> None:
    """Print a stamped header followed by rows left as they are.

    Summaries and tables are written as aligned columns, and stamping
    every row would both break that alignment and imply the rows happened
    at different times. They are one block, printed at one instant, so
    the header carries the time for all of them.
    """
    emit(header)
    for row in rows:
        print(row, flush=True)


def plural(count: int, noun: str) -> str:
    """Count and noun agreeing in number: '1 course', '2 courses'."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def emitError(line: str) -> None:
    """Print a timestamped line to stderr."""
    print(stamp(line), file=sys.stderr, flush=True)

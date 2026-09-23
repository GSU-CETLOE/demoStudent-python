"""Reading demo student accounts from a Brightspace Data Sets extract.

D2L publishes a Users data set as a zipped CSV. It is the better source
for this tool in one important way: it carries OrgRoleId, which the live
user API does not, so a demo student can be identified by its role
rather than by its username alone.

The tradeoff is freshness. D2L regenerates full extracts weekly on the
default schedule, with no guarantee of when generation starts or how
long it takes, so an account created since the last extract is missing
and a course deleted since then still looks alive. That is acceptable
here because an orphaned demo student has typically been orphaned for
months; it would not be acceptable for the enrollment run, which is why
that still reads live.

Every field in the extract arrives as a string, including the numbers
and the IsActive flag, so everything is converted explicitly.
"""

import csv
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig

USERS_DATA_SET_NAME = "Users"

# Columns this tool reads. A schema change that drops one should fail
# loudly rather than quietly yield no demo students.
REQUIRED_COLUMNS = ("UserId", "UserName", "IsActive", "OrgRoleId", "DateDeleted")

# The full extract carries a synthetic system account at this id.
SYSTEM_USER_ID = 0


class DataSetFormatError(Exception):
    """The extract was not shaped the way this tool expects."""


@dataclass(frozen=True)
class ExtractReadResult:
    """Demo students found in one extract, and what was passed over."""

    demoStudents: list[dict] = field(default_factory=list)
    rowsRead: int = 0
    unparsed: list[str] = field(default_factory=list)
    wrongRole: list[str] = field(default_factory=list)


def _parseIsActive(rawValue: str) -> bool | None:
    """The IsActive bit as a boolean, or None when the extract left it empty.

    D2L documents this column as nullable and never says what null means,
    so an empty value is carried through as unknown rather than guessed
    at. Treating it as inactive would silently exempt those accounts from
    cleanup; treating it as active could deactivate something twice.
    """
    value = rawValue.strip()
    if not value:
        return None
    return value not in ("0", "False", "false")


def _openUsersCsv(archivePath: Path) -> str:
    """The CSV text inside a Users extract archive."""
    with zipfile.ZipFile(archivePath) as archive:
        csvNames = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not csvNames:
            raise DataSetFormatError(
                f"{archivePath.name} holds no CSV file, so it is not a Users extract "
                f"(found: {', '.join(archive.namelist()) or 'nothing'})")
        with archive.open(csvNames[0]) as handle:
            return handle.read().decode("utf-8-sig")


def readDemoStudentsFromExtract(
    archivePath: Path,
    config: AppConfig,
) -> ExtractReadResult:
    """Demo student accounts listed in a Users extract.

    An account counts as a demo student when its username parses as
    {usernamePrefix}.{orgUnitId} *and* its org-level role matches the
    configured roleId. Requiring both is what the live API cannot do,
    since its user listing carries no role at all. An account named like
    ours but holding another role is recorded separately and never
    touched.

    Rows for the synthetic system user and for users already deleted in
    D2L are skipped.
    """
    from .orphans import parseDemoStudentUsername

    text = _openUsersCsv(archivePath)
    reader = csv.DictReader(io.StringIO(text))

    missing = [column for column in REQUIRED_COLUMNS
               if column not in (reader.fieldnames or [])]
    if missing:
        raise DataSetFormatError(
            f"{archivePath.name} is missing the column(s) this tool needs: "
            f"{', '.join(missing)}. The Users data set schema may have changed.")

    demoStudents: list[dict] = []
    unparsed: list[str] = []
    wrongRole: list[str] = []
    rowsRead = 0

    for row in reader:
        rowsRead += 1

        userIdText = (row.get("UserId") or "").strip()
        if not userIdText.isdigit() or int(userIdText) == SYSTEM_USER_ID:
            continue
        if (row.get("DateDeleted") or "").strip():
            continue

        userName = (row.get("UserName") or "").strip()
        if not userName:
            continue

        orgUnitId = parseDemoStudentUsername(userName, config)
        if orgUnitId is None:
            if userName.lower().startswith(f"{config.usernamePrefix}.".lower()):
                unparsed.append(userName)
            continue

        orgRoleId = (row.get("OrgRoleId") or "").strip()
        if orgRoleId != str(config.roleId):
            wrongRole.append(userName)
            continue

        demoStudents.append({
            "userId": int(userIdText),
            "userName": userName,
            "orgUnitId": orgUnitId,
            "isActive": _parseIsActive(row.get("IsActive") or ""),
        })

    return ExtractReadResult(
        demoStudents=demoStudents, rowsRead=rowsRead,
        unparsed=unparsed, wrongRole=wrongRole,
    )

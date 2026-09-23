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
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import AppConfig
from .db import MetricsDb
from .logs import LOGGER_NAME
from .output import emit

fileLogger = logging.getLogger(LOGGER_NAME)

# Downloaded extracts live here under the working directory, beside the
# database and logs.
EXTRACT_SUBDIR = "datasets"

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


class DataSetsApi(Protocol):
    """The slice of D2lApiClient that Data Sets reading needs."""

    def findDataSet(self, name: str) -> dict[str, Any] | None: ...
    def latestExtract(self, schemaId: str, pluginId: str) -> dict[str, Any] | None: ...
    def downloadExtract(self, downloadLink: str, target: Path) -> Path: ...


@dataclass(frozen=True)
class DataSetScanResult:
    """What one read of a Users extract turned up."""

    rowsRead: int = 0
    demoStudentsFound: int = 0
    newlyRecorded: int = 0
    unparsed: list[str] = field(default_factory=list)
    wrongRole: list[str] = field(default_factory=list)
    extractCreatedDate: str = ""


def _extractPath(config: AppConfig, createdDate: str) -> Path:
    """Where one extract lands, named for when D2L generated it.

    Naming by generation date means a re-run inside the same week finds
    the file already there and skips the download.
    """
    stamp = re.sub(r"[^0-9A-Za-z]", "", createdDate)
    return config.workingDir / EXTRACT_SUBDIR / f"Users_{stamp}.zip"


def scanDemoStudentsFromDataSets(
    api: DataSetsApi,
    config: AppConfig,
    db: MetricsDb,
    logFunc: Callable[[str], None] = emit,
) -> DataSetScanResult | None:
    """Record demo students from the newest Users extract into the inventory.

    Returns None when Data Sets cannot be used, which covers the tool
    being switched off for the tenant, the role lacking permission, the
    token lacking the scope, and no full extract having been generated
    yet. D2L reports the first three identically, so the caller falls
    back to another source rather than trying to tell them apart.

    The extract is kept on disk under the working directory. D2L
    regenerates it weekly by default, so a re-run in the same week reads
    the file already downloaded instead of fetching it again.
    """
    dataSet = api.findDataSet(USERS_DATA_SET_NAME)
    if dataSet is None:
        logFunc("Brightspace Data Sets is not available for this account.")
        return None

    extract = api.latestExtract(dataSet["schemaId"], dataSet["pluginId"])
    if extract is None:
        logFunc("Brightspace Data Sets has no full Users extract yet.")
        return None

    createdDate = extract["createdDate"]
    archivePath = _extractPath(config, createdDate)

    if archivePath.is_file():
        logFunc(f"Using the Users extract already downloaded, generated {createdDate}.")
    else:
        logFunc(f"Downloading the Users extract generated {createdDate}. "
                f"D2L regenerates this weekly, so it can be up to a week behind.")
        api.downloadExtract(extract["downloadLink"], archivePath)

    readResult = readDemoStudentsFromExtract(archivePath, config)
    newlyRecorded = db.recordDemoStudents(readResult.demoStudents)

    logFunc(f"Read {readResult.rowsRead} users; found "
            f"{len(readResult.demoStudents)} demo students "
            f"({newlyRecorded} new to the inventory).")
    if readResult.wrongRole:
        noun = "account" if len(readResult.wrongRole) == 1 else "accounts"
        logFunc(f"  Left alone: {len(readResult.wrongRole)} {noun} named like a demo "
                f"student but not holding role {config.roleId}.")
    if readResult.unparsed:
        noun = "account" if len(readResult.unparsed) == 1 else "accounts"
        logFunc(f"  Left alone: {len(readResult.unparsed)} {noun} matching the "
                f"'{config.usernamePrefix}.' prefix with no usable org unit id.")

    fileLogger.info(
        f"Data Sets scan (extract {createdDate}): read {readResult.rowsRead} users, "
        f"found {len(readResult.demoStudents)} demo students, {newlyRecorded} new, "
        f"{len(readResult.wrongRole)} wrong role, {len(readResult.unparsed)} unparsed")

    return DataSetScanResult(
        rowsRead=readResult.rowsRead,
        demoStudentsFound=len(readResult.demoStudents),
        newlyRecorded=newlyRecorded,
        unparsed=readResult.unparsed,
        wrongRole=readResult.wrongRole,
        extractCreatedDate=createdDate,
    )

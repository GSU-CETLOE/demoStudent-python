"""Course offering discovery.

Default mode walks the whole organization and collects every course
offering beneath it. Walking semesters instead would miss any course
whose only parent is a course template rather than a semester --
building sections, sandboxes, and university-related courses among them.
An explicit OU list can be supplied instead; course offerings in the
list are used directly and any other org unit type is walked for
course-offering descendants.
"""

from dataclasses import dataclass
from typing import Any, Iterator, Protocol

from .config import AppConfig


class OrgStructureApi(Protocol):
    """The slice of D2lApiClient that discovery needs."""

    def iterDescendants(self, orgUnitId: int, ouTypeId: int) -> Iterator[dict[str, Any]]: ...
    def getOrgUnit(self, orgUnitId: int) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CourseOffering:
    """A course offering org unit that should contain a demo student."""

    orgUnitId: int
    name: str
    code: str


def _asCourseOffering(item: dict[str, Any]) -> CourseOffering:
    return CourseOffering(
        orgUnitId=int(item["Identifier"]),
        name=item.get("Name") or "",
        code=item.get("Code") or "",
    )


def discoverCourseOfferings(
    api: OrgStructureApi,
    config: AppConfig,
    ouList: list[int] | None = None,
) -> list[CourseOffering]:
    """All course offerings to process, deduplicated by org unit id."""
    offerings: dict[int, CourseOffering] = {}

    if ouList:
        for orgUnitId in ouList:
            orgUnit = api.getOrgUnit(orgUnitId)
            if orgUnit["Type"]["Id"] == config.courseOfferingOuType:
                offering = _asCourseOffering(orgUnit)
                offerings[offering.orgUnitId] = offering
            else:
                for item in api.iterDescendants(orgUnitId, config.courseOfferingOuType):
                    offering = _asCourseOffering(item)
                    offerings[offering.orgUnitId] = offering
        return list(offerings.values())

    for item in api.iterDescendants(config.orgOu, config.courseOfferingOuType):
        offering = _asCourseOffering(item)
        offerings[offering.orgUnitId] = offering
    return list(offerings.values())

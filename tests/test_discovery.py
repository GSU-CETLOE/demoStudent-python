"""Tests for course offering discovery."""

import pytest

from demostudent.config import AppConfig
from demostudent.discovery import CourseOffering, discoverCourseOfferings


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        host="example.brightspace.com",
        clientId="client-id",
        clientSecret="client-secret",
        redirectUri="https://localhost:8443/callback",
        workingDir=tmp_path / "working",
        recentSemesterCount=2,
    )


class FakeApi:
    """Serves canned org-structure data through the D2lApiClient interface."""

    def __init__(self, children=None, descendants=None, orgUnits=None):
        self.children = children or {}
        self.descendants = descendants or {}
        self.orgUnits = orgUnits or {}

    def getChildOrgUnits(self, orgUnitId, ouTypeId):
        return self.children[(orgUnitId, ouTypeId)]

    def iterDescendants(self, orgUnitId, ouTypeId):
        yield from self.descendants.get((orgUnitId, ouTypeId), [])

    def getOrgUnit(self, orgUnitId):
        return self.orgUnits[orgUnitId]


def courseItem(identifier, name="Course", code="CODE"):
    return {"Identifier": str(identifier), "Name": name, "Code": code}


def test_discoverFromRecentSemesters(config):
    api = FakeApi(
        children={
            (1070555, 5): [courseItem(201), courseItem(203), courseItem(202)],
        },
        descendants={
            (203, 3): [courseItem(301, "Bio", "BIO101"), courseItem(302)],
            (202, 3): [courseItem(303)],
        },
    )
    offerings = discoverCourseOfferings(api, config)
    # Two most recent semesters (203, 202) by descending id; 201 excluded
    assert {offering.orgUnitId for offering in offerings} == {301, 302, 303}
    bio = next(offering for offering in offerings if offering.orgUnitId == 301)
    assert bio == CourseOffering(orgUnitId=301, name="Bio", code="BIO101")


def test_recentSemesterCountOverride(config):
    api = FakeApi(
        children={(1070555, 5): [courseItem(201), courseItem(203), courseItem(202)]},
        descendants={(203, 3): [courseItem(301)]},
    )
    offerings = discoverCourseOfferings(api, config, recentSemesterCount=1)
    assert {offering.orgUnitId for offering in offerings} == {301}


def test_ouListWithCourseOfferingUsedDirectly(config):
    api = FakeApi(
        orgUnits={
            301: {"Identifier": "301", "Name": "Bio", "Code": "BIO101", "Type": {"Id": 3}},
        },
    )
    offerings = discoverCourseOfferings(api, config, ouList=[301])
    assert offerings == [CourseOffering(orgUnitId=301, name="Bio", code="BIO101")]


def test_ouListWithContainerWalksDescendants(config):
    api = FakeApi(
        orgUnits={202: {"Identifier": "202", "Name": "Fall", "Code": "F25", "Type": {"Id": 5}}},
        descendants={(202, 3): [courseItem(303), courseItem(304)]},
    )
    offerings = discoverCourseOfferings(api, config, ouList=[202])
    assert {offering.orgUnitId for offering in offerings} == {303, 304}


def test_duplicateCoursesDeduplicated(config):
    api = FakeApi(
        orgUnits={
            202: {"Identifier": "202", "Name": "Fall", "Code": "F25", "Type": {"Id": 5}},
            301: {"Identifier": "301", "Name": "Bio", "Code": "BIO101", "Type": {"Id": 3}},
        },
        descendants={(202, 3): [courseItem(301, "Bio", "BIO101")]},
    )
    offerings = discoverCourseOfferings(api, config, ouList=[202, 301])
    assert len(offerings) == 1

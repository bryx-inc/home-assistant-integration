"""Invented job payloads shaped like the ones the Bryx GraphQL API returns."""

import copy
from typing import Any

# Every value is made up, but the shapes match what graph.dev.bryx911.com
# returns, including the JSON-in-a-string GeoPoint and the epoch-millisecond
# Instant strings. The coordinates are in open ocean.
OPEN_JOB: dict[str, Any] = {
    "id": "job-open-0001",
    "ts": "1790865000000",
    "creationTs": "1790865000612",
    "lastModified": "1790865000904",
    "disposition": "OPEN",
    "synopsis": "SMOKE ALARM SOUNDING",
    "locationOfIncident": "100 EXAMPLE AVE, ANYTOWN",
    "centroid": (
        '{"type":"Point","coordinates":[-70.123456,39.654321],'
        '"crs":{"type":"name","properties":{"name":"EPSG:4326"}}}'
    ),
    "city": "ANYTOWN",
    "state": "ZZ",
    "zip": None,
    "street": "100 EXAMPLE AVE",
    "crossStreets": "FIRST ST / SECOND ST",
    "box": "BOX-1",
    "priority": "2",
    "incidentId": "INC-0001",
    "cadId": "CAD-0001",
    "municipality": "ANYTOWN",
    "locationInfo": None,
    "delayInSeconds": None,
    "assignment": ["BOX-1", "P2"],
    "type": {
        "id": "ALARM - RESIDENTIAL",
        "description": "RESIDENTIAL",
        "category": "FIRE",
        "section": "AUTOMATIC ALARM",
    },
    "agency": {
        "id": "agency-sheriff",
        "name": "Example County Sheriff's Office",
    },
    "units": [
        {"unitId": "unit-patrol-1", "unit": {"id": "unit-patrol-1", "name": "Patrol 1"}}
    ],
    "radioChannels": [],
    "responders": [],
    "supplementals": [],
    "currentResponse": None,
}

CLOSED_JOB: dict[str, Any] = {
    "id": "job-closed-0001",
    "ts": "1790862300000",
    "creationTs": "1790862300083",
    "lastModified": "1790864430250",
    "disposition": "CLOSED",
    "synopsis": "GRILL FIRE ON DECK",
    "locationOfIncident": "200 SAMPLE RD, OTHERTOWN",
    "centroid": (
        '{"type":"Point","coordinates":[-70.234567,39.765432],'
        '"crs":{"type":"name","properties":{"name":"EPSG:4326"}}}'
    ),
    "city": "OTHERTOWN",
    "state": "ZZ",
    "zip": "00000",
    "street": "200 SAMPLE RD",
    "crossStreets": None,
    "box": None,
    "priority": None,
    "incidentId": "INC-0002",
    "cadId": "CAD-0002",
    "municipality": "OTHERTOWN",
    "locationInfo": None,
    "delayInSeconds": None,
    "assignment": [],
    "type": {
        "id": "FIRE",
        "description": "OUTSIDE FIRE",
        "category": "FIRE",
        "section": "fire",
    },
    "agency": {"id": "agency-fire", "name": "Sample Fire Company"},
    "units": [
        {"unitId": "unit-engine-1", "unit": {"id": "unit-engine-1", "name": "Engine 1"}}
    ],
    "radioChannels": [{"channel": "TAC2", "description": "Tactical 2"}],
    "responders": [
        {
            "verboseResponse": "No",
            "isPositive": False,
            "responseTime": "1790863512345",
            "client": {"id": "client-1", "fullName": "Test Member", "initials": "TM"},
        },
        {
            "verboseResponse": "To Scene",
            "isPositive": True,
            "responseTime": "1790863512348",
            "client": {"id": "other-client", "fullName": "Alex Doe", "initials": "AD"},
        },
    ],
    # Deliberately out of order to prove the parser sorts them.
    "supplementals": [
        {"id": "s2", "text": "FIRE KNOCKED DOWN", "ts": "1790863435125"},
        {"id": "s1", "text": "NO EXPOSURES", "ts": "1790862370500"},
    ],
    "currentResponse": {
        "verboseResponse": "No",
        "isPositive": False,
        "responseTime": "1790863512345",
    },
}


def job_update(node: dict[str, Any], update_type: str = "UPDATED") -> dict[str, Any]:
    """Wrap a job node the way the subscription delivers it."""
    return {
        "jobs": {
            "__typename": "JobUpdateNode",
            "type": update_type,
            "job": copy.deepcopy(node),
        }
    }


def initial_loaded(count: int = 2) -> dict[str, Any]:
    """Return the sentinel the server sends after replaying existing jobs."""
    return {"jobs": {"__typename": "InitialDataLoaded", "numberEntities": count}}

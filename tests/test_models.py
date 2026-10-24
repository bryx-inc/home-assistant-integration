"""Tests for parsing the shapes the Bryx GraphQL API actually returns."""

from datetime import UTC, datetime

import pytest

from custom_components.bryx911.models import (
    BryxAgency,
    BryxData,
    BryxJob,
    BryxResponseOption,
    parse_geo_point,
    parse_instant,
    slugify_response,
)

from .job_payloads import CLOSED_JOB, OPEN_JOB


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1790865000000", datetime(2026, 10, 1, 14, 30, tzinfo=UTC)),
        (1790865000000, datetime(2026, 10, 1, 14, 30, tzinfo=UTC)),
        (None, None),
        ("", None),
        ("not-a-number", None),
    ],
)
def test_parse_instant(value, expected) -> None:
    """Instant is a string of epoch milliseconds, and bad input must not raise."""
    assert parse_instant(value) == expected


def test_parse_geo_point_swaps_to_lat_lon() -> None:
    """GeoPoint is GeoJSON in a string, and GeoJSON puts longitude first."""
    point = (
        '{"type":"Point","coordinates":[-70.123456,39.654321],'
        '"crs":{"type":"name","properties":{"name":"EPSG:4326"}}}'
    )
    assert parse_geo_point(point) == (39.654321, -70.123456)


@pytest.mark.parametrize("value", [None, "", "not json", '{"type":"Point"}', "{}"])
def test_parse_geo_point_bad_input(value) -> None:
    """A malformed GeoPoint yields None rather than raising."""
    assert parse_geo_point(value) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("To Scene", "to_scene"),
        ("Responding - Running Late", "responding_running_late"),
        ("I'll Meet You There", "i_ll_meet_you_there"),
        ("Not available today!", "not_available_today"),
        ("Yes", "yes"),
    ],
)
def test_slugify_response(text, expected) -> None:
    """Response option text becomes a stable key."""
    assert slugify_response(text) == expected


def test_job_from_open_node() -> None:
    """An OPEN job parses into an active job with coordinates."""
    job = BryxJob.from_node(OPEN_JOB)

    assert job.id == "job-open-0001"
    assert job.is_open
    assert job.disposition == "OPEN"
    assert job.synopsis == "SMOKE ALARM SOUNDING"
    assert job.job_type == "RESIDENTIAL"
    assert job.category == "FIRE"
    assert job.agency_name == "Example County Sheriff's Office"
    assert job.latitude == pytest.approx(39.654321)
    assert job.longitude == pytest.approx(-70.123456)
    assert job.units == ["Patrol 1"]
    assert job.unit_ids == ["unit-patrol-1"]
    assert job.assignment == ["BOX-1", "P2"]
    assert job.responders == []


def test_job_from_closed_node() -> None:
    """A CLOSED job is not open and carries its responders and notes."""
    job = BryxJob.from_node(CLOSED_JOB)

    assert not job.is_open
    assert job.own_response == "No"
    assert len(job.responders) == 2
    assert {responder.key for responder in job.responders} == {"no", "to_scene"}
    assert job.radio_channels == ["TAC2"]


def test_supplementals_sorted_oldest_first() -> None:
    """Supplementals are ordered by timestamp regardless of payload order."""
    job = BryxJob.from_node(CLOSED_JOB)

    assert [s.text for s in job.supplementals] == ["NO EXPOSURES", "FIRE KNOCKED DOWN"]


@pytest.mark.parametrize(
    ("disposition", "is_open"),
    [
        ("OPEN", True),
        # OLD is an open job that has aged, not a finished one.
        ("OLD", True),
        ("CLOSED", False),
        ("TIMED_OUT", False),
    ],
)
def test_open_and_old_are_both_active(disposition, is_open) -> None:
    """OPEN and OLD are active calls; CLOSED and TIMED_OUT are not."""
    node = dict(OPEN_JOB, disposition=disposition)
    assert BryxJob.from_node(node).is_open is is_open


def test_job_tolerates_missing_optional_fields() -> None:
    """A sparse node still parses; only id is truly required."""
    job = BryxJob.from_node({"id": "abc", "ts": "1790865000000"})

    assert job.id == "abc"
    assert job.synopsis == ""
    assert job.latitude is None
    assert job.units == []
    assert job.job_type == ""


def test_job_without_ts_falls_back_to_now() -> None:
    """A job missing ts still gets a usable timestamp."""
    job = BryxJob.from_node({"id": "abc"})

    assert job.ts.tzinfo is not None


def test_views_by_agency_and_unit() -> None:
    """An agency's view holds the jobs it dispatched; a unit's, the jobs listing it."""
    open_job = BryxJob.from_node(OPEN_JOB)
    closed_job = BryxJob.from_node(CLOSED_JOB)
    # A newer sheriff's job that also dispatches the fire company's engine.
    shared = BryxJob.from_node(
        dict(
            OPEN_JOB,
            id="job-shared",
            ts="1790866000000",
            units=OPEN_JOB["units"] + CLOSED_JOB["units"],
        )
    )
    data = BryxData(jobs={job.id: job for job in (closed_job, open_job, shared)})

    sheriff = data.for_agency(OPEN_JOB["agency"]["id"])
    assert [job.id for job in sheriff.sorted_jobs] == [shared.id, open_job.id]
    assert sheriff.latest.id == shared.id

    engine = data.for_unit("unit-engine-1")
    assert [job.id for job in engine.sorted_jobs] == [shared.id, closed_job.id]
    assert [job.id for job in engine.open_jobs] == [shared.id]
    assert engine.latest_open.id == shared.id

    assert [job.id for job in data.open_jobs] == [shared.id, open_job.id]


def test_empty_view() -> None:
    """A unit with no jobs has no latest job."""
    jobs = BryxData().for_unit("unit-engine-1")

    assert jobs.latest is None
    assert jobs.latest_open is None
    assert jobs.open_jobs == []


def test_agency_response_texts_deduplicated() -> None:
    """Duplicate texts within an agency collapse to one option."""
    agency = BryxAgency(
        id="a1",
        name="A",
        response_options=[
            BryxResponseOption(id="1", text="Yes", is_positive=True),
            # Real agencies really do list the same text twice.
            BryxResponseOption(id="2", text="Yes", is_positive=True),
            BryxResponseOption(id="3", text="No", is_positive=False),
            BryxResponseOption(id="4", text="To Scene", is_positive=True),
        ],
    )

    assert agency.response_texts == ["No", "To Scene", "Yes"]

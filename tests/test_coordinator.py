"""Tests for how the coordinator applies subscription updates."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bryx911.api import (
    BryxApiError,
    BryxAuthError,
    BryxConnectionError,
)
from custom_components.bryx911.const import (
    CONF_AGENCIES,
    CONF_EXCLUDED_UNITS,
    MAX_TRACKED_JOBS,
)
from custom_components.bryx911.coordinator import BryxCoordinator
from custom_components.bryx911.models import BryxAgency, BryxUnit

from .job_payloads import CLOSED_JOB, OPEN_JOB, initial_loaded, job_update

PROFILE = {"id": "client-1", "fullName": "Test Member", "onDuty": True}


@pytest.fixture
async def coordinator(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> AsyncGenerator[BryxCoordinator]:
    """Return a coordinator wired to a config entry."""
    config_entry.add_to_hass(hass)
    coordinator = BryxCoordinator(hass, config_entry)
    yield coordinator
    await coordinator.async_shutdown()


async def test_apply_created_and_updated(coordinator: BryxCoordinator) -> None:
    """CREATED and UPDATED both store the job."""
    await coordinator._handle_payload(job_update(OPEN_JOB, "CREATED"))
    assert len(coordinator.data.jobs) == 1

    closed = dict(OPEN_JOB, disposition="CLOSED")
    await coordinator._handle_payload(job_update(closed, "UPDATED"))

    assert len(coordinator.data.jobs) == 1
    assert coordinator.data.jobs[OPEN_JOB["id"]].disposition == "CLOSED"
    assert coordinator.data.open_jobs == []


async def test_apply_deleted_removes_job(coordinator: BryxCoordinator) -> None:
    """DELETED means the job left the filter set, so stop tracking it."""
    await coordinator._handle_payload(job_update(OPEN_JOB, "CREATED"))
    await coordinator._handle_payload(job_update(OPEN_JOB, "DELETED"))

    assert coordinator.data.jobs == {}


async def test_initial_data_loaded_sets_flag(coordinator: BryxCoordinator) -> None:
    """The sentinel marks the replay as finished."""
    assert not coordinator.data.initial_load_complete

    await coordinator._handle_payload(initial_loaded(2))

    assert coordinator.data.initial_load_complete


async def test_unknown_payload_ignored(coordinator: BryxCoordinator) -> None:
    """Unrecognized payloads are dropped without raising."""
    await coordinator._handle_payload({})
    await coordinator._handle_payload({"jobs": {"__typename": "SomethingElse"}})
    await coordinator._handle_payload({"jobs": {"__typename": "JobUpdateNode"}})
    await coordinator._handle_payload(
        {"jobs": {"__typename": "JobUpdateNode", "type": "UPDATED", "job": {}}}
    )

    assert coordinator.data.jobs == {}


async def test_agency_filter_drops_other_agencies(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """With agencies configured, jobs from other agencies are ignored."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_AGENCIES: [CLOSED_JOB["agency"]["id"]]}
    )
    coordinator = BryxCoordinator(hass, config_entry)

    await coordinator._handle_payload(job_update(OPEN_JOB, "CREATED"))
    await coordinator._handle_payload(job_update(CLOSED_JOB, "CREATED"))

    assert list(coordinator.data.jobs) == [CLOSED_JOB["id"]]
    await coordinator.async_shutdown()


async def test_no_agency_filter_keeps_everything(coordinator: BryxCoordinator) -> None:
    """With no agencies configured, every agency is tracked."""
    await coordinator._handle_payload(job_update(OPEN_JOB, "CREATED"))
    await coordinator._handle_payload(job_update(CLOSED_JOB, "CREATED"))

    assert len(coordinator.data.jobs) == 2


async def test_trim_keeps_open_jobs(coordinator: BryxCoordinator) -> None:
    """Trimming drops the oldest closed jobs but never an open one."""
    for index in range(MAX_TRACKED_JOBS + 20):
        node = dict(
            CLOSED_JOB,
            id=f"closed-{index}",
            # Ascending timestamps, so the earliest are the oldest.
            ts=str(1_600_000_000_000 + index * 1000),
        )
        await coordinator._handle_payload(job_update(node, "CREATED"))

    # An open job older than everything else must survive.
    oldest_open = dict(OPEN_JOB, id="open-1", ts="1500000000000")
    await coordinator._handle_payload(job_update(oldest_open, "CREATED"))

    assert len(coordinator.data.jobs) <= MAX_TRACKED_JOBS + 1
    assert "open-1" in coordinator.data.jobs


async def test_setup_fetches_units_for_tracked_agencies(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_subscription
) -> None:
    """Units are fetched only for the agencies chosen in the options."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_AGENCIES: ["a2"]}
    )
    coordinator = BryxCoordinator(hass, config_entry)
    engine = BryxUnit(id="u1", name="Engine 1", agency_id="a2")
    get_units = AsyncMock(return_value=[engine])

    with (
        patch.object(
            coordinator.client,
            "async_get_agencies",
            new=AsyncMock(
                return_value=[
                    BryxAgency(id="a1", name="A"),
                    BryxAgency(id="a2", name="B"),
                ]
            ),
        ),
        patch.object(coordinator.client, "async_get_units", new=get_units),
        patch.object(coordinator.client, "async_update_device", new=AsyncMock()),
    ):
        await coordinator.async_setup()

    get_units.assert_awaited_once_with("a2")
    # Untracked agencies stay listed so the options flow can offer them.
    assert [agency.id for agency in coordinator.agencies] == ["a1", "a2"]
    assert [agency.id for agency in coordinator.tracked_agencies] == ["a2"]
    assert coordinator.tracked_units == [engine]
    await coordinator.async_shutdown()


async def test_agency_filter_keeps_jobs_for_member_units(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """A job from an untracked agency is kept if it dispatches a member's unit."""
    fire_id = CLOSED_JOB["agency"]["id"]
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_AGENCIES: [fire_id]}
    )
    coordinator = BryxCoordinator(hass, config_entry)
    coordinator.agencies = [
        BryxAgency(
            id=fire_id,
            name="Sample Fire Company",
            units=[BryxUnit(id="unit-engine-1", name="Engine 1", agency_id=fire_id)],
        )
    ]
    mutual_aid = dict(
        OPEN_JOB, id="job-mutual-aid", units=OPEN_JOB["units"] + CLOSED_JOB["units"]
    )

    await coordinator._handle_payload(job_update(OPEN_JOB, "CREATED"))
    await coordinator._handle_payload(job_update(mutual_aid, "CREATED"))

    assert list(coordinator.data.jobs) == ["job-mutual-aid"]
    await coordinator.async_shutdown()


async def test_excluded_units_are_not_tracked(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """A unit cleared in the options is dropped and no longer keeps jobs."""
    fire_id = CLOSED_JOB["agency"]["id"]
    engine = BryxUnit(id="unit-engine-1", name="Engine 1", agency_id=fire_id)
    ladder = BryxUnit(id="unit-ladder-1", name="Ladder 1", agency_id=fire_id)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={CONF_AGENCIES: [fire_id], CONF_EXCLUDED_UNITS: [engine.id]},
    )
    coordinator = BryxCoordinator(hass, config_entry)
    coordinator.agencies = [
        BryxAgency(id=fire_id, name="Sample Fire Company", units=[engine, ladder])
    ]

    assert coordinator.tracked_units == [ladder]
    # A sheriff's job that dispatches only the cleared engine is not kept.
    await coordinator._handle_payload(
        job_update(dict(OPEN_JOB, units=CLOSED_JOB["units"]), "CREATED")
    )
    assert coordinator.data.jobs == {}
    await coordinator.async_shutdown()


async def test_connection_callbacks(coordinator: BryxCoordinator) -> None:
    """Connecting and disconnecting update the reported state."""
    await coordinator._handle_connected()
    assert coordinator.data.connected

    await coordinator._handle_payload(initial_loaded(1))
    assert coordinator.data.initial_load_complete

    await coordinator._handle_disconnected()
    assert not coordinator.data.connected
    # A reconnect replays everything, so the flag must reset.
    assert not coordinator.data.initial_load_complete


async def test_setup_auth_failure_raises(coordinator: BryxCoordinator) -> None:
    """A rejected API key during setup asks for reauthentication."""
    with (
        patch.object(
            coordinator.client,
            "async_get_agencies",
            new=AsyncMock(side_effect=BryxAuthError("Not Authenticated")),
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await coordinator.async_setup()


async def test_set_duty_status(coordinator: BryxCoordinator) -> None:
    """Setting duty status stores what the API confirmed."""
    with patch.object(
        coordinator.client, "async_set_duty_status", new=AsyncMock(return_value=False)
    ) as mock:
        await coordinator.async_set_duty_status(False)

    mock.assert_awaited_once_with("client-1", False)
    assert coordinator.data.on_duty is False


async def test_keepalive_touches_the_device(coordinator: BryxCoordinator) -> None:
    """The hourly keepalive calls updateDevice with the stored device id."""
    with patch.object(
        coordinator.client, "async_update_device", new=AsyncMock()
    ) as mock:
        await coordinator._async_keepalive(dt_util.utcnow())

    mock.assert_awaited_once_with("device-1")


async def test_keepalive_auth_failure_starts_reauth(
    coordinator: BryxCoordinator,
) -> None:
    """A rejected keepalive asks for reauthentication.

    The API answers an unknown device id with "Not Authorized", so this also
    covers the device record having been removed from the account.
    """
    with (
        patch.object(
            coordinator.client,
            "async_update_device",
            new=AsyncMock(side_effect=BryxAuthError("Not Authorized")),
        ),
        patch.object(coordinator.config_entry, "async_start_reauth") as reauth,
    ):
        await coordinator._async_keepalive(dt_util.utcnow())

    reauth.assert_called_once()


@pytest.mark.parametrize(
    "error", [BryxConnectionError("offline"), BryxApiError("server said no")]
)
async def test_keepalive_transient_failure_is_swallowed(
    coordinator: BryxCoordinator, error
) -> None:
    """A transient keepalive failure is retried rather than escalated.

    Keepalive is housekeeping; a network blip must not knock the integration out
    or trigger a reauthentication prompt.
    """
    with (
        patch.object(
            coordinator.client, "async_update_device", new=AsyncMock(side_effect=error)
        ),
        patch.object(coordinator.config_entry, "async_start_reauth") as reauth,
    ):
        await coordinator._async_keepalive(dt_util.utcnow())

    reauth.assert_not_called()

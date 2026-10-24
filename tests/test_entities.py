"""Tests for the devices and entities the Bryx integration exposes."""

from collections.abc import AsyncGenerator
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_URL, STATE_OFF, STATE_ON, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.bryx911.api import (
    BryxApiError,
    BryxAuthError,
    BryxConnectionError,
)
from custom_components.bryx911.const import CONF_AGENCIES, DOMAIN
from custom_components.bryx911.models import BryxAgency, BryxResponseOption, BryxUnit

from .job_payloads import CLOSED_JOB, OPEN_JOB, initial_loaded, job_update

PROFILE = {"id": "client-1", "fullName": "Test Member", "onDuty": True}

SHERIFF_ID = OPEN_JOB["agency"]["id"]
FIRE_ID = CLOSED_JOB["agency"]["id"]

AGENCIES = [
    BryxAgency(
        id=SHERIFF_ID,
        name="Example County Sheriff's Office",
        response_options=[
            BryxResponseOption(id="1", text="Yes", is_positive=True),
            BryxResponseOption(id="2", text="No", is_positive=False),
        ],
    ),
    BryxAgency(
        id=FIRE_ID,
        name="Sample Fire Company",
        response_options=[
            BryxResponseOption(id="3", text="To Scene", is_positive=True),
            BryxResponseOption(id="4", text="No", is_positive=False),
        ],
    ),
]

# The member's units, matching the units the fixture jobs list.
UNITS = {
    SHERIFF_ID: [BryxUnit(id="unit-patrol-1", name="Patrol 1", agency_id=SHERIFF_ID)],
    FIRE_ID: [BryxUnit(id="unit-engine-1", name="Engine 1", agency_id=FIRE_ID)],
}

# Entity id prefixes, from the device names.
ACCOUNT = "test_member"
SHERIFF = "example_county_sheriff_s_office"
FIRE = "sample_fire_company"
PATROL = "patrol_1"
ENGINE = "engine_1"

UNIT_SENSOR_KEYS = (
    "latest_job",
    "latest_job_type",
    "latest_job_location",
    "latest_job_time",
    "open_jobs",
    "supplementals",
)


@pytest.fixture
def mock_update_device():
    """Stub the updateDevice call that setup and the keepalive timer send."""
    with patch(
        "custom_components.bryx911.coordinator.BryxClient.async_update_device",
        new=AsyncMock(),
    ) as mock:
        yield mock


@pytest.fixture
def mock_api(mock_update_device):
    """Stub the API calls that setting up the entry makes."""
    with (
        patch(
            "custom_components.bryx911.coordinator.BryxClient.async_get_profile",
            new=AsyncMock(return_value=PROFILE),
        ),
        patch(
            "custom_components.bryx911.coordinator.BryxClient.async_get_agencies",
            new=AsyncMock(return_value=AGENCIES),
        ),
        patch(
            "custom_components.bryx911.coordinator.BryxClient.async_get_units",
            new=AsyncMock(side_effect=lambda agency_id: UNITS[agency_id]),
        ),
    ):
        yield


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_subscription, mock_api
) -> AsyncGenerator[MockConfigEntry]:
    """Set up the integration with the network calls stubbed out."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry


async def _push(hass: HomeAssistant, entry: MockConfigEntry, *payloads) -> None:
    """Feed subscription payloads through the coordinator."""
    coordinator = entry.runtime_data
    for payload in payloads:
        await coordinator._handle_payload(payload)
    await coordinator._handle_connected()
    # The debouncer coalesces bursts, so flush it before asserting.
    await coordinator._async_publish()
    await hass.async_block_till_done()


def _device_name(hass: HomeAssistant, entity_id: str) -> str:
    """Return the name of the device an entity belongs to."""
    entity = er.async_get(hass).async_get(entity_id)
    return dr.async_get(hass).async_get(entity.device_id).name


async def test_setup_and_unload(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The entry sets up and unloads cleanly."""
    assert setup_integration.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize(
    ("method", "error"),
    [
        ("async_get_agencies", BryxConnectionError("offline")),
        ("async_get_agencies", BryxApiError("server said no")),
        ("async_get_units", BryxConnectionError("offline")),
        ("async_get_profile", BryxConnectionError("offline")),
    ],
)
async def test_api_failure_during_setup_is_retried(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_subscription,
    mock_api,
    method: str,
    error: Exception,
) -> None:
    """A failing or unreachable API leaves the entry for Home Assistant to retry."""
    config_entry.add_to_hass(hass)
    with patch(
        f"custom_components.bryx911.coordinator.BryxClient.{method}",
        new=AsyncMock(side_effect=error),
    ):
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_recovers_once_the_api_answers(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_subscription, mock_api
) -> None:
    """The retry Home Assistant schedules succeeds once the API is back."""
    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.bryx911.coordinator.BryxClient.async_get_agencies",
        new=AsyncMock(side_effect=BryxConnectionError("offline")),
    ):
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY

    # Past the longest retry delay Home Assistant uses. The retry runs as a
    # background task, so wait for those too.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert config_entry.state is ConfigEntryState.LOADED


async def test_rejected_key_during_setup_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_subscription, mock_api
) -> None:
    """A rejected key asks for reauthentication instead of retrying forever."""
    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.bryx911.coordinator.BryxClient.async_get_agencies",
        new=AsyncMock(side_effect=BryxAuthError("Not Authenticated")),
    ):
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_device_hierarchy(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Units sit under their agency, and agencies under the account."""
    device_registry = dr.async_get(hass)
    entry_id = setup_integration.entry_id

    def device(identifier: str) -> dr.DeviceEntry:
        found = device_registry.async_get_device_by_identifier(
            (DOMAIN, identifier), entry_id
        )
        assert found is not None
        return found

    account = device(entry_id)
    sheriff = device(f"{entry_id}_agency_{SHERIFF_ID}")
    fire = device(f"{entry_id}_agency_{FIRE_ID}")
    patrol = device(f"{entry_id}_unit_unit-patrol-1")
    engine = device(f"{entry_id}_unit_unit-engine-1")

    assert account.name == "Test Member"
    assert account.via_device_id is None
    assert (sheriff.name, sheriff.via_device_id) == (
        "Example County Sheriff's Office",
        account.id,
    )
    assert (fire.name, fire.via_device_id) == ("Sample Fire Company", account.id)
    assert (patrol.name, patrol.via_device_id) == ("Patrol 1", sheriff.id)
    assert (engine.name, engine.via_device_id) == ("Engine 1", fire.id)
    assert len(dr.async_entries_for_config_entry(device_registry, entry_id)) == 5


async def test_entities_sit_on_their_devices(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Duty and connection stay on the account; agencies and units get the rest."""
    assert _device_name(hass, f"switch.{ACCOUNT}_on_duty") == "Test Member"
    assert _device_name(hass, f"binary_sensor.{ACCOUNT}_connection") == "Test Member"
    assert (
        _device_name(hass, f"calendar.{SHERIFF}_jobs")
        == "Example County Sheriff's Office"
    )
    assert (
        _device_name(hass, f"sensor.{FIRE}_to_scene_responders")
        == "Sample Fire Company"
    )
    assert _device_name(hass, f"binary_sensor.{ENGINE}_active_job") == "Engine 1"
    for key in UNIT_SENSOR_KEYS:
        assert _device_name(hass, f"sensor.{ENGINE}_{key}") == "Engine 1"

    entries = er.async_entries_for_config_entry(
        er.async_get(hass), setup_integration.entry_id
    )
    assert len({entry.unique_id for entry in entries}) == len(entries)
    # The account's two, each agency's two responder counts and calendar, and
    # each unit's seven.
    assert len(entries) == 2 + 2 * 3 + 2 * 7
    assert {entry.domain for entry in entries} == {
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.SWITCH,
        Platform.CALENDAR,
    }


async def test_responder_sensors_come_from_each_agency(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Each agency gets one counter per response option it offers."""
    responders = {
        entity_id
        for entity_id in hass.states.async_entity_ids(Platform.SENSOR)
        if entity_id.endswith("_responders")
    }

    # "No" appears in both agencies, and each gets its own counter.
    assert responders == {
        f"sensor.{SHERIFF}_yes_responders",
        f"sensor.{SHERIFF}_no_responders",
        f"sensor.{FIRE}_to_scene_responders",
        f"sensor.{FIRE}_no_responders",
    }


async def test_states_with_no_jobs(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Before any job arrives the unit sensors are idle."""
    await _push(hass, setup_integration)

    for unit in (PATROL, ENGINE):
        assert hass.states.get(f"binary_sensor.{unit}_active_job").state == STATE_OFF
        assert hass.states.get(f"sensor.{unit}_open_jobs").state == "0"
        assert hass.states.get(f"sensor.{unit}_latest_job").state == STATE_UNKNOWN


async def test_open_job_drives_its_units_sensors(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """An open job shows on the unit it lists, and on no other unit."""
    await _push(hass, setup_integration, job_update(OPEN_JOB, "CREATED"))

    assert hass.states.get(f"binary_sensor.{PATROL}_active_job").state == STATE_ON
    assert hass.states.get(f"sensor.{PATROL}_open_jobs").state == "1"

    latest = hass.states.get(f"sensor.{PATROL}_latest_job")
    assert latest.state == "SMOKE ALARM SOUNDING"
    assert latest.attributes["job_id"] == OPEN_JOB["id"]
    assert latest.attributes["agency"] == "Example County Sheriff's Office"
    assert latest.attributes["latitude"] == pytest.approx(39.654321)
    assert latest.attributes["longitude"] == pytest.approx(-70.123456)
    assert latest.attributes["disposition"] == "OPEN"

    assert (
        hass.states.get(f"sensor.{PATROL}_latest_job_location").state
        == "100 EXAMPLE AVE, ANYTOWN"
    )
    assert hass.states.get(f"sensor.{PATROL}_latest_job_type").state == "RESIDENTIAL"
    assert (
        hass.states.get(f"sensor.{PATROL}_latest_job_time").state
        == "2026-10-01T14:30:00+00:00"
    )

    # The fire company's engine is not on this job.
    assert hass.states.get(f"binary_sensor.{ENGINE}_active_job").state == STATE_OFF
    assert hass.states.get(f"sensor.{ENGINE}_latest_job").state == STATE_UNKNOWN


async def test_job_from_another_agency_shows_on_listed_unit(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A unit follows every job that lists it, whichever agency created it."""
    mutual_aid = dict(
        OPEN_JOB, id="job-mutual-aid", units=OPEN_JOB["units"] + CLOSED_JOB["units"]
    )
    await _push(hass, setup_integration, job_update(mutual_aid, "CREATED"))

    for unit in (PATROL, ENGINE):
        assert hass.states.get(f"binary_sensor.{unit}_active_job").state == STATE_ON
        latest = hass.states.get(f"sensor.{unit}_latest_job")
        assert latest.attributes["job_id"] == "job-mutual-aid"


async def test_closing_a_job_clears_active(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A job moving to CLOSED turns the unit's active-job flag off."""
    await _push(hass, setup_integration, job_update(OPEN_JOB, "CREATED"))
    assert hass.states.get(f"binary_sensor.{PATROL}_active_job").state == STATE_ON

    await _push(
        hass, setup_integration, job_update(dict(OPEN_JOB, disposition="CLOSED"))
    )

    assert hass.states.get(f"binary_sensor.{PATROL}_active_job").state == STATE_OFF
    assert hass.states.get(f"sensor.{PATROL}_open_jobs").state == "0"
    # The job is still tracked for history even though it is not active.
    assert (
        hass.states.get(f"sensor.{PATROL}_latest_job").state == "SMOKE ALARM SOUNDING"
    )


async def test_responder_counts(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Responder sensors count responses on their agency's open jobs only."""
    open_with_responders = dict(CLOSED_JOB, disposition="OPEN")
    await _push(hass, setup_integration, job_update(open_with_responders, "CREATED"))

    to_scene = hass.states.get(f"sensor.{FIRE}_to_scene_responders")
    assert to_scene.state == "1"
    assert to_scene.attributes["responders"][0]["name"] == "Alex Doe"
    assert hass.states.get(f"sensor.{FIRE}_no_responders").state == "1"

    # The job is the fire company's, so the sheriff's counters stay at zero.
    assert hass.states.get(f"sensor.{SHERIFF}_no_responders").state == "0"
    assert hass.states.get(f"sensor.{SHERIFF}_yes_responders").state == "0"

    # Once closed, the job no longer contributes.
    await _push(hass, setup_integration, job_update(CLOSED_JOB))
    assert hass.states.get(f"sensor.{FIRE}_to_scene_responders").state == "0"


async def test_supplementals_sensor(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The supplementals sensor counts the notes on the unit's latest job."""
    await _push(hass, setup_integration, job_update(CLOSED_JOB, "CREATED"))

    state = hass.states.get(f"sensor.{ENGINE}_supplementals")
    assert state.state == "2"
    assert [item["text"] for item in state.attributes["supplementals"]] == [
        "NO EXPOSURES",
        "FIRE KNOCKED DOWN",
    ]


async def test_long_synopsis_truncated(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A synopsis longer than the state limit is truncated, not dropped.

    Live data has produced a 462 character synopsis, and Home Assistant refuses
    to store a state longer than 255 characters.
    """
    long_text = "X" * 462
    await _push(
        hass,
        setup_integration,
        job_update(dict(OPEN_JOB, synopsis=long_text), "CREATED"),
    )

    state = hass.states.get(f"sensor.{PATROL}_latest_job")
    assert len(state.state) == 255
    assert state.state.endswith("…")
    # The full text survives as an attribute.
    assert state.attributes["synopsis"] == long_text


async def test_connection_sensor(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The connectivity sensor follows the subscription."""
    coordinator = setup_integration.runtime_data

    await _push(hass, setup_integration, initial_loaded(1))
    state = hass.states.get(f"binary_sensor.{ACCOUNT}_connection")
    assert state.state == STATE_ON
    assert state.attributes["initial_load_complete"] is True

    await coordinator._handle_disconnected()
    await coordinator._async_publish()
    await hass.async_block_till_done()

    assert hass.states.get(f"binary_sensor.{ACCOUNT}_connection").state == STATE_OFF


async def test_duty_switch(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The duty switch reflects and changes the member's status."""
    await _push(hass, setup_integration)
    assert hass.states.get(f"switch.{ACCOUNT}_on_duty").state == STATE_ON

    with patch(
        "custom_components.bryx911.coordinator.BryxClient.async_set_duty_status",
        new=AsyncMock(return_value=False),
    ) as mock:
        await hass.services.async_call(
            Platform.SWITCH,
            "turn_off",
            {"entity_id": f"switch.{ACCOUNT}_on_duty"},
            blocking=True,
        )

    mock.assert_awaited_once_with("client-1", False)
    assert hass.states.get(f"switch.{ACCOUNT}_on_duty").state == STATE_OFF


async def test_agency_calendars(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Each agency's calendar reports its own jobs as events."""
    # A calendar's state depends on whether an event covers the current moment,
    # and the payloads carry fixed timestamps. Shift the open job to just now so
    # the assertion does not depend on when the suite is run.
    now = dt_util.utcnow()
    active = dict(
        OPEN_JOB, ts=str(int((now - timedelta(minutes=5)).timestamp() * 1000))
    )

    await _push(
        hass,
        setup_integration,
        job_update(active, "CREATED"),
        job_update(CLOSED_JOB, "CREATED"),
    )

    sheriff = hass.states.get(f"calendar.{SHERIFF}_jobs")
    assert sheriff.state == STATE_ON
    assert sheriff.attributes["message"] == "RESIDENTIAL - SMOKE ALARM SOUNDING"
    assert sheriff.attributes["location"] == "100 EXAMPLE AVE, ANYTOWN"
    # The fire company's only job is closed, so nothing is under way.
    assert hass.states.get(f"calendar.{FIRE}_jobs").state == STATE_OFF

    calendars = hass.data["domain_entities"]["calendar"]
    start = now - timedelta(days=365 * 5)
    end = now + timedelta(days=365 * 5)
    sheriff_events = await calendars[f"calendar.{SHERIFF}_jobs"].async_get_events(
        hass, start, end
    )
    fire_events = await calendars[f"calendar.{FIRE}_jobs"].async_get_events(
        hass, start, end
    )

    assert [event.uid for event in sheriff_events] == [OPEN_JOB["id"]]
    assert [event.uid for event in fire_events] == [CLOSED_JOB["id"]]
    # A closed job ends when it was last modified rather than after a fixed hour.
    (closed,) = fire_events
    assert closed.end > closed.start
    assert closed.end.isoformat() == "2026-10-01T14:20:30.250000+00:00"

    # Nothing overlaps a window before any of them started.
    old = await calendars[f"calendar.{FIRE}_jobs"].async_get_events(
        hass, start, now - timedelta(days=365 * 4)
    )
    assert old == []


async def test_untracked_agency_devices_removed(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Narrowing the agencies removes the others' devices and entities."""
    hass.config_entries.async_update_entry(
        setup_integration, options={CONF_AGENCIES: [SHERIFF_ID]}
    )
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()

    devices = dr.async_entries_for_config_entry(
        dr.async_get(hass), setup_integration.entry_id
    )
    assert {device.name for device in devices} == {
        "Test Member",
        "Example County Sheriff's Office",
        "Patrol 1",
    }
    entity_registry = er.async_get(hass)
    assert entity_registry.async_get(f"sensor.{ENGINE}_latest_job") is None
    assert entity_registry.async_get(f"calendar.{FIRE}_jobs") is None
    assert entity_registry.async_get(f"sensor.{PATROL}_latest_job") is not None


async def test_migration_removes_account_job_entities(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_subscription, mock_api
) -> None:
    """Upgrading from version 1 drops the old account-wide job entities.

    The duty switch and connection sensor stay, so their history is kept.
    """
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, version=1)
    entity_registry = er.async_get(hass)
    old_entities = {
        "latest_job": Platform.SENSOR,
        "responders_yes": Platform.SENSOR,
        "active_job": Platform.BINARY_SENSOR,
        "calendar": Platform.CALENDAR,
        "connection": Platform.BINARY_SENSOR,
        "on_duty": Platform.SWITCH,
    }
    for key, platform in old_entities.items():
        entity_registry.async_get_or_create(
            platform,
            DOMAIN,
            f"{config_entry.entry_id}_{key}",
            config_entry=config_entry,
        )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.version == 2
    keys = {
        entry.unique_id.removeprefix(f"{config_entry.entry_id}_")
        for entry in er.async_entries_for_config_entry(
            entity_registry, config_entry.entry_id
        )
    }
    assert {"connection", "on_duty"} <= keys
    assert keys.isdisjoint({"latest_job", "responders_yes", "active_job", "calendar"})


@pytest.mark.parametrize(
    ("environment", "url"),
    [
        ("production", "https://graph.bryx911.com"),
        ("development", "https://graph.dev.bryx911.com"),
        (None, "https://graph.bryx911.com"),
    ],
)
async def test_migration_stores_the_server_url(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_subscription,
    mock_api,
    environment: str | None,
    url: str,
) -> None:
    """Version 2.1 named an environment, which becomes the server's URL."""
    data = {key: value for key, value in config_entry.data.items() if key != CONF_URL}
    if environment is not None:
        data["environment"] = environment
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, data=data, minor_version=1)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.minor_version == 2
    assert config_entry.data[CONF_URL] == url
    assert "environment" not in config_entry.data


async def test_entities_stay_available_when_poll_fails(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A failed duty-status poll must not black out live job data.

    Jobs arrive over the websocket, so while that is connected the job entities
    still hold current data even if the periodic profile request fails.
    """
    await _push(hass, setup_integration, job_update(OPEN_JOB, "CREATED"))
    coordinator = setup_integration.runtime_data
    assert coordinator.data.connected

    with patch(
        "custom_components.bryx911.coordinator.BryxClient.async_get_profile",
        new=AsyncMock(side_effect=BryxConnectionError("network blip")),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    state = hass.states.get(f"sensor.{PATROL}_latest_job")
    assert state.state == "SMOKE ALARM SOUNDING"


async def test_entities_unavailable_when_both_sources_fail(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """With the websocket down and the poll failing, entities go unavailable."""
    await _push(hass, setup_integration, job_update(OPEN_JOB, "CREATED"))
    coordinator = setup_integration.runtime_data
    await coordinator._handle_disconnected()

    with patch(
        "custom_components.bryx911.coordinator.BryxClient.async_get_profile",
        new=AsyncMock(side_effect=BryxConnectionError("network blip")),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get(f"sensor.{PATROL}_latest_job").state == "unavailable"
    # The connectivity sensor still reports, so the cause stays visible.
    assert hass.states.get(f"binary_sensor.{ACCOUNT}_connection").state == STATE_OFF


async def test_duty_poll_runs_on_its_own_timer(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The duty-status poll must not share the coordinator's refresh timer.

    Every job push calls async_set_updated_data, which resets that timer. If the
    poll used it, a feed busier than the poll interval would starve it forever,
    so the poll owns a separate timer and the coordinator has no interval.
    """
    coordinator = setup_integration.runtime_data

    assert coordinator.update_interval is None
    assert coordinator._unsub_poll is not None

    profile = AsyncMock(
        return_value={"id": "client-1", "fullName": "Test Member", "onDuty": False}
    )
    with patch(
        "custom_components.bryx911.coordinator.BryxClient.async_get_profile",
        new=profile,
    ):
        # A burst of job pushes must not stop the next poll from doing its work.
        for index in range(5):
            await coordinator._handle_payload(
                job_update(dict(OPEN_JOB, id=f"job-{index}"), "CREATED")
            )
            await coordinator._async_publish()
        await coordinator._async_poll(dt_util.utcnow())
        await hass.async_block_till_done()

    profile.assert_awaited_once()
    assert hass.states.get(f"switch.{ACCOUNT}_on_duty").state == STATE_OFF


async def test_duty_poll_timer_released_on_unload(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Unloading the entry cancels the duty-poll timer."""
    coordinator = setup_integration.runtime_data
    assert coordinator._unsub_poll is not None

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    assert coordinator._unsub_poll is None


async def test_old_job_still_counts_as_active(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """An OLD job is an aged open job, so it keeps the active-job flag on."""
    await _push(
        hass,
        setup_integration,
        job_update(dict(OPEN_JOB, disposition="OLD"), "UPDATED"),
    )

    assert hass.states.get(f"binary_sensor.{PATROL}_active_job").state == STATE_ON
    assert hass.states.get(f"sensor.{PATROL}_open_jobs").state == "1"


async def test_timed_out_job_is_not_active(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A TIMED_OUT job is finished, but stays tracked for history."""
    await _push(
        hass,
        setup_integration,
        job_update(dict(OPEN_JOB, disposition="TIMED_OUT"), "UPDATED"),
    )

    assert hass.states.get(f"binary_sensor.{PATROL}_active_job").state == STATE_OFF
    assert hass.states.get(f"sensor.{PATROL}_open_jobs").state == "0"
    # Still available to the calendar and the latest-job sensors.
    assert (
        hass.states.get(f"sensor.{PATROL}_latest_job").state == "SMOKE ALARM SOUNDING"
    )


async def test_responders_counted_on_old_jobs(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Responses on an OLD job still count, because the job is still open."""
    await _push(
        hass,
        setup_integration,
        job_update(dict(CLOSED_JOB, disposition="OLD"), "UPDATED"),
    )

    assert hass.states.get(f"sensor.{FIRE}_to_scene_responders").state == "1"
    assert hass.states.get(f"sensor.{FIRE}_no_responders").state == "1"


async def test_device_keepalive_timer_registered_and_released(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The keepalive runs on its own hourly timer and stops on unload."""
    from custom_components.bryx911.const import DEVICE_KEEPALIVE_INTERVAL

    coordinator = setup_integration.runtime_data
    assert coordinator._unsub_keepalive is not None
    assert timedelta(hours=1) == DEVICE_KEEPALIVE_INTERVAL

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    assert coordinator._unsub_keepalive is None


async def test_device_keepalive_fires_on_schedule(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_update_device
) -> None:
    """UpdateDevice is sent at setup, then once per hour of elapsed time."""
    from custom_components.bryx911.const import DEVICE_KEEPALIVE_INTERVAL

    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_update_device.await_count == 1
    for hour in range(1, 4):
        async_fire_time_changed(
            hass,
            dt_util.utcnow() + DEVICE_KEEPALIVE_INTERVAL * hour + timedelta(minutes=1),
        )
        await hass.async_block_till_done()

    assert mock_update_device.await_count == 4
    assert mock_update_device.await_args.args == ("device-1",)


async def test_setup_sends_the_device_description(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_update_device
) -> None:
    """Setup sends updateDevice straight away rather than an hour later."""
    await hass.async_block_till_done(wait_background_tasks=True)

    mock_update_device.assert_awaited_once_with("device-1")


async def test_reload_sends_the_device_description_again(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_update_device
) -> None:
    """A reload restarts the hourly timer, so it sends updateDevice again."""
    assert await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_update_device.await_count == 2


@pytest.mark.parametrize(
    ("error", "flow_sources"),
    [
        (BryxAuthError("Not Authorized"), [SOURCE_REAUTH]),
        (BryxConnectionError("offline"), []),
        (BryxApiError("server said no"), []),
    ],
)
async def test_failed_device_keepalive_at_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_subscription,
    mock_api,
    mock_update_device,
    error: Exception,
    flow_sources: list[str],
) -> None:
    """A failed send at setup still leaves the entry loaded.

    A refusal asks for reauthentication. Any other failure waits for the next
    hourly send.
    """
    mock_update_device.side_effect = error
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert config_entry.state is ConfigEntryState.LOADED
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == flow_sources

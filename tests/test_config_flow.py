"""Tests for the Bryx 911 config flow."""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, FlowResultType, section
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bryx911.api import (
    BryxApiError,
    BryxAuthError,
    BryxConnectionError,
)
from custom_components.bryx911.const import (
    CONF_ADVANCED,
    CONF_AGENCIES,
    CONF_API_KEY,
    CONF_CLIENT_ID,
    CONF_DEVICE_ID,
    CONF_EXCLUDED_UNITS,
    CONF_HARDWARE_ID,
    CONF_UNITS,
    DEFAULT_URL,
    DOMAIN,
)
from custom_components.bryx911.models import BryxAgency, BryxUnit

USER_INPUT = {
    CONF_EMAIL: "member@example.com",
    CONF_PASSWORD: "hunter2",
    CONF_ADVANCED: {CONF_URL: DEFAULT_URL},
}

PROFILE = {"id": "client-1", "fullName": "Test Member", "email": "member@example.com"}

DEV_URL = "https://graph.dev.bryx911.com"


@pytest.fixture
def mock_setup_entry():
    """Keep a newly created entry from being set up against the real API."""
    with patch(
        "custom_components.bryx911.async_setup_entry", return_value=True
    ) as mock:
        yield mock


@pytest.fixture
def mock_client():
    """Replace the flow's API client, so a test can see the URL it was given."""
    with patch(
        "custom_components.bryx911.config_flow.BryxClient", autospec=True
    ) as client_class:
        client = client_class.return_value
        client.async_create_device.return_value = ("api-key-1", "device-1")
        client.async_get_profile.return_value = PROFILE
        yield client_class


def _server_field(result: FlowResult) -> tuple[section, Any]:
    """Return the sign-in form's server section and its URL field."""
    schema = result["data_schema"].schema
    (key,) = (key for key in schema if key == CONF_ADVANCED)
    (url_key,) = schema[key].schema.schema
    return schema[key], url_key


def _suggested(result: FlowResult, field: str) -> Any:
    """Return the value the form suggests for a top-level field."""
    (key,) = (key for key in result["data_schema"].schema if key == field)
    return (key.description or {}).get("suggested_value")


def _patch_api(create_device=None, profile=None):
    """Patch the API calls the flow makes."""
    return (
        patch(
            "custom_components.bryx911.config_flow.BryxClient.async_create_device",
            new=AsyncMock(**create_device)
            if isinstance(create_device, dict)
            else AsyncMock(return_value=("api-key-1", "device-1")),
        ),
        patch(
            "custom_components.bryx911.config_flow.BryxClient.async_get_profile",
            new=AsyncMock(return_value=profile or PROFILE),
        ),
    )


async def test_user_flow_creates_entry(hass: HomeAssistant, mock_setup_entry) -> None:
    """A successful sign-in stores the API key and never the password."""
    create, profile = _patch_api()
    with create, profile:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test Member"
    assert result["data"][CONF_API_KEY] == "api-key-1"
    assert result["data"][CONF_DEVICE_ID] == "device-1"
    assert result["data"][CONF_CLIENT_ID] == "client-1"
    assert result["data"][CONF_EMAIL] == "member@example.com"
    assert result["data"][CONF_HARDWARE_ID]
    assert result["data"][CONF_URL] == DEFAULT_URL
    # The user chose not to persist the password.
    assert CONF_PASSWORD not in result["data"]
    assert result["result"].unique_id == "client-1"
    mock_setup_entry.assert_awaited_once()


@pytest.mark.parametrize(
    ("side_effect", "expected_error"),
    [
        (BryxAuthError("Not Authorized"), "invalid_auth"),
        (BryxConnectionError("boom"), "cannot_connect"),
        (BryxApiError("weird"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant, mock_setup_entry, side_effect, expected_error
) -> None:
    """Each failure shows an error and the form can then be resubmitted."""
    with patch(
        "custom_components.bryx911.config_flow.BryxClient.async_create_device",
        new=AsyncMock(side_effect=side_effect),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}

    create, profile = _patch_api()
    with create, profile:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_server_url_starts_hidden(hass: HomeAssistant) -> None:
    """The server URL sits in a collapsed section, set to production."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    advanced, url_key = _server_field(result)
    assert advanced.options["collapsed"]
    assert url_key.default() == DEFAULT_URL
    assert url_key.description is None


async def test_custom_server_url(
    hass: HomeAssistant, mock_setup_entry, mock_client
) -> None:
    """Another server is signed in to and stored without a trailing slash."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_ADVANCED: {CONF_URL: f" {DEV_URL}/ "}}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == DEV_URL
    assert mock_client.call_args.args[1] == f"{DEV_URL}/graphql"


async def test_flow_without_server_section_uses_production(
    hass: HomeAssistant, mock_setup_entry, mock_client
) -> None:
    """A flow started with only an email and password signs in to production."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={CONF_EMAIL: "member@example.com", CONF_PASSWORD: "hunter2"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == DEFAULT_URL
    assert mock_client.call_args.args[1] == f"{DEFAULT_URL}/graphql"


@pytest.mark.parametrize(
    "url",
    [
        "http://graph.bryx911.com",
        "graph.bryx911.com",
        "https://",
        "https://graph.bryx911.com/?debug=1",
        "https://graph.bryx911.com:99999",
    ],
)
async def test_invalid_server_url_shows_error(
    hass: HomeAssistant, mock_client, url: str
) -> None:
    """A URL that is not plain HTTPS is refused before the password is sent."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_ADVANCED: {CONF_URL: url}}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_url"}
    assert result["description_placeholders"] == {"default_url": DEFAULT_URL}
    mock_client.assert_not_called()
    # The form comes back open, with what was entered apart from the password.
    advanced, url_key = _server_field(result)
    assert not advanced.options["collapsed"]
    assert url_key.description == {"suggested_value": url}
    assert _suggested(result, CONF_EMAIL) == "member@example.com"
    assert _suggested(result, CONF_PASSWORD) is None


async def test_duplicate_account_aborts(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """The same Bryx account cannot be added twice."""
    config_entry.add_to_hass(hass)

    create, profile = _patch_api()
    with create, profile:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_api_key(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Reauth mints a new key and reuses the stored hardware id."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    create_mock = AsyncMock(return_value=("api-key-2", "device-1"))
    with (
        patch(
            "custom_components.bryx911.config_flow.BryxClient.async_create_device",
            new=create_mock,
        ),
        patch(
            "custom_components.bryx911.config_flow.BryxClient.async_get_profile",
            new=AsyncMock(return_value=PROFILE),
        ),
        patch("custom_components.bryx911.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_API_KEY] == "api-key-2"
    assert CONF_PASSWORD not in config_entry.data
    # Same hardware id, so the Bryx account keeps one device record.
    assert create_mock.call_args.args[2] == "hardware-1"


async def test_reauth_uses_the_stored_server(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client
) -> None:
    """Reauth signs in to the server the entry was set up with."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_URL: DEV_URL}
    )
    result = await config_entry.start_reauth_flow(hass)

    with patch("custom_components.bryx911.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert mock_client.call_args.args[1] == f"{DEV_URL}/graphql"


async def test_reauth_wrong_account_aborts(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Signing in as a different member during reauth is refused."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)

    with (
        patch(
            "custom_components.bryx911.config_flow.BryxClient.async_create_device",
            new=AsyncMock(return_value=("api-key-2", "device-2")),
        ),
        patch(
            "custom_components.bryx911.config_flow.BryxClient.async_get_profile",
            new=AsyncMock(return_value={"id": "someone-else", "fullName": "Other"}),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert config_entry.data[CONF_API_KEY] == "test-api-key"


async def test_reauth_invalid_password_shows_error(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """A bad password during reauth re-shows the form."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)

    with patch(
        "custom_components.bryx911.config_flow.BryxClient.async_create_device",
        new=AsyncMock(side_effect=BryxAuthError("Not Authorized")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "wrong"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


OPTION_AGENCIES = [
    BryxAgency(id="agency-a", name="Agency A"),
    BryxAgency(id="agency-b", name="Agency B"),
]

# Both agencies have an "Engine 1", which the picker has to tell apart.
OPTION_UNITS = {
    "agency-a": [
        BryxUnit(id="unit-1", name="Engine 1", agency_id="agency-a"),
        BryxUnit(id="unit-2", name="Engine 2", agency_id="agency-a"),
    ],
    "agency-b": [BryxUnit(id="unit-3", name="Engine 1", agency_id="agency-b")],
}


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Return the options the entry starts with."""
    return {}


@pytest.fixture
def member_units() -> dict[str, list[BryxUnit]]:
    """Return the member's units in each agency."""
    return OPTION_UNITS


@pytest.fixture
def get_units(member_units: dict[str, list[BryxUnit]]) -> AsyncMock:
    """Return the stub that fetches the member's units in one agency."""
    return AsyncMock(side_effect=lambda agency_id: member_units[agency_id])


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_subscription,
    entry_options: dict[str, Any],
    get_units: AsyncMock,
) -> AsyncGenerator[MockConfigEntry]:
    """Set up the entry, keeping the API stubbed for the reloads options cause."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options=entry_options)
    with (
        patch(
            "custom_components.bryx911.coordinator.BryxClient.async_get_profile",
            new=AsyncMock(return_value=PROFILE),
        ),
        patch(
            "custom_components.bryx911.coordinator.BryxClient.async_get_agencies",
            new=AsyncMock(return_value=OPTION_AGENCIES),
        ),
        patch(
            "custom_components.bryx911.coordinator.BryxClient.async_get_units",
            new=get_units,
        ),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        yield config_entry


async def _choose_agencies(
    hass: HomeAssistant, entry: MockConfigEntry, agencies: list[str]
) -> FlowResult:
    """Open the options and submit the agency step."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_AGENCIES: agencies}
    )


def _unit_picker(result: FlowResult) -> tuple[list[str], list[str]]:
    """Return the unit picker's labels and the units it suggests keeping."""
    assert result["step_id"] == "units"
    schema = result["data_schema"].schema
    (key,) = (key for key in schema if key == CONF_UNITS)
    labels = [option["label"] for option in schema[key].config["options"]]
    return labels, key.description["suggested_value"]


def _unit_device_ids(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    """Return the ids of the units that have a device."""
    return {
        identifier.rsplit("_unit_", 1)[1]
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        )
        for _, identifier in device.identifiers
        if "_unit_" in identifier
    }


async def test_changing_options_reloads_entry(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Saving a different selection reloads the entry to apply it."""
    coordinator = loaded_entry.runtime_data

    result = await _choose_agencies(hass, loaded_entry, ["agency-b"])
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UNITS: ["unit-3"]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded_entry.options == {
        CONF_AGENCIES: ["agency-b"],
        CONF_EXCLUDED_UNITS: [],
    }
    assert loaded_entry.state is ConfigEntryState.LOADED
    # Setting the entry up again builds a fresh coordinator.
    assert loaded_entry.runtime_data is not coordinator
    assert [agency.id for agency in loaded_entry.runtime_data.tracked_agencies] == [
        "agency-b"
    ]
    assert _unit_device_ids(hass, loaded_entry) == {"unit-3"}


async def test_unit_picker_starts_with_every_unit(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Every unit in the chosen agencies starts out selected, named by agency."""
    result = await _choose_agencies(hass, loaded_entry, [])

    labels, suggested = _unit_picker(result)
    assert labels == [
        "Engine 1 (Agency A)",
        "Engine 2 (Agency A)",
        "Engine 1 (Agency B)",
    ]
    assert suggested == ["unit-1", "unit-2", "unit-3"]


async def test_cleared_units_lose_their_devices(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """A unit cleared in the picker is saved as excluded and loses its device."""
    assert _unit_device_ids(hass, loaded_entry) == {"unit-1", "unit-2", "unit-3"}

    result = await _choose_agencies(hass, loaded_entry, [])
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UNITS: ["unit-1", "unit-3"]}
    )
    await hass.async_block_till_done()

    assert loaded_entry.options == {CONF_AGENCIES: [], CONF_EXCLUDED_UNITS: ["unit-2"]}
    assert _unit_device_ids(hass, loaded_entry) == {"unit-1", "unit-3"}
    # Opening the options again shows the same choice.
    result = await _choose_agencies(hass, loaded_entry, [])
    assert _unit_picker(result)[1] == ["unit-1", "unit-3"]


async def test_clearing_every_unit_keeps_only_the_agencies(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """An empty unit selection means no unit devices, not every unit."""
    result = await _choose_agencies(hass, loaded_entry, [])
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UNITS: []}
    )
    await hass.async_block_till_done()

    assert sorted(loaded_entry.options[CONF_EXCLUDED_UNITS]) == [
        "unit-1",
        "unit-2",
        "unit-3",
    ]
    assert _unit_device_ids(hass, loaded_entry) == set()


@pytest.mark.parametrize(
    "member_units",
    [
        {
            "agency-a": [
                BryxUnit(id="unit-1", name="Engine 1", agency_id="agency-a"),
                BryxUnit(id="unit-9", name="Engine 1", agency_id="agency-a"),
            ],
            "agency-b": [],
        }
    ],
)
async def test_same_named_units_show_their_ids(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Two units with the same name in one agency are told apart by id."""
    result = await _choose_agencies(hass, loaded_entry, [])

    assert _unit_picker(result)[0] == ["Engine 1 [unit-1]", "Engine 1 [unit-9]"]


@pytest.mark.parametrize("entry_options", [{CONF_AGENCIES: ["agency-a"]}])
async def test_newly_chosen_agency_units_are_fetched(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, get_units: AsyncMock
) -> None:
    """An agency the options newly pick has its units fetched for the picker."""
    get_units.assert_awaited_once_with("agency-a")

    result = await _choose_agencies(hass, loaded_entry, ["agency-a", "agency-b"])

    get_units.assert_awaited_with("agency-b")
    assert get_units.await_count == 2
    assert _unit_picker(result)[1] == ["unit-1", "unit-2", "unit-3"]


@pytest.mark.parametrize("entry_options", [{CONF_AGENCIES: ["agency-a"]}])
async def test_unit_fetch_failure_shows_error(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, get_units: AsyncMock
) -> None:
    """If a newly picked agency's units cannot load, the agency step says so."""
    get_units.side_effect = BryxConnectionError("offline")

    result = await _choose_agencies(hass, loaded_entry, ["agency-a", "agency-b"])

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize("member_units", [{"agency-a": [], "agency-b": []}])
async def test_units_step_skipped_without_units(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """With no units in the chosen agencies, saving the agencies finishes."""
    result = await _choose_agencies(hass, loaded_entry, ["agency-b"])
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded_entry.options == {
        CONF_AGENCIES: ["agency-b"],
        CONF_EXCLUDED_UNITS: [],
    }

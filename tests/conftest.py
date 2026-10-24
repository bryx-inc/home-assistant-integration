"""Shared fixtures for the Bryx 911 tests."""

from unittest.mock import patch

from homeassistant.const import CONF_EMAIL, CONF_URL
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bryx911.const import (
    CONF_API_KEY,
    CONF_CLIENT_ID,
    CONF_DEVICE_ID,
    CONF_HARDWARE_ID,
    DEFAULT_URL,
    DOMAIN,
)

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the custom integration in every test."""
    return


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a configured Bryx entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test Member",
        unique_id="client-1",
        version=2,
        minor_version=2,
        data={
            CONF_EMAIL: "member@example.com",
            CONF_API_KEY: "test-api-key",
            CONF_DEVICE_ID: "device-1",
            CONF_CLIENT_ID: "client-1",
            CONF_HARDWARE_ID: "hardware-1",
            CONF_URL: DEFAULT_URL,
        },
    )


@pytest.fixture
def mock_subscription():
    """Stop the websocket subscription from running during tests."""
    with patch(
        "custom_components.bryx911.coordinator.GraphQLSubscription.async_run",
        return_value=None,
    ) as mock:
        yield mock

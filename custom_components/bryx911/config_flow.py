"""Config flow for the Bryx 911 integration."""

from collections import Counter
from collections.abc import Mapping
import logging
from typing import TYPE_CHECKING, Any
import uuid

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_URL
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol
from yarl import URL

from .api import (
    BryxApiError,
    BryxAuthError,
    BryxClient,
    BryxConnectionError,
    BryxError,
    endpoint_urls,
)
from .const import (
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

if TYPE_CHECKING:
    # Imported for typing only. Keeping the coordinator out of this module's
    # runtime imports means a problem there cannot stop the config flow class
    # from registering, which Home Assistant reports only as "Invalid handler
    # specified".
    from .coordinator import BryxConfigEntry
    from .models import BryxUnit

_LOGGER = logging.getLogger(__name__)

_PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)

STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR})


def _user_schema(expanded: bool) -> vol.Schema:
    """Return the sign-in form.

    The server URL sits in a section that starts collapsed, because nearly
    everyone signs in to the production server.
    """
    return vol.Schema(
        {
            vol.Required(CONF_EMAIL): TextSelector(
                TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
            ),
            vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
            vol.Required(CONF_ADVANCED): section(
                vol.Schema(
                    {
                        vol.Required(CONF_URL, default=DEFAULT_URL): TextSelector(
                            TextSelectorConfig(type=TextSelectorType.URL)
                        )
                    }
                ),
                {"collapsed": not expanded},
            ),
        }
    )


def _normalize_url(url: str) -> str | None:
    """Return the server's base URL without a trailing slash.

    Returns None unless the URL is HTTPS with a host and no query or fragment.
    The sign-in sends the password, and every later request sends the API key.
    """
    try:
        parsed = URL(url.strip())
        usable = (
            parsed.scheme == "https"
            and bool(parsed.host)
            and not parsed.query_string
            and not parsed.fragment
        )
    except ValueError:
        return None
    if not usable:
        return None
    return str(parsed.with_path(parsed.path.rstrip("/")))


class BryxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Bryx 911 config flow."""

    VERSION = 2
    MINOR_VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in and register this Home Assistant instance as a device."""
        errors: dict[str, str] = {}
        url = DEFAULT_URL

        if user_input is not None:
            # A flow started with data rather than from the form has no section.
            url = user_input.get(CONF_ADVANCED, {}).get(CONF_URL, DEFAULT_URL)
            base_url = _normalize_url(url)
            if base_url is None:
                errors["base"] = "invalid_url"
            else:
                # A stable hardware id keeps repeated sign-ins tied to one device
                # record on the Bryx account rather than creating a new one each
                # time.
                hardware_id = str(uuid.uuid4())

                try:
                    api_key, device_id, profile = await self._async_authenticate(
                        base_url,
                        user_input[CONF_EMAIL],
                        user_input[CONF_PASSWORD],
                        hardware_id,
                    )
                except BryxAuthError:
                    errors["base"] = "invalid_auth"
                except BryxConnectionError:
                    errors["base"] = "cannot_connect"
                except BryxApiError:
                    _LOGGER.exception("Unexpected error from the Bryx API")
                    errors["base"] = "unknown"
                else:
                    await self.async_set_unique_id(profile["id"])
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=profile.get("fullName") or user_input[CONF_EMAIL],
                        data={
                            CONF_EMAIL: user_input[CONF_EMAIL],
                            CONF_API_KEY: api_key,
                            CONF_DEVICE_ID: device_id,
                            CONF_CLIENT_ID: profile["id"],
                            CONF_HARDWARE_ID: hardware_id,
                            CONF_URL: base_url,
                        },
                    )

        # Refill the form after an error, so a retry goes to the same server.
        # The section stays open while it names a server other than production.
        entered = {
            key: value
            for key, value in (user_input or {}).items()
            if key != CONF_PASSWORD
        }
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _user_schema(expanded=url != DEFAULT_URL), entered
            ),
            errors=errors,
            # Hassfest rejects URLs written into translation strings.
            description_placeholders={"default_url": DEFAULT_URL},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the stored API key stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again and mint a fresh API key."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            try:
                api_key, device_id, profile = await self._async_authenticate(
                    entry.data[CONF_URL],
                    entry.data[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    entry.data[CONF_HARDWARE_ID],
                )
            except BryxAuthError:
                errors["base"] = "invalid_auth"
            except BryxConnectionError:
                errors["base"] = "cannot_connect"
            except BryxApiError:
                _LOGGER.exception("Unexpected error from the Bryx API")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(profile["id"])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_API_KEY: api_key,
                        CONF_DEVICE_ID: device_id,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={CONF_EMAIL: entry.data[CONF_EMAIL]},
            errors=errors,
        )

    async def _async_authenticate(
        self, url: str, email: str, password: str, hardware_id: str
    ) -> tuple[str, str, dict[str, Any]]:
        """Sign in and return the API key, device id and profile."""
        graphql_url, _ = endpoint_urls(url)
        client = BryxClient(async_get_clientsession(self.hass), graphql_url)
        api_key, device_id = await client.async_create_device(
            email, password, hardware_id
        )
        profile = await client.async_get_profile()
        return api_key, device_id, profile

    @staticmethod
    @callback
    def async_get_options_flow(entry: BryxConfigEntry) -> BryxOptionsFlow:
        """Return the options flow."""
        return BryxOptionsFlow()


class BryxOptionsFlow(OptionsFlowWithReload):
    """Let the user narrow which agencies and units are tracked.

    Home Assistant reloads the entry when the saved options differ, which is
    what applies a new selection.
    """

    _agencies: list[str]
    _units: list[BryxUnit]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the agencies to track."""
        coordinator = self.config_entry.runtime_data
        errors: dict[str, str] = {}

        if user_input is not None:
            self._agencies = user_input.get(CONF_AGENCIES, [])
            try:
                # An empty selection tracks every agency.
                self._units = await coordinator.async_get_member_units(
                    set(self._agencies) or None
                )
            except BryxConnectionError:
                errors["base"] = "cannot_connect"
            except BryxError:
                _LOGGER.exception("Unexpected error loading units from the Bryx API")
                errors["base"] = "unknown"
            else:
                if not self._units:
                    return self._async_save(excluded=[])
                return await self.async_step_units()

        schema = vol.Schema(
            {
                vol.Optional(CONF_AGENCIES): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=agency.id, label=agency.name)
                            for agency in coordinator.agencies
                        ],
                        multiple=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        # Suggested rather than default values, so clearing a picker sticks.
        suggested = {CONF_AGENCIES: self.config_entry.options.get(CONF_AGENCIES, [])}
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_units(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which of the member's units in those agencies get a device."""
        if user_input is not None:
            kept = set(user_input.get(CONF_UNITS, []))
            return self._async_save(
                excluded=[unit.id for unit in self._units if unit.id not in kept]
            )

        coordinator = self.config_entry.runtime_data
        agency_names = {agency.id: agency.name for agency in coordinator.agencies}
        units = sorted(
            self._units,
            key=lambda unit: (agency_names.get(unit.agency_id, ""), unit.name),
        )
        # Unit names repeat across agencies, so name the agency when the list
        # spans more than one.
        several = len({unit.agency_id for unit in units}) > 1
        labels = {
            unit.id: (
                f"{unit.name} ({agency_names.get(unit.agency_id, '')})"
                if several
                else unit.name
            )
            for unit in units
        }
        # An agency can give two units the same name; their ids tell them apart.
        repeated = {
            label for label, count in Counter(labels.values()).items() if count > 1
        }
        options = [
            SelectOptionDict(
                value=unit.id,
                label=(
                    f"{labels[unit.id]} [{unit.id}]"
                    if labels[unit.id] in repeated
                    else labels[unit.id]
                ),
            )
            for unit in units
        ]

        schema = vol.Schema(
            {
                vol.Optional(CONF_UNITS): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        excluded = coordinator.excluded_units
        suggested = {CONF_UNITS: [unit.id for unit in units if unit.id not in excluded]}
        return self.async_show_form(
            step_id="units",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
        )

    @callback
    def _async_save(self, excluded: list[str]) -> ConfigFlowResult:
        """Save the agency selection and the units cleared from it."""
        return self.async_create_entry(
            data={CONF_AGENCIES: self._agencies, CONF_EXCLUDED_UNITS: excluded}
        )

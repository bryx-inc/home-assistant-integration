"""Coordinator for the Bryx 911 integration."""

import asyncio
from dataclasses import replace
from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    BryxApiError,
    BryxAuthError,
    BryxClient,
    BryxConnectionError,
    endpoint_urls,
)
from .const import (
    CONF_AGENCIES,
    CONF_API_KEY,
    CONF_CLIENT_ID,
    CONF_DEVICE_ID,
    CONF_EXCLUDED_UNITS,
    DEVICE_KEEPALIVE_INTERVAL,
    DOMAIN,
    MAX_TRACKED_JOBS,
    POLL_INTERVAL,
    TRACKED_DISPOSITIONS,
    UNIT_FETCH_CONCURRENCY,
    UPDATE_DEBOUNCE_SECONDS,
)
from .models import BryxAgency, BryxData, BryxJob, BryxUnit
from .queries import JOBS_SUBSCRIPTION
from .subscription import GraphQLSubscription

_LOGGER = logging.getLogger(__name__)

type BryxConfigEntry = ConfigEntry[BryxCoordinator]


class BryxCoordinator(DataUpdateCoordinator[BryxData]):
    """Keep job state in sync with the Bryx 911 API.

    Jobs arrive over a websocket subscription and are pushed into Home Assistant
    as they change. Two further timers run alongside it: one polls the signed-in
    member's duty status, which has no subscription of its own, and one sends
    the device description hourly, after a first send at setup, so Bryx keeps
    the device record.
    """

    config_entry: BryxConfigEntry

    def __init__(self, hass: HomeAssistant, entry: BryxConfigEntry) -> None:
        """Initialize the coordinator."""
        # No update_interval: job data is pushed, and every push calls
        # async_set_updated_data, which would otherwise keep resetting the
        # coordinator's own refresh timer so the duty-status poll never fired.
        # That poll runs on its own timer instead, started in async_setup.
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=None,
        )
        graphql_url, subscription_url = endpoint_urls(entry.data[CONF_URL])
        session = async_get_clientsession(hass)

        self.client = BryxClient(session, graphql_url, entry.data[CONF_API_KEY])
        self.client_id: str = entry.data[CONF_CLIENT_ID]
        self.device_id: str = entry.data[CONF_DEVICE_ID]
        self.agencies: list[BryxAgency] = []
        self.data = BryxData()

        self._subscription = GraphQLSubscription(
            session,
            subscription_url,
            JOBS_SUBSCRIPTION,
            {"params": {"dispositions": TRACKED_DISPOSITIONS}},
            on_payload=self._handle_payload,
            on_connected=self._handle_connected,
            on_disconnected=self._handle_disconnected,
        )
        self._subscription.set_api_key(entry.data[CONF_API_KEY])
        self._listen_task: asyncio.Task[None] | None = None
        self._unsub_poll: CALLBACK_TYPE | None = None
        self._unsub_keepalive: CALLBACK_TYPE | None = None

        # Job updates arrive in bursts; coalesce them into one state write.
        self._debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=UPDATE_DEBOUNCE_SECONDS,
            immediate=True,
            function=self._async_publish,
        )

    @property
    def selected_agencies(self) -> set[str] | None:
        """Return the agency ids to keep, or None to keep every agency."""
        configured = self.config_entry.options.get(CONF_AGENCIES)
        return set(configured) if configured else None

    @property
    def excluded_units(self) -> set[str]:
        """Return the ids of the units cleared in the options."""
        return set(self.config_entry.options.get(CONF_EXCLUDED_UNITS, []))

    @property
    def tracked_agencies(self) -> list[BryxAgency]:
        """Return the agencies that get a device, keeping only units that do too."""
        selected = self.selected_agencies
        excluded = self.excluded_units
        return [
            replace(
                agency,
                units=[unit for unit in agency.units if unit.id not in excluded],
            )
            for agency in self.agencies
            if selected is None or agency.id in selected
        ]

    @property
    def tracked_units(self) -> list[BryxUnit]:
        """Return the member's units in the tracked agencies."""
        return [unit for agency in self.tracked_agencies for unit in agency.units]

    async def async_setup(self) -> None:
        """Load the agencies and units, then start listening for job updates.

        The platforms need both before they can decide which devices and
        entities to create. The signed-in member's profile is left to the first
        refresh so it is only fetched once during setup.

        A rejected key starts reauthentication. Any other failure leaves the
        entry for Home Assistant to retry with backoff.
        """
        try:
            self.agencies = await self._async_get_agencies()
        except BryxAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (BryxConnectionError, BryxApiError) as err:
            raise ConfigEntryNotReady(
                f"Could not load the Bryx account: {err}"
            ) from err

        self._listen_task = self.config_entry.async_create_background_task(
            self.hass, self._async_listen(), f"{DOMAIN}-subscription"
        )
        self._unsub_poll = async_track_time_interval(
            self.hass, self._async_poll, POLL_INTERVAL, name=f"{DOMAIN}-duty-poll"
        )
        self._unsub_keepalive = async_track_time_interval(
            self.hass,
            self._async_keepalive,
            DEVICE_KEEPALIVE_INTERVAL,
            name=f"{DOMAIN}-device-keepalive",
        )
        # The timer first fires an hour from now, and restarts with every
        # reload. Send now as well, so Bryx shows the running versions straight
        # after a restart or upgrade.
        self.config_entry.async_create_background_task(
            self.hass, self._async_keepalive(), f"{DOMAIN}-device-keepalive-setup"
        )

    async def _async_get_agencies(self) -> list[BryxAgency]:
        """Return every agency, with the member's units on the tracked ones.

        Untracked agencies are still listed so the options flow can offer them.
        """
        selected = self.selected_agencies
        # One request per tracked agency, a few at a time, so a member of many
        # agencies neither waits on them one by one nor floods the API.
        semaphore = asyncio.Semaphore(UNIT_FETCH_CONCURRENCY)

        async def with_units(agency: BryxAgency) -> BryxAgency:
            if selected is not None and agency.id not in selected:
                return agency
            async with semaphore:
                units = await self.client.async_get_units(agency.id)
            return replace(agency, units=units)

        agencies = await self.client.async_get_agencies()
        return list(await asyncio.gather(*(with_units(agency) for agency in agencies)))

    async def async_get_member_units(
        self, agency_ids: set[str] | None
    ) -> list[BryxUnit]:
        """Return the member's units in these agencies, or in all of them for None.

        Setup only loads units for the tracked agencies, so an agency the options
        flow has just picked has its units fetched now.
        """
        selected = self.selected_agencies
        units: list[BryxUnit] = []
        for agency in self.agencies:
            if agency_ids is not None and agency.id not in agency_ids:
                continue
            if selected is None or agency.id in selected:
                units.extend(agency.units)
            else:
                units.extend(await self.client.async_get_units(agency.id))
        return units

    async def async_shutdown(self) -> None:
        """Stop listening and cancel pending work."""
        await self._subscription.async_close()
        if self._unsub_poll:
            self._unsub_poll()
            self._unsub_poll = None
        if self._unsub_keepalive:
            self._unsub_keepalive()
            self._unsub_keepalive = None
        if self._listen_task:
            self._listen_task.cancel()
            self._listen_task = None
        self._debouncer.async_shutdown()
        await super().async_shutdown()

    async def _async_poll(self, _now: datetime) -> None:
        """Refresh the duty status on its own schedule."""
        await self.async_refresh()

    async def _async_keepalive(self, _now: datetime | None = None) -> None:
        """Send the device description at setup and then hourly.

        Each updateDevice call pushes back the date Bryx deletes the device as
        unused, and replaces the name and versions Bryx shows for it. This is
        housekeeping, so a transient failure is logged and retried on the next
        tick rather than taken as the integration going down.
        """
        try:
            await self.client.async_update_device(self.device_id)
        except BryxAuthError as err:
            _LOGGER.warning(
                "Bryx rejected the device keepalive, reauthentication required: %s", err
            )
            self.config_entry.async_start_reauth(self.hass)
        except (BryxConnectionError, BryxApiError) as err:
            _LOGGER.debug("Bryx device keepalive failed, will retry: %s", err)
        else:
            _LOGGER.debug("Bryx device keepalive sent for device %s", self.device_id)

    async def _async_listen(self) -> None:
        """Run the subscription, turning auth failure into a reauth flow."""
        try:
            await self._subscription.async_run()
        except asyncio.CancelledError:
            raise
        except BryxAuthError as err:
            # This runs as a background task, so raising here would only log a
            # traceback. Start the reauth flow and stop listening instead.
            _LOGGER.warning(
                "Bryx rejected the stored API key, reauthentication required: %s", err
            )
            self.data.connected = False
            self.async_set_updated_data(self.data)
            self.config_entry.async_start_reauth(self.hass)

    async def _async_update_data(self) -> BryxData:
        """Refresh the duty status, which the subscription does not cover."""
        try:
            profile = await self.client.async_get_profile()
        except BryxAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (BryxConnectionError, BryxApiError) as err:
            raise UpdateFailed(f"Could not refresh the Bryx profile: {err}") from err

        self.data.on_duty = bool(profile.get("onDuty"))
        self.data.client_name = profile.get("fullName") or profile.get("email", "")
        return self.data

    async def async_set_duty_status(self, on_duty: bool) -> None:
        """Put the signed-in member on or off duty."""
        try:
            self.data.on_duty = await self.client.async_set_duty_status(
                self.client_id, on_duty
            )
        except BryxAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        self.async_set_updated_data(self.data)

    async def _handle_payload(self, payload: dict[str, Any]) -> None:
        """Apply one subscription payload to the job store."""
        update = payload.get("jobs")
        if not update:
            return

        typename = update.get("__typename")

        if typename == "InitialDataLoaded":
            # Everything already in the filter set has now been replayed.
            self.data.initial_load_complete = True
            _LOGGER.debug(
                "Bryx initial job load complete: %s job(s)",
                update.get("numberEntities"),
            )
            await self._debouncer.async_call()
            return

        if typename != "JobUpdateNode":
            return

        node = update.get("job")
        if not node or not node.get("id"):
            return

        try:
            job = BryxJob.from_node(node)
        except KeyError, TypeError, ValueError:
            _LOGGER.exception("Could not parse a Bryx job update")
            return

        # Keep jobs from tracked agencies, and jobs that dispatch one of the
        # member's units even though another agency created them.
        if (selected := self.selected_agencies) is not None and (
            job.agency_id not in selected
            and not any(unit.id in job.unit_ids for unit in self.tracked_units)
        ):
            return

        # DELETED means the job left the subscription's filter set rather than
        # that it was removed. Every disposition is tracked, so this should not
        # happen; handle it anyway so a job cannot get stuck if it ever does.
        if update.get("type") == "DELETED":
            self.data.jobs.pop(job.id, None)
        else:
            self.data.jobs[job.id] = job

        self._trim()
        await self._debouncer.async_call()

    def _trim(self) -> None:
        """Keep memory bounded by dropping the oldest closed jobs."""
        if len(self.data.jobs) <= MAX_TRACKED_JOBS:
            return
        for job in self.data.sorted_jobs[MAX_TRACKED_JOBS:]:
            if not job.is_open:
                self.data.jobs.pop(job.id, None)

    async def _handle_connected(self) -> None:
        """Mark the subscription as connected."""
        _LOGGER.debug("Bryx subscription connected")
        self.data.connected = True
        await self._debouncer.async_call()

    async def _handle_disconnected(self) -> None:
        """Mark the subscription as disconnected."""
        self.data.connected = False
        self.data.initial_load_complete = False
        await self._debouncer.async_call()

    async def _async_publish(self) -> None:
        """Write the current state out to the entities."""
        self.async_set_updated_data(self.data)

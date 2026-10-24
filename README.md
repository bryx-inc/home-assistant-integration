# Bryx 911 for Home Assistant

A custom integration that surfaces your Bryx 911 jobs, responders and duty
status in Home Assistant. It talks to the Bryx GraphQL API and receives job
updates over a websocket subscription, so state changes as soon as dispatch
does.

## What you get

Devices are nested account, then agency, then unit, and each level has its own
entities.

**Your account** is one device, named after you:

| Entity | What it reports |
| --- | --- |
| `switch.<you>_on_duty` | Your duty status |
| `binary_sensor.<you>_connection` | Whether the job subscription is connected (diagnostic) |

**Each tracked agency** is a device under your account:

| Entity | What it reports |
| --- | --- |
| `sensor.<agency>_<response>_responders` | How many members gave that response across the agency's open jobs |
| `calendar.<agency>_jobs` | The agency's tracked jobs as calendar events |

**Each of your units** in a tracked agency is a device under that agency,
unless you clear it in the options:

| Entity | What it reports |
| --- | --- |
| `binary_sensor.<unit>_active_job` | On while any job that lists the unit is open |
| `sensor.<unit>_latest_job` | Synopsis of the unit's most recent job; full job detail in its attributes |
| `sensor.<unit>_latest_job_type` | Job type description |
| `sensor.<unit>_latest_job_location` | Address, with `latitude` and `longitude` attributes |
| `sensor.<unit>_latest_job_time` | When the job was dispatched (timestamp) |
| `sensor.<unit>_open_jobs` | How many of the unit's jobs are open |
| `sensor.<unit>_supplementals` | Supplemental notes on the unit's latest job, with their text in attributes |

Your units are the ones you belong to in Bryx, read when the integration starts.
A job counts toward the agency that dispatched it. A unit's entities cover
every job you receive that lists the unit, whichever agency dispatched it.

Responder sensors are generated from the response options each agency actually
offers, so you get the wording that agency uses rather than a fixed list.

## Requirements

Home Assistant 2026.10 or newer. The integration targets the latest stable
release and is tested only against it. `hacs.json` declares the same minimum to
HACS.

## Installation

[![Open this repository in your Home Assistant instance.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bryx-inc&repository=home-assistant-integration&category=integration)

The badge opens this repository in your Home Assistant instance, adding it first
if needed. On Home Assistant 2026.11 and later it opens the Marketplace, which
needs a GitHub connection to add a repository from outside its catalog. On
2026.10 it opens HACS.

To add the repository to HACS without the badge, open HACS, choose **Custom
repositories** from the menu, and enter
`https://github.com/bryx-inc/home-assistant-integration` with the type
**Integration**. Then download **Bryx 911** and restart Home Assistant.

Home Assistant installs the integration's one Python dependency, `gql`, the first
time it loads the integration, so it needs internet access at that point.

### Manual installation

Copy `custom_components/bryx911` into your Home Assistant `config/custom_components`
directory and restart Home Assistant. The result must be
`config/custom_components/bryx911/manifest.json` — if you unzip an archive, check
you have not ended up with `custom_components/bryx911/bryx911/`.

## Setup

[![Open your Home Assistant instance and start setting up Bryx 911.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=bryx911)

Once the integration is installed, add it with the badge or from
**Settings → Devices & Services → Add Integration → Bryx 911**.

Sign in with your Bryx 911 email and password. Home Assistant registers itself
as a device on your account and stores the API key it receives. **Your password
is not stored.** If the key is ever rejected, Home Assistant prompts you to sign
in again.

The collapsed **Advanced** section under the password holds the server URL,
which is set to `https://graph.bryx911.com`. Leave it unchanged unless you were
told to use a different server.

If the UI says **"Config flow could not be loaded: Invalid handler specified"**,
that means `config_flow.py` failed to import and Home Assistant is hiding the
real error. Run the bundled diagnostic with the same Python that runs Home
Assistant to see it:

```bash
python3 scripts/diagnose.py /path/to/your/config
```

## Options

Use the integration's **Configure** button to choose what gets a device. It has
two steps:

1. Pick the agencies to track. Leaving the selection empty tracks every agency
   you belong to.
2. Pick your units in those agencies. Every unit starts out selected; clear the
   ones you don't want a device for. Clearing all of them leaves just the agency
   devices. A unit you join later is included automatically the next time the
   integration loads.

Saving a new selection reloads the integration. Agencies and units you stop
tracking are removed along with their entities.

The selection keeps the entity list manageable. Every tracked agency gets a
device with a responder sensor per response option and a calendar, and every
selected unit gets a device with seven entities. A member of many units can end
up with hundreds of entities if every unit stays selected.

## Notes on behavior

- A job counts as open while its disposition is `OPEN` or `OLD`. `OLD` is an
  open job that has aged, not a finished one. `CLOSED` and `TIMED_OUT` jobs are
  finished: they stay available for history and the calendar but do not count
  toward `active_job`, `open_jobs` or the responder counts.
- Job synopses can exceed Home Assistant's 255 character state limit. The sensor
  state is truncated; the full text is always in the `synopsis` attribute.
- Up to 250 jobs are kept in memory for history. Open jobs are never dropped.
- If the Bryx API cannot be reached while the integration is starting, Home
  Assistant retries in the background with growing delays rather than giving
  up. A rejected key prompts you to sign in again instead.
- Job data arrives over the websocket, so a failed duty-status poll does not
  mark the job entities unavailable.
- The integration calls `updateDevice` when it starts and once an hour after
  that. Each call keeps its device record on your Bryx account from expiring and
  reports the integration and Home Assistant versions. If that call is refused,
  Home Assistant prompts you to sign in again, which also covers the device
  having been removed from your account.

## Development

You need Python 3.14, which Home Assistant has required since 2026.3.

```bash
pip install -r requirements_test.txt
python -m pytest tests/ -q
ruff check custom_components/ tests/ scripts/
ruff format --check custom_components/ tests/ scripts/
```

`requirements_test.txt` pins `pytest-homeassistant-custom-component` 0.13.374,
which pins Home Assistant 2026.10.3, so the tests run against the release the
integration targets. When a new stable release ships, move to the plugin version
that pins it and raise the minimum in `hacs.json` to match.

Every push and pull request runs three GitHub Actions workflows: the tests and
lint above, hassfest, and the HACS validation the Marketplace relies on.
Hassfest and HACS validation also run daily, so a change on the Home Assistant
side shows up even without a commit.

The integration uses [gql](https://github.com/graphql-python/gql) for queries,
mutations and the websocket subscription. It is pinned to 4.0.0 because Home
Assistant constrains `graphql-core` below 3.3 and every later gql release
requires 3.3. Home Assistant's Tibber integration resolves to the same version.

### API details

These were verified against the live API and are worth knowing if you extend the
integration:

- Queries and mutations go to `https://graph.bryx911.com/graphql`.
- Subscriptions use a **separate** endpoint, `wss://graph.bryx911.com/subscriptions`.
  The `/graphql` path rejects websocket upgrades with HTTP 405.
- The `graphql-transport-ws` subprotocol is required. Connecting with the legacy
  `graphql-ws` subprotocol, or with none, is rejected with HTTP 400.
- Authentication uses the `X-API-KEY` header on HTTP. Over the websocket the key
  must travel in the `connection_init` payload as `{"X-API-KEY": "..."}`; the
  server ignores it on the upgrade request's headers.
- A missing or rejected key never produces an HTTP error status. Over HTTP the
  response is 200 with an `errors` entry ending `Not Authenticated`. Over the
  websocket, `connection_init` is acknowledged whatever the key, and the
  subscription is then rejected with an `error` message carrying the same text,
  followed by `complete`.
- `Instant` is serialized as a string of epoch milliseconds, for example
  `"1790865000000"`.
- `GeoPoint` and `GeoGeometry` are strings containing GeoJSON, so coordinates
  arrive longitude first and need a second parse.
- Paginated fields reject a page size outside `1..50`.
- `client.unitMemberships(filters: {agencyId: ...})` lists the units the member
  belongs to in one agency. Their ids match the `unitId` on a job's `units`, so
  the two can be compared directly.
- The `jobs` subscription replays every job already in its filter set as a
  `CREATED` update, then emits `InitialDataLoaded` as a sentinel, then streams
  live changes. There is no acknowledgment protocol.
- A `DELETED` update means the job **left the subscription's filter set**, not
  that it was removed. This integration subscribes to all four dispositions, so
  a job can never leave the set and every change arrives as `UPDATED`.
- The server does not send `graphql-transport-ws` pings, so the connection is
  kept alive with websocket-level ping frames.

## License

The code in this repository is released under the [MIT License](LICENSE).

The license covers the code, not the Bryx 911 service. The integration needs a
Bryx 911 account, and your use of the service is subject to your agreement with
Bryx. Agencies looking for station alerting should
[contact Bryx](https://bryx.com/contact-us) about Bryx Station Alerting or use
of this integration.

The Bryx name and logos, including the images in
`custom_components/bryx911/brand/`, are trademarks of Bryx, Inc. The MIT
License doesn't grant any rights to those trademarks.

The software comes with no warranty, so don't rely on it as your only way to
receive dispatches.

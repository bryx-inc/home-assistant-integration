"""GraphQL documents used by the Bryx 911 integration.

Each document is parsed when this module loads, so a syntax error fails at
import instead of on the first request.
"""

from typing import Final

from gql import gql

# Every field the entities read. Shared by the jobs query and the jobs
# subscription so both produce identically shaped payloads.
JOB_FRAGMENT: Final = """
fragment JobFields on JobNode {
  id
  ts
  creationTs
  lastModified
  disposition
  synopsis
  locationOfIncident
  centroid
  city
  state
  zip
  street
  crossStreets
  box
  priority
  incidentId
  cadId
  municipality
  locationInfo
  delayInSeconds
  assignment
  type {
    id
    description
    category
    section
  }
  agency {
    id
    name
  }
  units {
    unitId
    unit {
      id
      name
    }
  }
  radioChannels {
    channel
    description
  }
  responders {
    verboseResponse
    isPositive
    responseTime
    client {
      id
      fullName
      initials
    }
  }
  supplementals {
    id
    text
    ts
  }
  currentResponse {
    verboseResponse
    isPositive
    responseTime
  }
}
"""

CREATE_DEVICE: Final = gql(
    """
mutation CreateDevice($params: CreateDeviceParametersInput!) {
  createDevice(params: $params) {
    apiKey
    device {
      id
      name
    }
  }
}
"""
)

UPDATE_DEVICE: Final = gql(
    """
mutation UpdateDevice($deviceId: ID!, $params: UpdateDeviceParametersInput!) {
  updateDevice(deviceId: $deviceId, params: $params) {
    id
    name
    lastUsed
  }
}
"""
)

CLIENT_PROFILE: Final = gql(
    """
query ClientProfile {
  client {
    id
    email
    fullName
    initials
    onDuty
  }
}
"""
)

AGENCY_MEMBERSHIPS: Final = gql(
    """
query AgencyMemberships($first: Int!, $after: ID) {
  client {
    id
    agencyMemberships(first: $first, after: $after) {
      edges {
        cursor
        node {
          id
          agency {
            id
            name
            responseOptions {
              id
              text
              isPositive
            }
          }
        }
      }
      pageInfo {
        endCursor
      }
    }
  }
}
"""
)

UNIT_MEMBERSHIPS: Final = gql(
    """
query UnitMemberships($agencyId: ID!, $first: Int!, $after: ID) {
  client {
    id
    unitMemberships(filters: {agencyId: $agencyId}, first: $first, after: $after) {
      edges {
        node {
          id
          name
        }
      }
      pageInfo {
        endCursor
      }
    }
  }
}
"""
)

SET_DUTY_STATUS: Final = gql(
    """
mutation SetDutyStatus($clientId: ID!, $params: SetClientDutyStatusInput!) {
  setClientDutyStatus(clientId: $clientId, params: $params) {
    id
    onDuty
  }
}
"""
)

# The server replays every job already in the filter set as a CREATED update,
# then emits InitialDataLoaded as a sentinel, then streams live changes.
JOBS_SUBSCRIPTION: Final = gql(
    """
subscription Jobs($params: JobsSubscriptionParametersInput) {
  jobs(params: $params) {
    __typename
    ... on InitialDataLoaded {
      numberEntities
    }
    ... on JobUpdateNode {
      type
      job {
        ...JobFields
      }
    }
  }
}
"""
    + JOB_FRAGMENT
)

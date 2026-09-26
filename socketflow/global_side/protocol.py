"""Protocol version negotiation.

Each side declares the range of versions it understands. The server sends its
range during the handshake, the client picks a version both sides support, and
the connection is refused with a clear error when the ranges do not overlap.
"""

from typing import Optional, Tuple


# Version this build speaks by default.
PROTOCOL_VERSION = 1

# Oldest and newest protocol versions this build understands.
MIN_PROTOCOL_VERSION = 1
MAX_PROTOCOL_VERSION = 1


def is_enabled(protocol_version, minimum, maximum) -> bool:
    """True when the caller asked for version negotiation in any form."""
    return (
        protocol_version is not None
        or minimum is not None
        or maximum is not None
    )


def make_range(minimum: Optional[int] = None, maximum: Optional[int] = None) -> dict:
    """Build the version range advertised in the handshake."""
    low = MIN_PROTOCOL_VERSION if minimum is None else int(minimum)
    high = MAX_PROTOCOL_VERSION if maximum is None else int(maximum)
    if low > high:
        raise ValueError("minimum protocol version cannot exceed maximum")
    return {"min": low, "max": high}


def negotiate(
    local: dict, remote: Optional[dict]
) -> Optional[int]:
    """Pick the highest version both sides support.

    Returns the negotiated version, or None if the ranges do not overlap.
    """
    if not isinstance(remote, dict):
        return None
    try:
        remote_min = int(remote["min"])
        remote_max = int(remote["max"])
        local_min = int(local["min"])
        local_max = int(local["max"])
    except (KeyError, TypeError, ValueError):
        return None

    highest_min = max(local_min, remote_min)
    lowest_max = min(local_max, remote_max)
    if highest_min > lowest_max:
        return None
    return lowest_max


def describe(local: dict, remote: Optional[dict]) -> str:
    """Build a readable reason string for a failed negotiation."""
    local_text = f"{local.get('min')}-{local.get('max')}"
    if not isinstance(remote, dict):
        return (
            f"Server did not send a protocol version range (client supports "
            f"{local_text})"
        )
    remote_text = f"{remote.get('min')}-{remote.get('max')}"
    return (
        f"No common protocol version: client supports {local_text}, "
        f"server supports {remote_text}"
    )

import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import (
    unquote_plus,
    urlsplit,
    urlunsplit,
)


TRACKING_PARAMETERS = {
    "gh_src",
    "ref",
    "referrer",
    "source",
    "trackingid",
    "trk",
}


def clean_visible_url(url: str) -> str:
    """Remove punctuation accidentally captured after a URL."""

    return url.strip().rstrip(".,;:!?)]}\"'")


def normalize_url(url: str) -> str:
    """Return a safe canonical web URL without common tracking.

    Unknown query segments remain unchanged because some job sites use
    non-standard query strings as part of the job identifier.
    """

    cleaned = clean_visible_url(url)

    try:
        parts = urlsplit(cleaned)
        hostname = parts.hostname
        parts.port
    except ValueError:
        return ""

    scheme = parts.scheme.lower()

    if (
        scheme not in {"http", "https"}
        or not parts.netloc
        or not hostname
        or parts.username is not None
        or parts.password is not None
    ):
        return ""

    preserved_segments: list[str] = []

    for segment in parts.query.split("&"):
        if not segment:
            continue

        encoded_key = segment.split("=", 1)[0]
        decoded_key = unquote_plus(
            encoded_key
        ).lower()

        if decoded_key.startswith("utm_"):
            continue

        if decoded_key in TRACKING_PARAMETERS:
            continue

        preserved_segments.append(segment)

    normalized_path = parts.path

    if normalized_path != "/":
        normalized_path = (
            normalized_path.rstrip("/")
        )

    normalized_query = "&".join(
        preserved_segments
    )

    return urlunsplit(
        (
            scheme,
            parts.netloc.lower(),
            normalized_path,
            normalized_query,
            "",
        )
    )


def is_public_web_url(
    url: str,
    *,
    resolver: Callable[..., list[Any]] = socket.getaddrinfo,
) -> bool:
    """Return whether a URL resolves only to public HTTP(S) IPs."""

    normalized = normalize_url(url)

    if not normalized:
        return False

    try:
        parts = urlsplit(normalized)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return False

    if not hostname:
        return False

    hostname = hostname.rstrip(".").casefold()

    if (
        hostname == "localhost"
        or hostname.endswith(".localhost")
    ):
        return False

    try:
        literal_address = ipaddress.ip_address(
            hostname.split("%", 1)[0]
        )
    except ValueError:
        literal_address = None

    if literal_address is not None:
        return literal_address.is_global

    try:
        address_info = resolver(
            hostname,
            port or (
                443
                if parts.scheme.casefold() == "https"
                else 80
            ),
            type=socket.SOCK_STREAM,
        )
    except (
        OSError,
        UnicodeError,
        ValueError,
    ):
        return False

    resolved_addresses: list[
        ipaddress.IPv4Address
        | ipaddress.IPv6Address
    ] = []

    for item in address_info:
        try:
            address_text = str(
                item[4][0]
            ).split("%", 1)[0]
            resolved_addresses.append(
                ipaddress.ip_address(
                    address_text
                )
            )
        except (
            IndexError,
            TypeError,
            ValueError,
        ):
            return False

    return (
        bool(resolved_addresses)
        and all(
            address.is_global
            for address in resolved_addresses
        )
    )

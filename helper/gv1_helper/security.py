"""Validation for externally supplied URLs and ESP32-compatible names."""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import parse_qs, SplitResult, urlsplit, urlunsplit

from .errors import ValidationFailure

_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)
_FORBIDDEN_FILENAME = frozenset(r'/\:*?"<>|')
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_IPV6_LOCAL = (
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _split_url(raw: str, *, max_length: int = 2048) -> SplitResult:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > max_length:
        raise ValidationFailure("URL is empty or too long")
    try:
        parsed = urlsplit(raw.strip())
        _ = parsed.port
    except ValueError as exc:
        raise ValidationFailure("URL is malformed") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValidationFailure("credentials are not allowed in URLs")
    if not parsed.hostname:
        raise ValidationFailure("URL must include a host")
    return parsed


def validate_youtube_url(raw: str) -> str:
    """Accept only canonical HTTPS YouTube hosts, never suffix lookalikes."""
    parsed = _split_url(raw)
    if parsed.scheme.lower() != "https":
        raise ValidationFailure("YouTube URL must use HTTPS")
    if parsed.port not in (None, 443):
        raise ValidationFailure("custom YouTube URL ports are not allowed")
    try:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValidationFailure("YouTube host is invalid") from exc
    if host not in _YOUTUBE_HOSTS:
        raise ValidationFailure("URL must use youtube.com or youtu.be")
    video_id = r"[A-Za-z0-9_-]{3,32}"
    if host in {"youtu.be", "www.youtu.be"}:
        valid_path = re.fullmatch(rf"/{video_id}/?", parsed.path) is not None
    elif parsed.path == "/watch":
        values = parse_qs(parsed.query, keep_blank_values=True).get("v", [])
        valid_path = len(values) == 1 and re.fullmatch(video_id, values[0]) is not None
    else:
        valid_path = (
            re.fullmatch(rf"/(?:shorts|live|embed|v)/{video_id}/?", parsed.path)
            is not None
        )
    if not valid_path:
        raise ValidationFailure("URL must identify one YouTube video, not a redirect or playlist")
    return urlunsplit(("https", host, parsed.path, parsed.query, ""))


def validate_output_name(value: str | None, fallback_stem: str) -> str:
    """Return a firmware-safe UTF-8 .gv1 filename shorter than 128 bytes."""
    supplied = value is not None and bool(value.strip())
    raw = value.strip() if supplied else fallback_stem.strip()
    raw = unicodedata.normalize("NFC", raw)
    if not supplied:
        raw = "".join("_" if (ord(c) < 0x20 or c in _FORBIDDEN_FILENAME) else c for c in raw)
        raw = raw.replace("..", "_").strip(" .")
    if raw.lower().endswith(".gv1"):
        name = raw[:-4] + ".gv1"
    else:
        name = raw + ".gv1"
    if not name or name.startswith(".") or ".." in name:
        raise ValidationFailure("output_name is not a safe filename")
    if any(ord(c) < 0x20 or c in _FORBIDDEN_FILENAME for c in name):
        raise ValidationFailure("output_name contains a forbidden character")
    if len(name.encode("utf-8")) >= 128:
        if supplied:
            raise ValidationFailure("output_name must be shorter than 128 UTF-8 bytes")
        suffix = ".gv1"
        stem = name[:-4]
        while stem and len((stem + suffix).encode("utf-8")) >= 128:
            stem = stem[:-1]
        name = (stem.strip() or "video") + suffix
    return name or "video.gv1"


def validate_player_url(raw: str) -> str:
    """Allow only direct HTTP URLs to private/link-local IP literals.

    Requiring a literal address prevents DNS rebinding from turning the helper
    into a general-purpose SSRF proxy. ESP32 upload pages advertise such an IP.
    """
    parsed = _split_url(raw, max_length=256)
    if parsed.scheme.lower() != "http":
        raise ValidationFailure("player_url must use plain HTTP")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValidationFailure("player_url must be an origin without path, query, or fragment")
    try:
        address = ipaddress.ip_address(parsed.hostname.rstrip("."))
    except ValueError as exc:
        raise ValidationFailure("player_url host must be a private IP address") from exc
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValidationFailure("player_url address is not an ESP32 LAN address")
    if address.version == 4:
        allowed = any(address in network for network in _RFC1918) or address.is_link_local
    else:
        allowed = any(address in network for network in _IPV6_LOCAL)
    if not allowed:
        raise ValidationFailure("player_url must use a private or link-local address")
    port = parsed.port or 80
    if not 1 <= port <= 65535:
        raise ValidationFailure("player_url port is invalid")
    host_text = f"[{address.compressed}]" if address.version == 6 else address.compressed
    netloc = host_text if port == 80 else f"{host_text}:{port}"
    return f"http://{netloc}"


def looks_like_mp4(prefix: bytes) -> bool:
    """Perform a cheap ISO-BMFF signature check before invoking FFprobe."""
    if len(prefix) < 12:
        return False
    # ISO BMFF normally starts with an ftyp box. Permit a small leading box,
    # while still rejecting arbitrary uploads mislabeled as MP4.
    return any(prefix[pos : pos + 4] == b"ftyp" for pos in range(4, min(len(prefix), 64), 4))


def github_pages_origin(origin: str, explicit: tuple[str, ...] = ()) -> bool:
    normalized = origin.rstrip("/")
    if normalized in explicit:
        return True
    return bool(
        re.fullmatch(r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.github\.io", normalized, re.I)
        or re.fullmatch(r"http://(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?", normalized, re.I)
    )

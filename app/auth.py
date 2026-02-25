"""
Board authentication helpers.

Security note: This is NOT a cryptographic auth system.
It protects against accidental access, not targeted attacks in the LAN.
The key is visible in the URL, browser history, and server logs.
"""
import re
import secrets

RESERVED_SLUGS = {
    "health", "docs", "redoc", "openapi.json", "static",
    "favicon.ico", "api", "b",
}
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,49}$")


def validate_slug(slug: str) -> bool:
    """Return True if slug is valid and not reserved."""
    return slug not in RESERVED_SLUGS and SLUG_PATTERN.match(slug) is not None


def generate_key() -> str:
    """Generate a 16-character URL-safe random key."""
    return secrets.token_urlsafe(12)  # 12 bytes → 16 base64url chars

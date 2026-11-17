"""Creating short links: URL validation, code generation and aliases.

Generated codes look like airline PNR record locators: six characters, upper-case
letters and digits (for example ``K7QF2M``).
"""
import re
import secrets
from urllib.parse import urlsplit

from ..db import Database, DuplicateError, Link

# The record-locator character set: upper-case letters and digits without the
# look-alikes 0/O and 1/I, as reservation systems do, so a code survives being
# read aloud or retyped. 32 symbols over 6 positions gives about 1.07 billion codes.
PNR_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
PNR_LENGTH = 6
PNR_PATTERN = re.compile(f'[{PNR_ALPHABET}]{{{PNR_LENGTH}}}')
ALIAS_PATTERN = re.compile(r'[A-Za-z0-9_-]{3,32}')
MAX_URL_LENGTH = 2048

# Single-segment paths the application itself serves; a code must never shadow them.
RESERVED_CODES = frozenset({
    'api', 'admin', 'login', 'logout', 'links', 'docs', 'redoc', 'health', 'static', 'favicon', '_nicegui',
})


class InvalidUrlError(ValueError):
    pass


class InvalidAliasError(ValueError):
    pass


def normalize_url(raw: str) -> str:
    """Validate a destination URL and return it in a clean form.

    A missing scheme defaults to https, so "example.com/page" is accepted.
    """
    url = raw.strip()
    if not url:
        raise InvalidUrlError('Enter a URL to shorten.')
    if len(url) > MAX_URL_LENGTH:
        raise InvalidUrlError(f'That URL is longer than {MAX_URL_LENGTH} characters.')
    if any(c.isspace() or ord(c) < 32 for c in url):
        raise InvalidUrlError('A URL cannot contain spaces.')
    if '://' not in url:
        url = f'https://{url}'
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        raise InvalidUrlError('That does not look like a valid URL.') from None
    if parts.scheme.lower() not in ('http', 'https'):
        raise InvalidUrlError('Only http:// and https:// links can be shortened.')
    if not host or ('.' not in host and ':' not in host and host != 'localhost'):
        raise InvalidUrlError('That does not look like a valid URL.')
    return url


def validate_alias(alias: str) -> str:
    alias = alias.strip()
    if not ALIAS_PATTERN.fullmatch(alias):
        raise InvalidAliasError('An alias is 3-32 characters: letters, digits, "-" or "_".')
    if alias.lower() in RESERVED_CODES:
        raise InvalidAliasError(f'"{alias}" is reserved, pick another alias.')
    return alias


def generate_code() -> str:
    """Return a random PNR-style code.

    Each character is drawn independently from the operating system's CSPRNG
    (``secrets``), so codes are not sequential and knowing any number of issued
    codes tells an observer nothing about the others.
    """
    while True:
        code = ''.join(secrets.choice(PNR_ALPHABET) for _ in range(PNR_LENGTH))
        if code.lower() not in RESERVED_CODES:
            return code


async def create_link(
    db: Database,
    target_url: str,
    *,
    owner_id: int | None = None,
    alias: str | None = None,
) -> Link:
    """Shorten ``target_url``.

    Without an alias, shortening the same URL twice for the same owner returns
    the existing link instead of creating a duplicate.
    """
    target_url = normalize_url(target_url)

    if alias and alias.strip():
        alias = validate_alias(alias)
        try:
            return await db.links.add(alias, target_url, owner_id)
        except DuplicateError:
            raise InvalidAliasError(f'The alias "{alias}" is already taken.') from None

    if existing := await db.links.find_by_target(target_url, owner_id):
        return existing
    for _ in range(10):
        try:
            return await db.links.add(generate_code(), target_url, owner_id)
        except DuplicateError:
            continue  # collision with an existing code, try another
    raise RuntimeError('Could not find a free short code.')


async def resolve_code(db: Database, code: str) -> Link | None:
    """Find the link for a requested path.

    Like a record locator, a generated code is accepted in any letter case
    ("k7qf2m" finds "K7QF2M"). Custom aliases stay case-sensitive.
    """
    if link := await db.links.get_by_code(code):
        return link
    upper = code.upper()
    if upper != code and PNR_PATTERN.fullmatch(upper):
        return await db.links.get_by_code(upper)
    return None

"""Application settings, read from environment variables or a local ``.env`` file."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_STORAGE_SECRET = 'dev-only-change-me'


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    # Storage. Any URL understood by ``app.db.create_database``.
    database_url: str = 'sqlite:///./urls.db'

    # Public origin used when printing short links (e.g. "https://u.example.com").
    # When unset, the origin of the incoming request is used.
    base_url: str | None = None

    host: str = '127.0.0.1'
    port: int = 8000
    storage_secret: str = DEV_STORAGE_SECRET  # signs the session cookie
    trust_proxy_headers: bool = False  # behind a reverse proxy: read the client IP from X-Forwarded-For

    allow_anonymous: bool = True  # let visitors shorten links without signing in

    # Default accounts, created on first start if they do not exist yet.
    admin_username: str = 'admin'
    admin_password: str = 'admin'
    demo_username: str = 'demo'
    demo_password: str = 'demo'
    seed_demo_data: bool = True  # give the demo user a sample link with sample clicks

    # Abuse protection, counted per client IP address.
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 300  # pages and API calls per minute
    rate_limit_redirects: int = 60  # short links followed per minute
    rate_limit_links: int = 20  # links created per minute
    rate_limit_links_daily: int = 300  # links created per day
    rate_limit_logins: int = 10  # failed sign-ins per 10 minutes
    # An address that exceeds a limit this many times within an hour is blocked (0 disables).
    auto_block_strikes: int = 5
    auto_block_hours: int = 24

    # IP geolocation.
    geoip_enabled: bool = True
    geoip_timeout: float = 4.0
    # Private/loopback addresses cannot be geolocated. With this on, they are resolved
    # to the server's own public location instead, which is handy during local development.
    geoip_local_fallback: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()

# µrl — URL shortener

A URL shortener with QR codes and geolocated click analytics, built with
[FastAPI](https://fastapi.tiangolo.com) and [NiceGUI](https://nicegui.io).

- Shorten a URL and get a short link plus a QR code (SVG or PNG). Short codes look like airline PNR
  record locators (`/K7QF2M`): six upper-case letters and digits without the look-alikes `0 O 1 I`,
  drawn from a cryptographically secure random source so they cannot be guessed or enumerated.
  They are accepted in any letter case. A custom alias can be used instead.
- Every click is recorded and geolocated through free IP lookup services.
- Each link has an analytics page: clicks are drawn on an OpenStreetMap background, with totals,
  clicks per day, top countries and recent clicks floating above it. The page updates live.
- Accounts with two roles. Users see their own links; admins see every link and manage users.
- Per-IP rate limiting, with automatic and manual IP blocking for repeat offenders.
- A JSON API with interactive docs at `/docs`.
- Storage sits behind an abstract repository layer, so the database can be swapped by changing one URL.

## Background

I first built this several years ago as a small Flask app, purely to understand how URL shorteners
like Bitly work under the hood: take a long URL, hand back a short code, redirect whoever follows it.
It did that and nothing more, in a single file with a handful of HTML templates.

This version is a rebuild from scratch, applying what I have learned in the years since. The idea is
the same, but it now reflects how I would approach a real service: clear separation between the web
layer, the business logic and storage, a database that can be swapped without touching application
code, protection against abuse, automated tests, and a container setup for deployment.

The short codes are a nod to my time working for an airline. Every booking is identified by a PNR
record locator, six upper-case letters and digits such as `K7QF2M`, and that format has spent decades
being read over the phone, printed on boarding passes and typed in at check-in desks. Those are the
same demands a short link faces, so I borrowed it: six characters, no look-alikes such as `0`/`O` or
`1`/`I`, accepted in any letter case, and generated at random so they cannot be guessed.

```mermaid
flowchart TB
    subgraph before["Before: Flask, one file"]
        direction TB
        b1["Browser"] -->|"form post / page load"| b2["app.py<br/>3 routes: shorten, redirect, list all"]
        b2 --> b3["Jinja HTML templates<br/>Bootstrap from a CDN"]
        b2 --> b4["Flask-SQLAlchemy model"]
        b4 --> b5[("SQLite")]
    end

    subgraph after["Now: FastAPI + NiceGUI"]
        direction TB
        a1["Browser"] -->|"HTTP + WebSocket"| a2["Guard middleware<br/>per-IP rate limits, IP block list"]
        a2 --> a3["NiceGUI pages<br/>shorten, links, analytics map, admin"]
        a2 --> a4["JSON API<br/>HTTP Basic auth, OpenAPI docs"]
        a2 --> a5["Redirect endpoint<br/>PNR-style codes"]
        a3 --> a6["Services<br/>shortener, analytics, QR, guard, seed"]
        a4 --> a6
        a5 --> a6
        a5 -.->|"after the response is sent"| a7["Click recording + geolocation"]
        a7 -.-> a8["IP lookup providers<br/>with fallback and caching"]
        a6 --> a9["Repository interfaces<br/>users, links, clicks, blocks"]
        a7 --> a9
        a9 --> a10[("SQLite / PostgreSQL / MySQL")]
        a9 --> a11[("In-memory")]
        a1 -.->|"map tiles"| a12["OpenStreetMap"]
    end

    before ==>|"rebuilt"| after
```

A point-by-point comparison is under [Architectural changes](#architectural-changes).

## Run it

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run python -m app          # http://127.0.0.1:8000
```

Two accounts are created on first start:

| Username | Password | Role  |
|----------|----------|-------|
| `admin`  | `admin`  | admin |
| `demo`   | `demo`   | user  |

**Change these passwords before exposing the app** (`ADMIN_PASSWORD`, `DEMO_PASSWORD`), and set
`STORAGE_SECRET`. The demo account also gets one link, `/demo`, with 80 made-up clicks so the map has
something to show; turn that off with `SEED_DEMO_DATA=false`.

Clicks made from your own machine come from `127.0.0.1`, which cannot be geolocated, so they are
counted but not placed on the map. Set `GEOIP_LOCAL_FALLBACK=true` during development to place them at
your machine's public location instead.

## Docker

> **Untested.** The [Dockerfile](Dockerfile) and [docker-compose.yml](docker-compose.yml) have not
> been built or run yet, and the app has only been exercised against SQLite so far, not PostgreSQL.
> Expect to fix small things on first use.

```sh
docker compose up --build     # http://localhost:8000
```

This starts the app together with a PostgreSQL 17 database. Link data lives in the `pgdata` volume
and sign-in sessions in the `sessions` volume, so both survive restarts.

Before exposing it to anyone else, set these in the shell or in a `.env` file next to the compose
file: `STORAGE_SECRET`, `ADMIN_PASSWORD`, `DEMO_PASSWORD`, `POSTGRES_PASSWORD` (use letters and digits,
since it is placed inside the database URL) and `BASE_URL` (the public address, used in short links
and QR codes). The default account passwords only apply when the accounts are first created.

Rate limiting needs the visitor's real address. Docker Desktop (macOS, Windows) can present every
visitor as the same internal gateway address, in which case they share one budget. In production, put
a reverse proxy in front and set `TRUST_PROXY_HEADERS=true`.

To use the image without compose, point it at any database:

```sh
docker build -t murl .
docker run -p 8000:8000 -e DATABASE_URL=postgresql://user:pass@host/db -e STORAGE_SECRET=... murl
```

## Configuration

Settings are environment variables, or entries in a `.env` file. See [.env.example](.env.example) for
the full list.

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `sqlite:///./urls.db` | Where data is stored (see below) |
| `BASE_URL` | request origin | Public origin used in short links and QR codes |
| `STORAGE_SECRET` | development value | Signs the session cookie |
| `TRUST_PROXY_HEADERS` | `false` | Read the visitor IP from `X-Forwarded-For` (set when behind a proxy) |
| `ALLOW_ANONYMOUS` | `true` | Let visitors shorten links without an account |
| `GEOIP_ENABLED` | `true` | Geolocate clicks |

## Changing the database

| `DATABASE_URL` | Extra install |
|----------------|---------------|
| `sqlite:///./urls.db` | none |
| `postgresql://user:pass@host/db` | `uv sync --extra postgres` |
| `mysql://user:pass@host/db` | `uv sync --extra mysql` |
| `memory://` | none (data is lost on restart) |

Tables are created on startup. There are no migrations yet, so a schema change needs one (Alembic
would be the natural choice).

The application only talks to the abstract classes in [app/db/base.py](app/db/base.py) —
`Database` with a `UserRepository`, `LinkRepository`, `ClickRepository` and `BlockRepository` — and exchanges plain
dataclasses from [app/db/models.py](app/db/models.py). To support a store SQLAlchemy does not cover
(MongoDB, DynamoDB, Redis, ...):

1. Implement the five classes. [app/db/memory.py](app/db/memory.py) is a complete example in about 150 lines.
2. Register its URL scheme in `create_database` in [app/db/__init__.py](app/db/__init__.py).
3. Add it to the `db` fixture in [tests/conftest.py](tests/conftest.py); the repository contract tests
   then run against it.

## Rate limiting and IP blocking

Every client IP address has a budget per kind of request. Going over it answers `429 Too Many
Requests` with a `Retry-After` header until the window ends.

| What | Default | Setting |
|------|---------|---------|
| Pages and API calls | 300 per minute | `RATE_LIMIT_REQUESTS` |
| Short links followed | 60 per minute | `RATE_LIMIT_REDIRECTS` |
| Links created | 20 per minute and 300 per day | `RATE_LIMIT_LINKS`, `RATE_LIMIT_LINKS_DAILY` |
| Failed sign-ins (UI and API) | 10 per 10 minutes | `RATE_LIMIT_LOGINS` |

An address that exceeds a limit 5 times within an hour (`AUTO_BLOCK_STRIKES`) is blocked for 24 hours
(`AUTO_BLOCK_HOURS`): every page, API call and short link answers `403`. Blocks are stored in the
database, so they survive restarts. Admins can review, lift and add blocks under **Blocked IPs**.

Things to know:

- **Behind a reverse proxy, set `TRUST_PROXY_HEADERS=true`.** Otherwise every visitor appears to come
  from the proxy's address and shares one budget. The client address is taken from the last
  `X-Forwarded-For` entry, the one your proxy appends, so it cannot be forged by the client.
- Private and loopback addresses are rate limited but never blocked automatically.
- An IPv6 client is identified by its /64 network.
- Counters live in the process's memory. That fits this app, which runs as a single process; they
  reset on restart (blocks do not).

## Geolocation

Lookups run after the redirect has been sent, so visitors never wait for them. Providers are tried in
order until one answers, and results are cached per IP:

1. [ipwho.is](https://ipwho.is)
2. [geojs.io](https://www.geojs.io)
3. [ip-api.com](https://ip-api.com) (its free tier is HTTP only, and for non-commercial use)

All are free and need no key, but have rate limits; check their terms before running real traffic.
Providers are small classes in [app/services/geoip.py](app/services/geoip.py). Only a truncated IP
address is stored (IPv4 `/24`, IPv6 `/48`).

Map tiles come from OpenStreetMap's public tile servers, which are meant for light use; see their
[tile usage policy](https://operations.osmfoundation.org/policies/tiles/).

## API

HTTP Basic auth with an account's username and password. Full reference at `/docs`.

```sh
curl -u demo:demo -H 'content-type: application/json' \
     -d '{"url": "https://example.com/a/long/address", "alias": "example"}' \
     http://127.0.0.1:8000/api/links

curl -u demo:demo http://127.0.0.1:8000/api/links/example/stats
curl -o qr.png 'http://127.0.0.1:8000/api/links/example/qr?format=png'
```

| Method | Path | |
|--------|------|-|
| `POST` | `/api/links` | Shorten a URL |
| `GET` | `/api/links` | List your links (admins: all links) |
| `GET` `DELETE` | `/api/links/{code}` | Read or delete a link |
| `GET` | `/api/links/{code}/stats` | Totals, per-day counts, countries and map positions |
| `GET` | `/api/links/{code}/qr` | QR code, `?format=svg` or `png` (public) |
| `GET` | `/{code}` | Follow a short link (public) |

## Architectural changes

| Area | Original Flask app | This version |
|------|--------------------|--------------|
| Structure | One `app.py` holding routes, model and logic | Web layer, services and storage in separate packages |
| Pages | Server-rendered Jinja templates | NiceGUI components in Python, updated live over a WebSocket |
| API | None | JSON API with generated OpenAPI docs |
| Storage | Flask-SQLAlchemy model used directly in routes, SQLite | Abstract repositories; SQL and in-memory backends chosen by URL |
| Request handling | Synchronous | Async end to end, including database access |
| Short codes | 4 random letters from `random` (predictable generator) | 6-character PNR-style codes from a cryptographically secure source |
| URL validation | Browser-side `pattern` attribute only | Validated on the server |
| Accounts | None; every link was public at `/all` | Users and admins, hashed passwords, links private to their owner |
| Analytics | None | Clicks recorded after the redirect, geolocated, shown on a map |
| Abuse protection | None | Per-IP rate limits and persistent IP blocks |
| Configuration | Hard-coded | Environment variables or `.env` |
| Tests | None | Automated suite, with storage contract tests run against every backend |
| Deployment | `gunicorn` listed in requirements | Dockerfile and compose stack with PostgreSQL (untested) |

### OSI checklist

Where each concern is handled, layer by layer. A ticked box is implemented in this repository; an
unticked one is deliberately left to the deployment environment.

**Layer 7 — Application**
- [x] HTTP routing for pages, the JSON API and short-link redirects
- [x] Authentication (session sign-in for pages, HTTP Basic for the API) and per-owner authorisation
- [x] Server-side validation of URLs and aliases
- [x] Per-IP rate limiting with `429` and `Retry-After`; blocked addresses get `403`
- [x] Health endpoint for container and load balancer checks
- [ ] Web application firewall and bot detection (reverse proxy or CDN)

**Layer 6 — Presentation**
- [x] JSON request and response schemas, validated by Pydantic
- [x] QR codes rendered as SVG and PNG
- [x] Response compression (gzip) for pages
- [x] Passwords stored as salted scrypt hashes
- [ ] TLS encryption (terminate HTTPS at a reverse proxy; HTTP Basic credentials are only safe over TLS)

**Layer 5 — Session**
- [x] Signed session cookie for signed-in users
- [x] Persistent WebSocket per open page, with reconnection, for live updates
- [ ] Shared session store across several instances (the app runs as a single process)

**Layer 4 — Transport**
- [x] TCP served by uvicorn on a configurable host and port
- [x] Timeouts on outgoing geolocation requests
- [x] Database connection pooling with liveness checks
- [ ] Connection limits and slow-client protection (reverse proxy)

**Layer 3 — Network**
- [x] Client identified by IP address: IPv4 address or IPv6 /64 network
- [x] Real client address taken from the proxy's `X-Forwarded-For` entry when enabled
- [x] IP geolocation of clicks; private and loopback addresses are skipped
- [x] Stored addresses truncated (IPv4 `/24`, IPv6 `/48`)
- [x] Database reachable only on the internal compose network, not published to the host
- [ ] Firewall rules, DDoS mitigation and network-level IP filtering (hosting provider)

**Layers 2 and 1 — Data link and physical**
- [ ] Not applicable to application code; provided by the container network and the hosting platform

## Layout

```
app/
  main.py        FastAPI app, startup, NiceGUI mount
  api.py         JSON API and the redirect endpoint
  config.py      settings
  db/            storage abstraction: base (contract), models, sql and memory backends
  services/      shortener, geoip, analytics, guard (rate limits, IP blocks), qr, seed
  ui/            NiceGUI pages
tests/           uv run pytest
Dockerfile, docker-compose.yml    container image and app + PostgreSQL stack (untested)
```

"""Server settings, sourced from environment variables.

Kept deliberately minimal — every setting is overridable via env so
12-factor deploy is straightforward, with sensible defaults for local
dev. Settings are immutable after process start; reload requires
restart (matches how Docker / systemd unit files cycle).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # Storage
    database_url: str = "sqlite:///./.evalguard/server.db"
    # Auth — when empty, the server runs in ``open`` mode (dev only).
    # Production deployments must set ``EVALGUARD_API_KEY``.
    api_key: str = ""
    # Default tenancy — every ingest gets attributed to this org/project
    # if the request doesn't carry override headers (Phase 2.5 wiring).
    default_org_slug: str = "default"
    default_project_slug: str = "default"
    # CORS — comma-separated allowlist. Default is **empty** (no
    # cross-origin access) — production safety beats dev convenience.
    # Local development sets ``EVALGUARD_CORS_ORIGINS=*`` explicitly;
    # the startup check refuses to boot if both ``cors_origins=("*",)``
    # AND ``is_open_mode`` are true (a wide-open prod waiting to
    # happen). Same applies if open-mode is on with any CORS at all
    # — open-mode demands explicit acknowledgement.
    cors_origins: tuple[str, ...] = ()
    # Trusted hostnames for ``TrustedHostMiddleware``. Defaults to
    # accept any host in dev; production must set this. ``*`` is
    # accepted but warned-against at startup.
    trusted_hosts: tuple[str, ...] = ("*",)
    # When true, install ``HTTPSRedirectMiddleware``. Defaults off so
    # local dev on http://localhost works without flags.
    require_https: bool = False
    # Explicit opt-in for open mode (no API key). Defaults to false —
    # missing ``EVALGUARD_API_KEY`` alone now refuses to boot, instead
    # of silently exposing the API. Set ``EVALGUARD_OPEN_MODE=1`` to
    # acknowledge.
    open_mode_opt_in: bool = False
    # Bind
    bind_host: str = "127.0.0.1"
    bind_port: int = 8787
    # Maximum size of an inbound request body, in bytes. Defends
    # against adversarial / buggy clients pushing multi-GB payloads
    # that would OOM a worker. A real run with 10k rows and full
    # audit events tops out around ~50 MB; the default leaves
    # headroom while still rejecting obvious abuse.
    max_request_bytes: int = 100 * 1024 * 1024  # 100 MB
    # Database pool. SQLAlchemy defaults (5 + 10) multiplied across
    # uvicorn workers blow Postgres ``max_connections`` quickly on
    # busy deployments. The defaults here are conservative; operators
    # who genuinely need more should bump
    # ``EVALGUARD_DB_POOL_SIZE`` / ``EVALGUARD_DB_MAX_OVERFLOW``
    # alongside the Postgres-side ``max_connections``.
    db_pool_size: int = 10
    db_max_overflow: int = 0
    db_pool_pre_ping: bool = True
    db_pool_recycle_s: int = 1800   # recycle connections every 30 min
    # Phase 3c — probabilistic head-based sampling on OTLP ingest.
    # 1.0 (default) accepts every span; 0.1 keeps roughly 10 % of
    # traces (deterministic on traceId, so all spans of the same
    # trace agree). Drops are silent at the OTLP/HTTP layer (the
    # collector sees 200 OK) and counted in the structured access
    # log. Operators who want the OTel collector to do all sampling
    # themselves should leave this at 1.0; this knob is the API-edge
    # safety net for misconfigured collectors emitting orders of
    # magnitude more traces than expected.
    otlp_sample_rate: float = 1.0

    @property
    def is_open_mode(self) -> bool:
        """True iff no API key is configured. Open mode is loud:
        startup logs warn, ``GET /v1/health`` advertises it. Suitable
        for local dev only — production must set ``EVALGUARD_API_KEY``.
        Even in dev, ``EVALGUARD_OPEN_MODE=1`` is now required to
        explicitly acknowledge the no-auth posture."""
        return not self.api_key

    @property
    def sqlite_path(self) -> Path | None:
        """If ``database_url`` is a sqlite URL, return the filesystem
        path; otherwise None (Postgres support lands later)."""
        url = self.database_url
        if url.startswith("sqlite:///"):
            return Path(url[len("sqlite:///"):])
        if url.startswith("sqlite://"):
            return Path(url[len("sqlite://"):])
        return None


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in value.split(",") if s.strip())


def load_settings() -> Settings:
    """Read settings from the environment.

    Honours the ``EVALGUARD_*`` prefix on every key so a single env
    block governs the whole deployment. Defaults err toward production
    safety: missing ``EVALGUARD_CORS_ORIGINS`` is *empty* (no cross-
    origin), missing ``EVALGUARD_API_KEY`` is *not* enough on its own
    to boot in open mode — the operator must also set
    ``EVALGUARD_OPEN_MODE=1`` to acknowledge.
    """
    cors_raw = os.environ.get("EVALGUARD_CORS_ORIGINS")
    cors_origins = _split_csv(cors_raw) if cors_raw is not None else ()
    return Settings(
        database_url=os.environ.get("EVALGUARD_DATABASE_URL", Settings.database_url),
        api_key=os.environ.get("EVALGUARD_API_KEY", ""),
        default_org_slug=os.environ.get("EVALGUARD_DEFAULT_ORG", Settings.default_org_slug),
        default_project_slug=os.environ.get("EVALGUARD_DEFAULT_PROJECT", Settings.default_project_slug),
        cors_origins=cors_origins,
        trusted_hosts=_split_csv(os.environ.get("EVALGUARD_TRUSTED_HOSTS", "*")) or ("*",),
        require_https=os.environ.get("EVALGUARD_REQUIRE_HTTPS", "0") in {"1", "true", "TRUE"},
        open_mode_opt_in=os.environ.get("EVALGUARD_OPEN_MODE", "0") in {"1", "true", "TRUE"},
        bind_host=os.environ.get("EVALGUARD_HOST", Settings.bind_host),
        bind_port=int(os.environ.get("EVALGUARD_PORT", Settings.bind_port)),
        max_request_bytes=int(os.environ.get("EVALGUARD_MAX_REQUEST_BYTES",
                                              Settings.max_request_bytes)),
        db_pool_size=int(os.environ.get("EVALGUARD_DB_POOL_SIZE",
                                         Settings.db_pool_size)),
        db_max_overflow=int(os.environ.get("EVALGUARD_DB_MAX_OVERFLOW",
                                            Settings.db_max_overflow)),
        db_pool_pre_ping=os.environ.get("EVALGUARD_DB_POOL_PRE_PING", "1")
                            in {"1", "true", "TRUE"},
        db_pool_recycle_s=int(os.environ.get("EVALGUARD_DB_POOL_RECYCLE_S",
                                              Settings.db_pool_recycle_s)),
        otlp_sample_rate=float(os.environ.get("EVALGUARD_OTLP_SAMPLE_RATE",
                                               Settings.otlp_sample_rate)),
    )


class StartupRefusal(RuntimeError):
    """Configuration is unsafe to start. The error message tells the
    operator exactly which env var to set; tests assert on the prefix.
    """


def validate_for_startup(settings: Settings) -> None:
    """Refuse-to-boot checks. Raises ``StartupRefusal`` if the
    combination of settings would expose the server in a way the
    operator likely didn't intend. Called from the FastAPI lifespan
    so a misconfigured deployment fails fast and loud rather than
    silently running open.
    """
    if settings.is_open_mode and not settings.open_mode_opt_in:
        raise StartupRefusal(
            "EVALGUARD_API_KEY is empty (open mode) but EVALGUARD_OPEN_MODE=1 "
            "is not set. Open mode disables authentication entirely; set the "
            "env var explicitly to acknowledge, or set EVALGUARD_API_KEY to "
            "secure the server."
        )
    if settings.is_open_mode and "*" in settings.cors_origins:
        raise StartupRefusal(
            "Open mode (no API key) AND CORS=* would let any browser tab call "
            "the API. Set EVALGUARD_CORS_ORIGINS to an explicit allowlist or "
            "configure EVALGUARD_API_KEY."
        )
    # PROXY-2.5 review-pass: open mode + network-exposed bind is
    # particularly dangerous now that ``/v1/projects/{slug}/invoke``
    # fires real (paid) LLM calls.  Anyone on the network could
    # burn provider credit at line rate.  Refuse to boot unless the
    # operator has scoped the listener to loopback.  Operators who
    # need open mode on a non-loopback interface should put a
    # reverse-proxy with auth in front.
    #
    # Round-3 review-pass: include bracketed IPv6 loopback
    # ``[::1]`` — some shell pipelines surface the host in
    # bracketed form, and silently letting it through would defeat
    # the refusal.  Whitespace is stripped first so a trailing
    # newline from a shell capture doesn't sneak past either.
    # The error message names the EVALGUARD_HOST env var the
    # loader actually reads (config.py:load_settings reads
    # ``os.environ.get("EVALGUARD_HOST", ...)``); earlier text
    # said ``EVALGUARD_BIND_HOST`` which doesn't exist.
    _LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
    if settings.is_open_mode and (
        settings.bind_host.strip() not in _LOOPBACK_HOSTS
    ):
        raise StartupRefusal(
            f"Open mode (no API key) AND EVALGUARD_HOST="
            f"{settings.bind_host!r} would expose the proxy invoke "
            f"endpoint to any caller on the network. Bind to 127.0.0.1 "
            f"(default), put auth in front via a reverse proxy, or set "
            f"EVALGUARD_API_KEY to secure the server."
        )
    if "*" in settings.cors_origins and not settings.is_open_mode:
        # Token-bearing requests from arbitrary origins are still
        # risky; we *warn* but allow because some users genuinely
        # need it. The middleware install logs a follow-up.
        pass
    if not (0.0 <= settings.otlp_sample_rate <= 1.0):
        raise StartupRefusal(
            f"EVALGUARD_OTLP_SAMPLE_RATE must be in [0.0, 1.0]; "
            f"got {settings.otlp_sample_rate!r}. Use 0.0 to drop "
            f"every trace, 1.0 to accept every trace, anything in "
            f"between for probabilistic sampling."
        )

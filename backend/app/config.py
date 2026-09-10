"""Runtime configuration.

The API key is read here and nowhere else. It is never serialised into a
response, never logged, and never reaches the frontend -- the browser
talks only to the Resolve-E API, which is the whole point of having a
backend in front of CALL-E (``docs/security-privacy.md``).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.models import PolicySettings


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "development"
    log_level: str = "INFO"
    app_secret: str = "dev-secret-change-me"  # noqa: S105 - a default, not a secret

    database_url: str = (
        "postgresql+psycopg://resolvee:resolvee@localhost:5432/resolvee"
    )
    redis_url: str = "redis://localhost:6379/0"

    # ── CALL-E ────────────────────────────────────────────────────────
    calle_api_key: str = ""
    calle_base_url: str = "https://api.heycall-e.com"
    calle_webhook_url: str = ""
    calle_timeout_seconds: float = 30.0

    #: Use the official `calle-ai` SDK for live calls. The hand-rolled
    #: HTTP client stays available as a fallback -- both build identical
    #: requests, which is worth keeping as a second opinion.
    calle_use_sdk: bool = True

    #: The company the agent says it is calling for. Spoken aloud on
    #: every call, so it must be real and operator-supplied -- never
    #: inferred, and never a placeholder. Live dispatch fails closed
    #: without it (see :meth:`disclosure_ready`).
    buyer_company: str = ""

    #: The safety interlock. While false, Resolve-E uses the
    #: deterministic mock provider and places no real calls. Every
    #: reliability scenario in the test suite and the whole seeded demo
    #: run with this off.
    calle_live_calls: bool = False

    #: Belt and braces for the live path: when set, only these numbers
    #: may be dialled, regardless of what is in the database. Empty means
    #: no allowlist filtering (mock mode, or a deliberate production
    #: deployment that relies on the recipient-authorization column).
    calle_allowed_numbers: str = ""

    # ── policy knobs ──────────────────────────────────────────────────
    max_attempts: int = 3
    delay_threshold_hours: int = 48
    min_completion_confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    enforce_quiet_hours: bool = False

    # ── workers ───────────────────────────────────────────────────────
    scanner_interval_seconds: int = 10
    outbox_interval_seconds: int = 2
    reconciliation_interval_seconds: int = 15
    #: How long a call may sit in CALLING before we go and ask CALL-E
    #: for authoritative state rather than waiting for a webhook.
    reconciliation_after_seconds: int = 45

    @field_validator("calle_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def allowed_numbers(self) -> frozenset[str]:
        return frozenset(
            n.strip() for n in self.calle_allowed_numbers.split(",") if n.strip()
        )

    @property
    def live_calls_enabled(self) -> bool:
        """Live calling requires an explicit opt-in *and* a key.

        Two conditions rather than one, so that a stray environment
        variable cannot start dialling real phone numbers on its own.
        """
        return self.calle_live_calls and bool(self.calle_api_key)

    @property
    def disclosure_ready(self) -> bool:
        """True when the agent can truthfully say who it represents.

        A call that cannot name the company on whose behalf it is
        calling should not be placed. Enforced in the orchestrator, not
        merely asked for in the prompt.
        """
        return bool(self.buyer_company.strip())

    def policy(self) -> PolicySettings:
        return PolicySettings(
            max_attempts=self.max_attempts,
            delay_threshold_hours=self.delay_threshold_hours,
            min_completion_confidence=self.min_completion_confidence,
            enforce_quiet_hours=self.enforce_quiet_hours,
        )

    def safe_dump(self) -> dict[str, object]:
        """Config for the /health endpoint, with every secret removed."""
        return {
            "app_env": self.app_env,
            "live_calls_enabled": self.live_calls_enabled,
            "calle_base_url": self.calle_base_url,
            "transport": "sdk" if self.calle_use_sdk else "rest",
            "buyer_company": self.buyer_company or None,
            "disclosure_ready": self.disclosure_ready,
            "calle_api_key_present": bool(self.calle_api_key),
            "webhook_configured": bool(self.calle_webhook_url),
            "max_attempts": self.max_attempts,
            "delay_threshold_hours": self.delay_threshold_hours,
            "min_completion_confidence": self.min_completion_confidence,
            "enforce_quiet_hours": self.enforce_quiet_hours,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()

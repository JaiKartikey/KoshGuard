"""Central configuration.

Every threshold that changes security or cost behaviour lives here and can be
overridden from the environment (or a ``.env`` file). Keeping them in one typed
object means a reviewer can see the whole risk posture in one place, and tests
can build a ``Settings`` with exactly the values they need.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- LLM ---------------------------------------------------------------
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_mode: Literal["auto", "mock", "groq"] = "auto"
    primary_model: str = "llama-3.3-70b-versatile"
    fallback_model: str = "llama-3.1-8b-instant"
    max_output_tokens: int = 400
    request_timeout_seconds: float = 20.0

    # USD per 1M tokens as (input, output). Check https://groq.com/pricing before
    # relying on cost figures; these are configuration, not truth.
    model_prices: dict[str, tuple[float, float]] = Field(
        default_factory=lambda: {
            "llama-3.3-70b-versatile": (0.59, 0.79),
            "llama-3.1-8b-instant": (0.05, 0.08),
        }
    )

    # --- Gateway -------------------------------------------------------------
    max_retries: int = 2
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    breaker_failure_threshold: int = 5
    breaker_reset_seconds: float = 30.0
    rate_limit_capacity: int = 10
    rate_limit_refill_per_second: float = 0.5
    daily_token_budget: int = 50_000
    cache_ttl_seconds: int = 3600
    cache_max_entries: int = 1024
    redis_url: str | None = None

    # --- Guardrails ----------------------------------------------------------
    injection_block_threshold: float = 0.6
    grounding_min_overlap: float = 0.5
    max_message_chars: int = 2000
    allowed_link_domains: list[str] = Field(default_factory=lambda: ["koshbank.example"])

    # --- RAG -----------------------------------------------------------------
    policies_dir: Path = PROJECT_ROOT / "data" / "policies"
    retrieval_top_k: int = 3
    retrieval_min_score: float = 0.5

    # --- Observability -------------------------------------------------------
    audit_log_path: Path = PROJECT_ROOT / "logs" / "audit.jsonl"
    audit_hmac_key: str | None = None
    log_level: str = "INFO"
    langsmith_api_key: str | None = None
    langsmith_project: str = "koshguard"
    logfire_token: str | None = None

    @property
    def use_mock_llm(self) -> bool:
        if self.llm_mode == "mock":
            return True
        if self.llm_mode == "groq":
            return False
        return not self.groq_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()

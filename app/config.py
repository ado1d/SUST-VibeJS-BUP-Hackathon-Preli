"""Environment-driven configuration. All secrets come from env vars — never committed."""
import os


def _get(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


class Settings:
    """Central settings. Override any value via environment variables."""

    # --- LLM (OpenAI) ---
    openai_api_key: str = os.environ.get("OPENAI_API_KEY", "")
    openai_base_url: str = _get("OPENAI_BASE_URL", "")  # optional proxy/gateway
    openai_model: str = _get("OPENAI_MODEL", "gpt-4o-mini")
    openai_fallback_model: str = _get("OPENAI_FALLBACK_MODEL", "gpt-4.1-mini")
    openai_timeout_s: float = _get_float("OPENAI_TIMEOUT_S", 10.0)
    openai_fallback_timeout_s: float = _get_float("OPENAI_FALLBACK_TIMEOUT_S", 8.0)
    # Re-ask the LLM once (with validation error feedback) when guardrails reject output
    llm_validation_retries: int = _get_int("LLM_VALIDATION_RETRIES", 1)

    # --- Pipeline timing ---
    # Judge hard limit is 30s per request; keep an internal safety budget below it.
    request_budget_s: float = _get_float("REQUEST_BUDGET_S", 26.0)

    # --- Interpretation cache (repeated identical notes -> instant response) ---
    cache_enabled: bool = _get("CACHE_ENABLED", "1") == "1"
    cache_max_entries: int = _get_int("CACHE_MAX_ENTRIES", 512)

    # --- Misc ---
    log_level: str = _get("LOG_LEVEL", "INFO")
    # Numeric tolerance used by the self-validator (matches judge tolerance)
    tolerance: float = _get_float("NUMERIC_TOLERANCE", 0.01)


settings = Settings()

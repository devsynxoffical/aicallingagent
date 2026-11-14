"""Runtime configuration, loaded from environment variables / .env."""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Claude
    anthropic_api_key: str | None = None
    onboarding_model: str = "claude-opus-5"
    onboarding_effort: str = "high"
    call_model: str = "claude-opus-5"
    call_effort: str = "low"
    # Headroom so a goodbye plus log_call_outcome + end_call never truncates (streaming, so no latency cost).
    call_max_tokens: int = 1024
    # "adaptive" (recommended): the model decides per turn and rarely thinks on small talk; effort low keeps it short.
    # "disabled": lowest latency, but Opus 5 can then occasionally write a tool call as text, which the
    # speech sanitizer catches. Measure both with `callagent chat`.
    call_thinking: str = "adaptive"
    # Fast mode: same model, up to 2.5x output speed, premium price (Claude API only, Opus 5 / 4.8).
    call_fast_mode: bool = False
    # Speak a short "Mm-hm." if the model has not produced its first word within filler_delay_ms.
    call_fillers: bool = True
    filler_delay_ms: int = 700
    postcall_model: str | None = None  # defaults to onboarding_model
    enable_refusal_fallbacks: bool = True

    # Which provider does each job. Every provider sits behind an adapter in callagent/providers.
    stt_provider: str = "deepgram"  # deepgram | soniox
    tts_provider: str = "elevenlabs"

    # Deepgram
    deepgram_api_key: str | None = None
    deepgram_model: str = "nova-3-general"
    # How quickly Deepgram finalizes a transcript after speech pauses (ms). Lower = snappier.
    deepgram_endpointing_ms: int = 300
    # Model used when a call follows the prospect across languages (code-switching).
    deepgram_multilingual_model: str = "nova-3"
    # Languages you have confirmed the multilingual model transcribes well on your own calls.
    # A campaign that needs a language outside this list will not start on Deepgram.
    deepgram_multilingual_languages: str = "en,es,fr,de,hi,it,ja,nl,ru,pt"

    # Soniox (alternative ears; one model with built-in language identification)
    soniox_api_key: str | None = None
    soniox_model: str = "stt-rt-v6"

    # ElevenLabs
    elevenlabs_api_key: str | None = None
    elevenlabs_voice_id: str | None = None
    # Optional voice per language for calls that switch language. Unset = one voice for all.
    elevenlabs_voice_id_en: str | None = None
    elevenlabs_voice_id_ar: str | None = None
    elevenlabs_voice_id_fr: str | None = None
    elevenlabs_model: str = "eleven_flash_v2_5"
    elevenlabs_stability: float = 0.45
    elevenlabs_similarity: float = 0.8
    elevenlabs_style: float = 0.25
    elevenlabs_speed: float = 1.0

    # Twilio
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None
    twilio_validate_signature: bool = True
    public_base_url: str | None = None
    human_transfer_number: str | None = None
    record_calls: bool = False
    call_time_limit_seconds: int = 600

    # Dialer policy
    max_concurrent_calls: int = 3
    max_attempts: int = 3
    retry_delay_minutes: int = 240
    calling_window_start: time = time(9, 0)
    calling_window_end: time = time(19, 0)
    default_timezone: str = "America/New_York"
    default_phone_region: str = "US"
    seconds_between_dials: float = 2.0
    # What one connected minute and one dial attempt cost you, all providers together
    # (telephony + speech recognition + model + voice). Measure it on your own calls.
    # Used for per-call cost, campaign spend and budgets. 0 = not measured yet.
    cost_per_connected_minute_usd: float = 0.0
    cost_per_call_attempt_usd: float = 0.0

    # Turn-taking
    use_smart_turn: bool = True
    opener_delay_seconds: float = 1.5
    user_idle_seconds: float = 8.0

    # Storage / server
    database_url: str = "sqlite:///./callagent.db"
    # Connection pool for a server database (ignored by SQLite). Size it to the number of
    # calls in progress plus API traffic; every call uses a connection for short moments only.
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_seconds: int = 10
    db_pool_recycle_seconds: int = 1800
    # How long a SQLite writer waits for another writer before giving up.
    sqlite_busy_timeout_seconds: int = 15
    # Longest we wait for the telephony provider's API before treating the request as unanswered.
    provider_timeout_seconds: float = 15.0
    data_dir: Path = Path("./data")
    server_host: str = "0.0.0.0"
    server_port: int = 8000
    server_url: str = "http://localhost:8000"
    booking_webhook_url: str | None = None
    # Shared secret for signing booking notifications (X-Callagent-Signature: sha256=<HMAC of the body>).
    booking_webhook_secret: str | None = None
    # Campaign (name or id) that answers calls to numbers with no campaign of their own.
    inbound_campaign: str | None = None
    # Bearer token for /api/*. Generated into DATA_DIR/api_token on first run if unset; the CLI reads the same file.
    api_token: str | None = None
    # Lets the website hand over what visitors send through its forms, and nothing else.
    # Set the same value as DEVYTALK_INTAKE_TOKEN on the website. Unset: intake is off.
    intake_token: str | None = None

    @field_validator("calling_window_start", "calling_window_end", mode="before")
    @classmethod
    def _parse_time(cls, v):
        if isinstance(v, str):
            hh, mm = v.split(":")
            return time(int(hh), int(mm))
        return v

    @field_validator("public_base_url", mode="before")
    @classmethod
    def _strip_slash(cls, v):
        return v.rstrip("/") if isinstance(v, str) and v else v

    @property
    def elevenlabs_voices(self) -> dict[str, str]:
        pairs = {"en": self.elevenlabs_voice_id_en, "ar": self.elevenlabs_voice_id_ar, "fr": self.elevenlabs_voice_id_fr}
        return {lang: voice for lang, voice in pairs.items() if voice}

    @property
    def postcall_model_id(self) -> str:
        return self.postcall_model or self.onboarding_model

    @property
    def ws_url(self) -> str:
        """wss:// URL Twilio should stream call audio to."""
        if not self.public_base_url:
            raise RuntimeError("PUBLIC_BASE_URL must be set to place calls")
        return self.public_base_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"

    @field_validator("stt_provider", "tts_provider", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.strip().lower() if isinstance(v, str) else v

    def missing_for_calls(self) -> list[str]:
        """Names of settings required to place live calls that are not configured."""
        from .providers import get_stt, get_tts

        required = {
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            **get_stt(self).required_settings(self),
            **get_tts(self).required_settings(self),
            "TWILIO_ACCOUNT_SID": self.twilio_account_sid,
            "TWILIO_AUTH_TOKEN": self.twilio_auth_token,
            "TWILIO_FROM_NUMBER": self.twilio_from_number,
            "PUBLIC_BASE_URL": self.public_base_url,
        }
        return [k for k, v in required.items() if not v]


def resolve_api_token(settings: "Settings") -> str:
    """The shared secret between the CLI and the server. Env wins; otherwise a random token
    is created once in DATA_DIR/api_token (mode 600) and reused."""
    if settings.api_token:
        return settings.api_token
    import secrets

    path = settings.data_dir / "api_token"
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(32)
    path.write_text(token)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s

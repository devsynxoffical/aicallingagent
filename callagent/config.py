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

    # Deepgram
    deepgram_api_key: str | None = None
    deepgram_model: str = "nova-3-general"
    # How quickly Deepgram finalizes a transcript after speech pauses (ms). Lower = snappier.
    deepgram_endpointing_ms: int = 300

    # ElevenLabs
    elevenlabs_api_key: str | None = None
    elevenlabs_voice_id: str | None = None
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

    # Turn-taking
    use_smart_turn: bool = True
    opener_delay_seconds: float = 1.5
    user_idle_seconds: float = 8.0

    # Storage / server
    database_url: str = "sqlite:///./callagent.db"
    data_dir: Path = Path("./data")
    server_host: str = "0.0.0.0"
    server_port: int = 8000
    server_url: str = "http://localhost:8000"
    booking_webhook_url: str | None = None
    # Bearer token for /api/*. Generated into DATA_DIR/api_token on first run if unset; the CLI reads the same file.
    api_token: str | None = None

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
    def postcall_model_id(self) -> str:
        return self.postcall_model or self.onboarding_model

    @property
    def ws_url(self) -> str:
        """wss:// URL Twilio should stream call audio to."""
        if not self.public_base_url:
            raise RuntimeError("PUBLIC_BASE_URL must be set to place calls")
        return self.public_base_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"

    def missing_for_calls(self) -> list[str]:
        """Names of settings required to place live calls that are not configured."""
        required = {
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "DEEPGRAM_API_KEY": self.deepgram_api_key,
            "ELEVENLABS_API_KEY": self.elevenlabs_api_key,
            "ELEVENLABS_VOICE_ID": self.elevenlabs_voice_id,
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

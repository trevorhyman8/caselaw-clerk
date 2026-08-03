"""Central config: env vars + config/config.yaml. No secrets have defaults —
anything sensitive must come from the environment (Railway env vars in
deployment, a local .env for dev, never committed)."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT / ".env"), extra="ignore")

    database_url: str = f"sqlite:///{ROOT / '.private' / 'caselaw.db'}"
    pipeline_mode: str = "shadow"  # shadow|live
    publishing_mode: str = "draft_only"  # draft_only|live — separate from pipeline_mode;
        # flip only after Scott's Phase 4 approval, per config/config.yaml publishing.mode

    courtlistener_token: str | None = None
    cl_webhook_secret: str | None = None
    govinfo_api_key: str | None = None

    anthropic_api_key: str | None = None
    llm_backend: str = "claude_p"  # claude_p (local dev, $0) | anthropic_api (deployed)

    telegram_bot_token: str | None = None
    telegram_allowed_user_ids: str = ""  # comma-separated

    wpcom_client_id: str | None = None
    wpcom_client_secret: str | None = None
    wpcom_access_token: str | None = None
    wpcom_site_id: str | None = None

    @property
    def telegram_allowlist(self) -> set[str]:
        return {u.strip() for u in self.telegram_allowed_user_ids.split(",") if u.strip()}


def load_config() -> dict:
    with open(ROOT / "config" / "config.yaml") as f:
        return yaml.safe_load(f)


settings = Settings()

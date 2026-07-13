"""
Career Raider - Settings Management
Pydantic V2 settings with validation, env parsing, and defaults.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, model_validator
from typing import List, Optional
import os


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # AI
    gemini_api_key: str = Field(..., alias="GEMINI_API_KEY")

    # Telegram
    telegram_bot_token: Optional[str] = Field(None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = Field(None, alias="TELEGRAM_CHAT_ID")

    # Alerting Channel
    alert_channel: str = Field("telegram", alias="ALERT_CHANNEL")

    # SMTP / Email Settings
    smtp_host: str = Field("smtp.gmail.com", alias="SMTP_HOST")
    smtp_port: int = Field(587, alias="SMTP_PORT")
    smtp_username: Optional[str] = Field(None, alias="SMTP_USERNAME")
    smtp_password: Optional[str] = Field(None, alias="SMTP_PASSWORD")
    alert_email_recipient: Optional[str] = Field(None, alias="ALERT_EMAIL_RECIPIENT")

    # GitHub (self-healing)
    github_token: Optional[str] = Field(None, alias="GITHUB_TOKEN")
    github_repo: Optional[str] = Field(None, alias="GITHUB_REPO")  # e.g. "user/career_raider"

    # LinkedIn IMAP
    linkedin_email: Optional[str] = Field(None, alias="LINKEDIN_EMAIL")
    linkedin_imap_password: Optional[str] = Field(None, alias="LINKEDIN_IMAP_PASSWORD")
    linkedin_imap_server: str = Field("imap.gmail.com", alias="LINKEDIN_IMAP_SERVER")

    # Database
    database_url: str = Field(
        "postgresql://career_user:career_password@localhost:5432/career_db",
        alias="DATABASE_URL"
    )

    # Redis
    redis_url: str = Field("redis://localhost:6379/0", alias="REDIS_URL")

    # Scraping
    greenhouse_poll_interval: int = Field(60, alias="GREENHOUSE_POLL_INTERVAL")
    rss_poll_interval: int = Field(300, alias="RSS_POLL_INTERVAL")
    playwright_poll_interval: int = Field(900, alias="PLAYWRIGHT_POLL_INTERVAL")
    telegram_poll_interval: int = Field(300, alias="TELEGRAM_POLL_INTERVAL")

    # AI
    gemini_batch_size: int = Field(20, alias="GEMINI_BATCH_SIZE")
    gemini_model: str = Field("gemini-2.0-flash", alias="GEMINI_MODEL")
    ai_timeout_secs: int = Field(60, alias="AI_TIMEOUT_SECS")

    # Scoring thresholds
    min_score_for_alert: int = Field(80, alias="MIN_SCORE_FOR_ALERT")
    min_salary_for_bonus: int = Field(150000, alias="MIN_SALARY_FOR_BONUS")

    # Health check
    health_report_interval: int = Field(21600, alias="HEALTH_REPORT_INTERVAL")  # 6 hours

    @model_validator(mode="after")
    def validate_keys(self):
        if not self.gemini_api_key or self.gemini_api_key == "your_gemini_api_key_here":
            raise ValueError("GEMINI_API_KEY must be set to a real key")
        
        channel = (self.alert_channel or "telegram").lower()
        if channel in ("telegram", "both"):
            if not self.telegram_bot_token or self.telegram_bot_token == "your_telegram_bot_token_here":
                raise ValueError("TELEGRAM_BOT_TOKEN must be set to a real token when Telegram alerts are enabled")
            if not self.telegram_chat_id or self.telegram_chat_id == "your_personal_telegram_chat_id":
                raise ValueError("TELEGRAM_CHAT_ID must be set to a real ID when Telegram alerts are enabled")
        
        if channel in ("email", "both"):
            if not self.smtp_username or self.smtp_username == "your_smtp_username_here":
                raise ValueError("SMTP_USERNAME must be set to a real email address when email alerts are enabled")
            if not self.smtp_password or self.smtp_password == "your_smtp_password_here":
                raise ValueError("SMTP_PASSWORD must be set to a real password when email alerts are enabled")
            if not self.alert_email_recipient or self.alert_email_recipient == "your_recipient_email_here":
                raise ValueError("ALERT_EMAIL_RECIPIENT must be set to a real recipient email address when email alerts are enabled")
        return self


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings

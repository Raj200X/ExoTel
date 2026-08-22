"""
Configuration management for Saarthi.

Loads settings from environment variables with sensible defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class AppConfig:
    """Application configuration loaded from environment variables."""

    # --- General ---
    env: str = "development"
    log_level: str = "INFO"
    secret_key: str = "change-this-to-a-secure-random-string"
    host: str = "0.0.0.0"
    port: int = 8000

    # --- LLM Provider (Google Gemini) ---
    gemini_api_key: str = ""
    model_name: str = "gemini-2.0-flash"

    # --- Vapi ---
    vapi_api_key: str = ""

    # --- Saarthi ---
    saarthi_phone_number: str = ""

    # --- Database ---
    database_url: str = "postgresql://user:password@localhost:5432/saarthi_db"

    @classmethod
    def from_env(cls) -> AppConfig:
        """Load configuration from environment variables.

        This method attempts to load variables from a local `.env` file first
        (if present), and then reads from the system's environment variables.
        It provides sensible defaults if specific variables are not set.

        Returns:
            An instantiated AppConfig object populated with environment values.
        """
        load_dotenv()
        return cls(
            env=os.getenv("ENV", "development"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            secret_key=os.getenv("SECRET_KEY", "change-this-to-a-secure-random-string"),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            model_name=os.getenv("MODEL_NAME", "gemini-2.0-flash"),
            vapi_api_key=os.getenv("VAPI_API_KEY", ""),
            saarthi_phone_number=os.getenv("SAARTHI_PHONE_NUMBER", ""),
            database_url=os.getenv("DATABASE_URL", "postgresql://user:password@localhost:5432/saarthi_db"),
        )

    @property
    def is_production(self) -> bool:
        """Check if the application is running in production mode.

        Returns:
            True if the environment is set to 'production', False otherwise.
        """
        return self.env == "production"

    @property
    def has_gemini(self) -> bool:
        """Check if the Google Gemini API key is configured.

        Returns:
            True if `gemini_api_key` is a non-empty string, False otherwise.
        """
        return bool(self.gemini_api_key)

    @property
    def has_vapi(self) -> bool:
        """Check if the Vapi API key is configured.

        Returns:
            True if `vapi_api_key` is a non-empty string, False otherwise.
        """
        return bool(self.vapi_api_key)

"""Configuración de la aplicación cargada desde el entorno."""

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict



class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env.local", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    deepgram_api_key: str = Field(
        default="", validation_alias=AliasChoices("deepgram__api_key", "DEEPGRAM_API_KEY")
    )
    llm_model: str = Field(
        default="gemma4:e2b", validation_alias=AliasChoices("llm__model", "LLM_MODEL")
    )
    llm_endpoint: str = Field(
        default="localhost:11434/api",
        validation_alias=AliasChoices("llm__endpoint", "LLM_ENDPOINT"),
    )
    app_version: str = "0.1.0"
    log_level: str = "INFO"
    claims_db_path: Path = Path("claims.sqlite3")

    @property
    def ollama_endpoint(self) -> str:
        endpoint = self.llm_endpoint.strip().rstrip("/")
        if not endpoint.startswith(("http://", "https://")):
            endpoint = f"http://{endpoint}"
        if endpoint.endswith("/api"):
            endpoint = endpoint[:-4] + "/v1"
        elif not endpoint.endswith("/v1"):
            endpoint += "/v1"
        return endpoint


@lru_cache
def get_settings() -> Settings:
    return Settings()

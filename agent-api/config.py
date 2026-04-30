from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # OpenEMR / FHIR
    openemr_base_url: str = "http://openemr"
    fhir_client_id: str = ""
    fhir_client_secret: str = ""
    fhir_token_url: str = ""  # defaults to openemr_base_url/oauth2/default/token if blank
    fhir_scopes: str = "system/Patient.read system/Observation.read system/MedicationRequest.read system/Condition.read system/AllergyIntolerance.read system/DiagnosticReport.read"

    # Redis
    redis_url: str = "redis://redis:6379/0"
    redis_ttl_seconds: int = 7200  # 2 hours — covers a full rounding shift

    # SQLite fallback checkpointer
    sqlite_db_path: str = "/data/checkpoints.db"

    # Anthropic
    anthropic_api_key: str = ""

    # Langfuse
    langfuse_secret_key: str = "secret"
    langfuse_public_key: str = "public"
    langfuse_host: str = "http://langfuse:3000"

    # App
    log_level: str = "INFO"

    # Phase 13 cutover flag — set to False once all cutover gates pass and the
    # legacy endpoints have been stable for one full release cycle.
    legacy_endpoints_enabled: bool = True

    @property
    def resolved_fhir_token_url(self) -> str:
        if self.fhir_token_url:
            return self.fhir_token_url
        return f"{self.openemr_base_url.rstrip('/')}/oauth2/default/token"


settings = Settings()

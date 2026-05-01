from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # OpenEMR / FHIR
    # This OpenEMR deployment uses password grant (not client_credentials) because
    # the SMART backend services flow is not configured on the Railway instance.
    openemr_base_url: str = "http://openemr"
    fhir_client_id: str = ""
    fhir_client_secret: str = ""
    fhir_token_url: str = ""  # defaults to openemr_base_url/oauth2/default/token if blank
    fhir_username: str = "admin"          # OpenEMR user for password grant
    fhir_password: str = ""              # set via FHIR_PASSWORD env var
    fhir_user_role: str = "users"        # OpenEMR user_role param required by password grant
    # user/* scopes with api:oemr work on this deployment.
    # system/* scopes + api:fhir return 401 — SMART backend services not configured.
    fhir_scopes: str = (
        "openid api:oemr "
        "user/Patient.rs user/Encounter.rs user/Observation.rs "
        "user/Condition.rs user/MedicationRequest.rs "
        "user/AllergyIntolerance.rs user/DiagnosticReport.rs"
    )

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
    langfuse_host: str = "https://us.cloud.langfuse.com"

    # App
    log_level: str = "INFO"

    # When set (truthy), exposes GET /diag/fhir for live FHIR connectivity probes.
    # Disabled by default; enable on Railway via COPILOT_DIAG=1 for one-curl
    # confirmation after a deploy.
    copilot_diag: str = ""

    # Phase 13 cutover flag — set to False once all cutover gates pass and the
    # legacy endpoints have been stable for one full release cycle.
    legacy_endpoints_enabled: bool = True

    @property
    def resolved_fhir_token_url(self) -> str:
        if self.fhir_token_url:
            return self.fhir_token_url
        return f"{self.openemr_base_url.rstrip('/')}/oauth2/default/token"


settings = Settings()

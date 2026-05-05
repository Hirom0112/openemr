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
    redis_ttl_seconds: int = 7200  # 2 hours — used by checkpointer turn storage

    # Per-data-cache TTLs (env-overridable). Split so a stale briefing window
    # does not have to match the bundle window. Bundle is the most expensive
    # to refetch (8 FHIR searches), so it gets the longest TTL; census is
    # short because triage criteria can change as new vitals/labs land;
    # explanation is long because it is keyed on a hash of the input criteria
    # and is therefore self-invalidating.
    bundle_cache_ttl_seconds: int = 7200       # 2 hours
    briefing_cache_ttl_seconds: int = 1800     # 30 minutes
    # Medication safety output cache. Mirrors briefing's TTL because the
    # two surfaces share the same staleness model — both are deterministic
    # given the FHIR bundle and re-validate against the bundle's
    # ``_cached_at`` fingerprint to invalidate when the underlying data
    # turns over. Without this cache every Meds button click pays the
    # full Haiku LLM round trip (~1.3s); with it, second clicks drop to
    # ~50-100ms.
    medication_safety_cache_ttl_seconds: int = 1800  # 30 minutes
    # Census drives every other surface (briefings warm from it, handoff
    # iterates over it), so worst-case staleness here propagates everywhere.
    # 5 minutes bounds that without making cold loads constant — the previous
    # 15-min TTL caused a visible 17-min mismatch between the census header
    # ("now") and the briefing's "Data as of" line.
    census_cache_ttl_seconds: int = 300        # 5 minutes
    explanation_cache_ttl_seconds: int = 86400  # 24 hours

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

    # ── Auth / CORS ──────────────────────────────────────────────────────────
    # HS256 secret used to verify JWTs minted by the OpenEMR PHP layer.
    # Empty string disables JWT verification (middleware logs a startup
    # warning and becomes a no-op) — keeps dev + tests green without
    # threading a token through every fixture.
    copilot_jwt_secret: str = ""
    # Single browser origin allowed by CORSMiddleware. Empty falls back to
    # ``*`` with a startup warning (dev only).
    openemr_origin: str = ""

    # PHI audit log — Postgres DSN for ``copilot_audit_events`` /
    # ``copilot_audit_destructions``.  Empty string disables the writer
    # entirely (no pool init, no DB calls); keeps tests + dev cheap and
    # makes it explicit which deployments are subject to the §9.7
    # 6-year retention contract.
    audit_db_url: str = ""

    # When set (truthy), exposes GET /diag/fhir for live FHIR connectivity probes.
    # Disabled by default; enable on Railway via COPILOT_DIAG=1 for one-curl
    # confirmation after a deploy.
    copilot_diag: str = ""

    # OpenEMR MySQL — used by the audit module to write rows into the
    # OpenEMR `log` table for HIPAA audit trail. All settings overridable
    # via env vars (OPENEMR_DB_HOST, etc). Defaults target the docker-compose
    # mysql service.
    openemr_db_host: str = "mysql"
    openemr_db_port: int = 3306
    openemr_db_name: str = "openemr"
    openemr_db_user: str = "openemr"
    openemr_db_password: str = ""

    # Phase 13 cutover flag — set to False once all cutover gates pass and the
    # legacy endpoints have been stable for one full release cycle.
    legacy_endpoints_enabled: bool = True

    # Cascading-fresh prefetch gate. When True, the OpenEMR landing-page
    # prefetch force-refreshes census + bundles + briefings + medication
    # safety on every login — so when the physician clicks Brief / Meds
    # later, they read truly fresh data, not whatever was cached from the
    # previous shift. Cost: ~$0.15 per login for a 10-patient census
    # (full breakdown in ARCHITECTURE.md §7.1.1).
    #
    # DEFAULT: True (clinical correctness wins for the current single-
    # provider demo + pilot scope). Set to False for cost-sensitive
    # deployments and rely on TTL-based cache reuse + explicit Refresh
    # button + a shift-aware warming cron instead.
    # Override via ``PREFETCH_FORCE_REFRESH_ON_LOGIN=0`` to disable.
    prefetch_force_refresh_on_login: bool = True

    @property
    def resolved_fhir_token_url(self) -> str:
        if self.fhir_token_url:
            return self.fhir_token_url
        return f"{self.openemr_base_url.rstrip('/')}/oauth2/default/token"


settings = Settings()

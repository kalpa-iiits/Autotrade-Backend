"""Fail-closed validation used both at startup and by the deployment pipeline."""

from app.core.config import production_config_errors, settings


def validate_production() -> None:
    errors = production_config_errors(settings)
    if settings.environment != "production":
        errors.insert(0, "AUTOTRADE_ENVIRONMENT must be production")
    if errors:
        raise RuntimeError("Unsafe production configuration:\n- " + "\n- ".join(errors))


if __name__ == "__main__":
    validate_production()
    print("Production configuration is valid")

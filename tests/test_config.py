import pytest

from terrasync.config import Settings


def test_defaults_are_closed():
    s = Settings.from_env({})
    assert s.allow_signup is False
    assert s.cors_origins == ()
    assert s.embedded_worker is False
    assert s.webhook_allow_private is False
    assert not s.smtp.configured
    assert not s.secure_origin


def test_reads_environment():
    s = Settings.from_env({
        "TERRASYNC_DB": "/data/x.db",
        "TERRASYNC_ALLOW_SIGNUP": "yes",
        "TERRASYNC_CORS_ORIGINS": "https://a.example, https://b.example",
        "TERRASYNC_PUBLIC_BASE_URL": "https://ts.example.com/",
        "TERRASYNC_WORKER_INTERVAL_S": "600",
        "TERRASYNC_SMTP_HOST": "smtp.example.com",
        "TERRASYNC_SMTP_FROM": "alerts@example.com",
        "TERRASHIELD_TOKEN": "svc",
    })
    assert s.db_path == "/data/x.db"
    assert s.allow_signup is True
    assert s.cors_origins == ("https://a.example", "https://b.example")
    assert s.public_base_url == "https://ts.example.com"
    assert s.secure_origin
    assert s.worker_interval_s == 600
    assert s.smtp.configured
    assert s.service_token == "svc"


def test_secrets_are_not_in_repr():
    s = Settings.from_env({"TERRASYNC_ADMIN_PASSWORD": "hunter2hunter2",
                           "TERRASYNC_SMTP_PASSWORD": "smtp-secret"})
    assert "hunter2hunter2" not in repr(s)
    assert "smtp-secret" not in repr(s)


@pytest.mark.parametrize("name,value", [
    ("TERRASYNC_ALLOW_SIGNUP", "maybe"),
    ("TERRASYNC_WORKER_INTERVAL_S", "5"),
    ("TERRASYNC_BACKFILL_DAYS", "0"),
])
def test_bad_values_fail_at_start(name, value):
    with pytest.raises(ValueError):
        Settings.from_env({name: value})

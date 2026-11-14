import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fixtures import make_sample_playbook  # noqa: E402


@pytest.fixture
def sample_playbook():
    return make_sample_playbook()


# ----------------------------------------------------------------------------- PostgreSQL as an ordinary role

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
APP_ROLE = "callagent_app_test"
APP_ROLE_PASSWORD = "only-for-the-test-database"  # a server that asks for passwords needs the role to have one


def postgres_app_url() -> str:
    """An empty PostgreSQL database owned by an ordinary role (no superuser, no BYPASSRLS),
    which is how the application must run for row-level security to bind it. The
    superuser in TEST_POSTGRES_URL is used only to prepare it."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    admin = create_engine(POSTGRES_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        if not c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": APP_ROLE}).scalar():
            c.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS"))
        c.execute(text(f"ALTER ROLE {APP_ROLE} PASSWORD '{APP_ROLE_PASSWORD}'"))
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text(f"CREATE SCHEMA public AUTHORIZATION {APP_ROLE}"))
    admin.dispose()
    return make_url(POSTGRES_URL).set(username=APP_ROLE, password=APP_ROLE_PASSWORD).render_as_string(hide_password=False)


@pytest.fixture(autouse=True)
def _fresh_knowledge_cache():
    """Knowledge indexes are kept between calls of a campaign. Tests start new databases
    that reuse the same ids, so each test starts with nothing kept."""
    from callagent.training import search

    search.clear_cache()
    yield
    search.clear_cache()

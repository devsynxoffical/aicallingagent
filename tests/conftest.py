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

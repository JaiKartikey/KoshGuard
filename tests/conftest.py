import pytest

from koshguard.config import Settings
from koshguard.service import KoshGuard


@pytest.fixture
def settings(tmp_path):
    return Settings(llm_mode="mock", groq_api_key=None, audit_log_path=tmp_path / "audit.jsonl", redis_url=None)


@pytest.fixture
def kg(settings):
    return KoshGuard(settings)

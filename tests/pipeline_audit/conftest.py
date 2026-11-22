"""Reuse the resource guard; assertion xfails cannot conceal forbidden persisted reads."""

import pytest

from tests.security.conftest import isolated_resources  # noqa: F401


@pytest.fixture
def settings(settings):
    """Extend the parent fixture: an ambient replay-cache override must not join independent cases."""
    return settings.model_copy(update={"enrich_cache": settings.cache_dir / "enrich"})

"""The ASTRA suite may read code/dependencies, never persisted HYDRA data or weights."""

import builtins
import io
import os
from pathlib import Path

import pytest

_ROOT = Path.home() / "HydraNexus"


class ForbiddenResourceAccess(RuntimeError):
    """Not an AssertionError: regression xfails must not hide resource-policy violations."""


@pytest.fixture(autouse=True)
def synthetic_resources_only(monkeypatch):
    attempts = []

    def guard(original):
        def checked(file, *args, **kwargs):
            if not isinstance(file, int):
                path = Path(os.fsdecode(file)).resolve()
                try:
                    relative = path.relative_to(_ROOT)
                except ValueError:
                    pass
                else:
                    parts = relative.parts
                    if parts and (parts[0] in {"cache", "models"} or parts[0].startswith("state")
                                  or parts[:2] == ("data", "nask")):
                        attempts.append(str(path))
                        raise ForbiddenResourceAccess(f"ASTRA synthetic-only boundary: {path}")
            return original(file, *args, **kwargs)
        return checked

    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", guard(io.open))
    monkeypatch.setattr(os, "open", guard(os.open))
    yield
    if attempts:
        raise ForbiddenResourceAccess(f"Forbidden resource reads were attempted: {attempts}")

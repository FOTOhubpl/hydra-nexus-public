from __future__ import annotations

import io
import os
import socket

# Unit tests never depend on the local (organiser-derived, outside git) lexicon overlay; the opt-in sample
# regression (HYDRA_SAMPLE_TEST=1) runs with it, as the product does.
# No background cache warm-up thread in tests (it would outlive the temporary state of a TestClient).
os.environ.setdefault("HYDRA_API_WARM", "0")
# AUTH-LOGIN: the product default is HYDRA_AUTH=1. The existing suite exercises the API without a login; tests/auth
# (and the e2e server, which sets HYDRA_AUTH=1 itself) run with it on. Forced, so a shell export cannot flip it.
os.environ["HYDRA_AUTH"] = "0"
os.environ.pop("HYDRA_ADMIN_PASSWORD", None)
if os.environ.get("HYDRA_SAMPLE_TEST") != "1":
    os.environ["HYDRA_LEXICON_DIR"] = "none"
    # Unit tests compute exact bits on synthetic data: the shipped background prior (default `mixed`) stays off
    # unless a test sets HYDRA_BACKGROUND_PRIOR itself; the sample regression runs with the product default.
    os.environ.setdefault("HYDRA_BACKGROUND_PRIOR", "0")
# The product's process socket guard (hydra.netguard) is replaced in tests by the stricter recording guard below;
# tests/core/test_netguard.py installs and removes it explicitly.
os.environ["HYDRA_SOCKET_GUARD"] = "0"
# META-COLLECT: no background Meta analysis worker in unit tests (tests/meta_collection drive MetaWorker directly).
os.environ.setdefault("HYDRA_META_WORKER", "0")
from pathlib import Path

import pytest
from PIL import Image

from hydra.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # Explicit kwargs beat HYDRA_* env vars, so tests never touch ~/HydraNexus/state.
    s = Settings(
        root=tmp_path,
        data_dir=tmp_path / "data",
        models_dir=tmp_path / "models",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        max_file_mb=5,
    )
    s.ensure_dirs()
    return s


@pytest.fixture(autouse=True)
def _no_real_meta_token(monkeypatch, tmp_path):
    """Tests never see a developer's real Ad Library token (env or ~/.config/hydra/meta.env)."""
    monkeypatch.delenv("META_AD_LIBRARY_TOKEN", raising=False)
    monkeypatch.delenv("HYDRA_META_TOKEN", raising=False)
    monkeypatch.setenv("HYDRA_META_ENV_FILE", str(tmp_path / "no-meta.env"))


def png_bytes(color: tuple[int, int, int] = (200, 30, 30), size: tuple[int, int] = (64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def make_png():
    return png_bytes


# ---------------------------------------------------------------------------------------------
# Network guard: every test runs offline. Any non-loopback connect / DNS lookup / UDP send raises
# AND is recorded, so a library that swallows the error still fails the test at teardown.
# ---------------------------------------------------------------------------------------------
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", None}
_ORIG_CONNECT = socket.socket.connect
_ORIG_CONNECT_EX = socket.socket.connect_ex
_ORIG_SENDTO = socket.socket.sendto
_ORIG_GETADDRINFO = socket.getaddrinfo
_network_attempts: list[str] = []


def _is_local(sock: socket.socket, address: object) -> bool:
    if sock.family == getattr(socket, "AF_UNIX", object()):
        return True
    return isinstance(address, tuple) and bool(address) and address[0] in _LOCAL_HOSTS


def _blocked(what: str) -> OSError:
    _network_attempts.append(what)
    return OSError(f"network blocked in tests: {what}")


def _guard_connect(self, address):  # type: ignore[no-untyped-def]
    if not _is_local(self, address):
        raise _blocked(f"connect {address!r}")
    return _ORIG_CONNECT(self, address)


def _guard_connect_ex(self, address):  # type: ignore[no-untyped-def]
    if not _is_local(self, address):
        raise _blocked(f"connect_ex {address!r}")
    return _ORIG_CONNECT_EX(self, address)


def _guard_sendto(self, data, *args):  # type: ignore[no-untyped-def]
    address = args[-1] if args else None
    if address is not None and not _is_local(self, address):
        raise _blocked(f"sendto {address!r}")
    return _ORIG_SENDTO(self, data, *args)


def _guard_getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
    if host not in _LOCAL_HOSTS and (not isinstance(host, bytes) or host.decode() not in _LOCAL_HOSTS):
        _network_attempts.append(f"getaddrinfo {host!r}")
        raise socket.gaierror(f"network blocked in tests: getaddrinfo {host!r}")
    return _ORIG_GETADDRINFO(host, *args, **kwargs)


@pytest.fixture(scope="session", autouse=True)
def _offline_socket_guard():
    socket.socket.connect = _guard_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _guard_connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = _guard_sendto  # type: ignore[method-assign]
    socket.getaddrinfo = _guard_getaddrinfo  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = _ORIG_CONNECT  # type: ignore[method-assign]
        socket.socket.connect_ex = _ORIG_CONNECT_EX  # type: ignore[method-assign]
        socket.socket.sendto = _ORIG_SENDTO  # type: ignore[method-assign]
        socket.getaddrinfo = _ORIG_GETADDRINFO  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _no_network_attempts(_offline_socket_guard):
    start = len(_network_attempts)
    yield
    attempts = _network_attempts[start:]
    assert not attempts, f"test tried to use the network: {attempts}"


@pytest.fixture(autouse=True)
def _never_the_default_state(tmp_path_factory, monkeypatch):
    """No test may write to ~/HydraNexus/state (or the shared caches): `get_settings()` is lru_cached, so the
    cache is cleared around every test and the default state/cache dirs point into a per-test temp dir. Tests that
    want their own paths still pass explicit `Settings(...)` kwargs or patch `get_settings`."""
    from hydra import config

    sandbox = tmp_path_factory.mktemp("default-env")
    monkeypatch.setenv("HYDRA_STATE_DIR", str(sandbox / "state"))
    monkeypatch.setenv("HYDRA_CACHE_DIR", str(sandbox / "cache"))
    original = config.get_settings  # a test may monkeypatch it; clear the real cache either way
    original.cache_clear()
    yield
    original.cache_clear()


def polish_line_image():
    """A rendered Polish line (all diacritics), or None when no suitable TTF font is installed."""
    from PIL import ImageDraw, ImageFont

    for font_path in ("/System/Library/Fonts/Supplemental/Arial.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(font_path).exists():
            font = ImageFont.truetype(font_path, 48)
            im = Image.new("RGB", (900, 120), (255, 255, 255))
            ImageDraw.Draw(im).text((20, 30), POLISH_LINE, fill=(0, 0, 0), font=font)
            return im
    return None


POLISH_LINE = "Zażółć gęślą jaźń 2026"


@pytest.fixture
def polish_line():
    im = polish_line_image()
    if im is None:
        pytest.skip("no TTF font with Polish glyphs")
    return im

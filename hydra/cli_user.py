"""`hydra user create|reset-password|list|disable|enable|token` — panel accounts from the shell (AUTH-LOGIN).

Works on the state DB directly (same single-writer discipline as `hydra import`: one Writer for the command), so
an operator who lost the admin password can always recover: `uv run hydra user reset-password admin`.
Generated passwords are printed once; `--password-stdin` reads one instead (never a command-line argument).
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Iterator

import typer

from hydra.config import get_settings

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Konta panelu / panel accounts")


@contextlib.contextmanager
def _service() -> Iterator:
    from hydra.auth import AuthService
    from hydra.db import Writer, connect

    s = get_settings()
    s.ensure_dirs()
    writer = Writer(s.db_path)
    try:
        svc = AuthService(writer, s, lambda: contextlib.closing(connect(s.db_path, readonly=True)))
        yield svc
    finally:
        writer.close()


def _fail(exc: Exception) -> None:
    from hydra.auth import AuthError

    if isinstance(exc, AuthError):
        typer.echo(f"błąd / error: {exc.message}", err=True)
        raise typer.Exit(1)
    raise exc


def _read_password() -> str:
    pw = sys.stdin.readline().rstrip("\n")
    if not pw:
        typer.echo("błąd / error: puste hasło na stdin / empty password on stdin", err=True)
        raise typer.Exit(1)
    return pw


@app.command("list")
def list_users() -> None:
    """Users, roles and status (no hashes)."""
    with _service() as svc:
        if not svc.user_count():
            svc.bootstrap_admin()
        typer.echo(json.dumps(svc.list_users(), indent=2, ensure_ascii=False))


@app.command()
def create(username: str, role: str = typer.Option("analyst", help="admin | analyst | viewer"),
           password_stdin: bool = typer.Option(False, "--password-stdin", help="read the password from stdin")) -> None:
    """Create a user. Without --password-stdin a 20-character password is generated and printed once."""
    pw = _read_password() if password_stdin else None
    with _service() as svc:
        try:
            user, password = svc.create_user("cli", username, role, password=pw, must_change=pw is None)
        except Exception as exc:  # noqa: BLE001
            _fail(exc)
            return
    typer.echo(f"utworzono / created: {user['username']} ({user['role']})")
    if pw is None:
        typer.echo(f"hasło / password (pokazywane raz / shown once): {password}")


@app.command("reset-password")
def reset_password(username: str, password_stdin: bool = typer.Option(False, "--password-stdin")) -> None:
    """New password for a user (generated unless --password-stdin), unlocks the account, ends its sessions."""
    pw = _read_password() if password_stdin else None
    with _service() as svc:
        try:
            user, password = svc.reset_password("cli", username, pw)
        except Exception as exc:  # noqa: BLE001
            _fail(exc)
            return
    typer.echo(f"zmieniono hasło / password reset: {user['username']}")
    if pw is None:
        typer.echo(f"hasło / password (pokazywane raz, zmiana przy logowaniu / shown once): {password}")


@app.command()
def disable(username: str) -> None:
    """Disable an account (its sessions end; tokens stop working)."""
    with _service() as svc:
        try:
            svc.update_user("cli", username, disabled=True)
        except Exception as exc:  # noqa: BLE001
            _fail(exc)
    typer.echo(f"wyłączono / disabled: {username}")


@app.command()
def enable(username: str) -> None:
    """Re-enable a disabled account."""
    with _service() as svc:
        try:
            svc.update_user("cli", username, disabled=False)
        except Exception as exc:  # noqa: BLE001
            _fail(exc)
    typer.echo(f"włączono / enabled: {username}")


@app.command()
def token(username: str, name: str = typer.Option("cli", help="token label"),
          days: int | None = typer.Option(None, help="expiry in days (default: none)")) -> None:
    """Issue a personal access token for USERNAME (printed once; use as HYDRA_API_TOKEN / Bearer)."""
    from hydra.auth import Principal

    with _service() as svc:
        try:
            user = svc._find(username)
            p = Principal(user["id"], user["username"], user["role"], False, "token")
            secret, _item = svc.create_token(p, name, days)
        except Exception as exc:  # noqa: BLE001
            _fail(exc)
            return
    typer.echo(secret)

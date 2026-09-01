"""Local operator commands for retained contacts and uncertain receipts."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import os
import sqlite3
import sys
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import UUID

from aiohttp import web

from discord_ferry.feedback import DestinationKind
from discord_ferry.feedback_service.app import (
    FeedbackAccessLogger,
    create_app,
    install_service_logging,
)
from discord_ferry.feedback_service.config import ConfigError, ServiceConfig
from discord_ferry.feedback_service.store import (
    ContactDecryptionError,
    FeedbackStore,
    ReceiptState,
    ReceiptTransitionError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

_CONTACT_KEY_ENV = "FERRY_FEEDBACK_CONTACT_KEY"


class OperatorError(RuntimeError):
    """Safe message for a rejected local operator command."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m discord_ferry.feedback_service")
    parser.add_argument("--database", type=Path)
    groups = parser.add_subparsers(dest="group", required=True)

    service = groups.add_parser("serve")
    service.add_argument("--host", type=_host, default="0.0.0.0")
    service.add_argument("--port", type=_port, default=8080)

    contact = groups.add_parser("contact")
    contact_commands = contact.add_subparsers(dest="action", required=True)
    for action in ("show", "delete"):
        command = contact_commands.add_parser(action)
        command.add_argument("receipt", type=UUID)

    receipt = groups.add_parser("receipt")
    receipt_commands = receipt.add_subparsers(dest="action", required=True)
    resolve = receipt_commands.add_parser("resolve")
    resolve.add_argument("receipt", type=UUID)
    resolve.add_argument("url")
    absent = receipt_commands.add_parser("absent")
    absent.add_argument("receipt", type=UUID)
    return parser


def _host(value: str) -> str:
    try:
        return ip_address(value).compressed
    except ValueError as exc:
        raise argparse.ArgumentTypeError("host must be an IP address") from exc


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be from 1 through 65535")
    return port


def serve(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., None] = web.run_app,
) -> int:
    """Validate service settings and run the public HTTP application."""

    arguments = _parser().parse_args(argv)
    if arguments.group != "serve":
        raise OperatorError("serve requires the serve command")
    source = os.environ if environ is None else environ
    try:
        config = ServiceConfig.from_env(source)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    install_service_logging()
    runner(
        create_app(config),
        host=arguments.host,
        port=arguments.port,
        handle_signals=True,
        access_log_class=FeedbackAccessLogger,
        print=None,
    )
    return 0


def _contact_key(environ: Mapping[str, str]) -> bytes:
    value = environ.get(_CONTACT_KEY_ENV)
    if value is None or not value:
        raise OperatorError(f"{_CONTACT_KEY_ENV} is required for contact commands")
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise OperatorError(f"{_CONTACT_KEY_ENV} is invalid") from exc
    if len(decoded) != 32:
        raise OperatorError(f"{_CONTACT_KEY_ENV} is invalid")
    return decoded


def _validated_destination(url: str, kind: DestinationKind) -> str:
    parsed = urlparse(url)
    expected_route = "issues" if kind is DestinationKind.ISSUE else "discussions"
    expected_prefix = f"/nordscope-fi/Discord-stoat-ferry/{expected_route}/"
    number = parsed.path.removeprefix(expected_prefix)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(expected_prefix)
        or not number.isdigit()
    ):
        raise OperatorError("destination must be a matching Ferry GitHub URL")
    return url


async def run(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    """Run one local command and return its process exit status."""

    parser = _parser()
    arguments = parser.parse_args(argv)
    database = arguments.database
    if arguments.group == "serve":
        print("error: serve must run through the service entry point", file=sys.stderr)
        return 1
    if database is None:
        parser.error("--database is required for operator commands")
    if not database.is_absolute() or not database.is_file():
        print("error: --database must name an existing local file", file=sys.stderr)
        return 1
    current = datetime.now(tz=UTC) if now is None else now
    source = os.environ if environ is None else environ

    try:
        if arguments.group == "contact":
            store = FeedbackStore(database, contact_key=_contact_key(source))
            if arguments.action == "show":
                contact = await store.get_contact(arguments.receipt, now=current)
                if contact is None:
                    raise OperatorError("no retained contact exists for this receipt")
                print(contact)
            else:
                if not await store.delete_contact(arguments.receipt):
                    raise OperatorError("no retained contact exists for this receipt")
            return 0

        store = FeedbackStore(database)
        if arguments.action == "absent":
            await store.mark_absent(arguments.receipt, now=current)
            return 0

        record = await store.get_receipt(arguments.receipt)
        if record is None or record.state is not ReceiptState.PENDING:
            raise ReceiptTransitionError(f"receipt {arguments.receipt} is not pending")
        url = _validated_destination(arguments.url, record.destination_kind)
        await store.resolve_destination(arguments.receipt, url, now=current)
        return 0
    except (
        ContactDecryptionError,
        OperatorError,
        ReceiptTransitionError,
        sqlite3.Error,
    ) as exc:
        if isinstance(exc, sqlite3.Error):
            print("error: local database operation failed", file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "serve":
        raise SystemExit(serve(arguments))
    raise SystemExit(asyncio.run(run(arguments)))


if __name__ == "__main__":
    main()

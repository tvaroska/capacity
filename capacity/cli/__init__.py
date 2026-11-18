"""Typer CLI package for `capacity`, registering one module per command."""

from __future__ import annotations

from dotenv import load_dotenv

from capacity.cli import (
    chargeback as _chargeback,
)
from capacity.cli import (
    collect as _collect,
)
from capacity.cli import (
    demo as _demo,
)
from capacity.cli import (
    gateway as _gateway,
)
from capacity.cli import (
    optimize as _optimize,
)
from capacity.cli import (
    recommend as _recommend,
)
from capacity.cli import (
    schedule as _schedule,
)
from capacity.cli import (
    serve as _serve,
)
from capacity.cli.common import app as app

_REGISTERED_COMMANDS = (
    _demo,
    _recommend,
    _optimize,
    _gateway,
    _schedule,
    _chargeback,
    _collect,
    _serve,
)


def main() -> None:
    load_dotenv()
    app()

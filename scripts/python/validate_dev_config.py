"""Check host-run endpoints before starting local dependencies; never print secrets."""

import os
import sys
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def connect_host(bind):
    return {"0.0.0.0": "127.0.0.1", "::": "::1", "[::]": "::1"}.get(bind.strip("[]"), bind.strip("[]"))


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def check_endpoint(host, port, bind, published, label):
    if not 1 <= port <= 65535:
        raise ValueError(f"{label} must use a port from 1 to 65535.")
    if host in {"emqx", "postgres"}:
        raise ValueError(f"{label} uses a container-only hostname; configure a host-reachable endpoint in .env.")
    expected = connect_host(bind)
    if is_loopback(host) or host == expected:
        same_host = host == expected or (host == "localhost" and expected in LOOPBACK)
        if not same_host or port != published:
            raise ValueError(f"{label} does not match its configured host binding/port; update .env before make run.")


def main():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    try:
        check_endpoint(
            os.getenv("BROKER_HOST", "localhost"), int(os.getenv("BROKER_PORT", "1883")),
            os.getenv("BROKER_BIND", "127.0.0.1"), int(os.getenv("BROKER_PUBLISHED_PORT", "1883")),
            "BROKER_HOST/BROKER_PORT",
        )
        database = urlsplit(os.getenv(
            "DATABASE_URL", "postgresql+asyncpg://hbot:hummingbot-api@localhost:5432/hummingbot_api"
        ))
        if not database.hostname or not database.scheme.startswith("postgresql"):
            raise ValueError("DATABASE_URL must specify a PostgreSQL host endpoint.")
        check_endpoint(database.hostname, 5432 if database.port is None else database.port, os.getenv("DB_BIND", "127.0.0.1"),
                       int(os.getenv("DB_PORT", "5432")), "DATABASE_URL")
    except ValueError:
        # Parser exceptions can contain the supplied URL or secret; use a fixed message.
        print("Error: host-run broker/database settings are invalid or do not match published ports. "
              "Check BROKER_HOST/BROKER_PORT, DATABASE_URL, and the bind/port settings in .env.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

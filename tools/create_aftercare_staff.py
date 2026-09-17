#!/usr/bin/env python3
"""Create one internal aftercare staff account without exposing its password."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import aftercare_db
from app.aftercare_routes import create_staff_password


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--role", choices=aftercare_db.STAFF_ROLES, default="operator")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.getenv("LIMEAUTO_AFTERCARE_DB_PATH", aftercare_db.DEFAULT_DB_PATH)),
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password once from stdin; otherwise prompt without echo",
    )
    args = parser.parse_args()
    password = input() if args.password_stdin else getpass.getpass("Password: ")
    try:
        password_hash = create_staff_password(password)
        with aftercare_db.connect(args.db) as db:
            user = aftercare_db.create_staff_user(
                db,
                email=args.email,
                password_hash=password_hash,
                role=args.role,
            )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"created aftercare staff id={user['id']} email={user['email']} role={user['role']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

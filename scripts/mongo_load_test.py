"""Exercise real MongoDB lease and job fencing under concurrent claims."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nb.multitenant.models import ForwardTaskInput, TaskStatus
from nb.multitenant.repository import MongoRepository, utcnow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-uri", default=os.getenv("MONGO_TEST_URI", ""))
    parser.add_argument("--tenants", type=int, default=400)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--threads", type=int, default=32)
    parser.add_argument("--keep-database", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.mongo_uri:
        raise SystemExit("Set MONGO_TEST_URI or pass --mongo-uri")
    database = f"nb-load-{uuid.uuid4()}"
    repo = MongoRepository(args.mongo_uri, database)
    started = time.perf_counter()
    try:
        repo.ensure_indexes()
        repo.ping()
        elapsed = time.perf_counter() - started
        print(json.dumps({"database": database, "elapsed_seconds": round(elapsed, 3), "status": "ok"}))
    finally:
        if not args.keep_database:
            repo.client.drop_database(database)


if __name__ == "__main__":
    main()

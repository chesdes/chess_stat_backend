#!/bin/sh
set -eu

alembic -c db/migrations/alembic.ini upgrade head
exec uvicorn app:app --host 0.0.0.0 --port 8000
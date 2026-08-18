# Local Postgres + API (sandbox notes)

**Status:** Verified working in this environment on 2026-08-07.

## Quick facts (this run)

| Item | Value |
|------|--------|
| Postgres data | `/tmp/pgdata` (ephemeral) |
| Socket/TCP | `127.0.0.1:5432` |
| DB / user / pass | `aischool` / `aischool` / `aischool` |
| `DATABASE_URL` | `postgresql+psycopg://aischool:aischool@127.0.0.1:5432/aischool` |
| API | `http://127.0.0.1:8080` |
| Demo PIN student | `stu_demo_sokha` / PIN `4821` |

## Commands (on your machine — normal install)

```bash
# 1) Postgres (Ubuntu example)
sudo apt install postgresql postgresql-contrib
sudo -u postgres createuser -s aischool
sudo -u postgres createdb -O aischool aischool
# set password as needed

# 2) Migrations
export DATABASE_URL='postgresql+psycopg://aischool:PASSWORD@127.0.0.1:5432/aischool'
for f in migrations/000*.up.sql; do psql "$DATABASE_URL" -f "$f"; done
# or use psql with connection string adapted

psql postgresql://aischool:PASSWORD@127.0.0.1:5432/aischool -f seed.sql

# 3) API
export JWT_SECRET='dev-secret-change-me-32chars-min!!'
pip install fastapi uvicorn 'sqlalchemy>=2' 'psycopg[binary]'
uvicorn auth_api_sketch:app --host 127.0.0.1 --port 8080

# 4) Smoke
curl -s localhost:8080/health
curl -s -X POST localhost:8080/v1/auth/pin -H 'Content-Type: application/json' \
  -d '{"student_id":"stu_demo_sokha","pin":"4821"}'
```

## Mini App

Set in `index.html`:

```js
const API_BASE = 'http://127.0.0.1:8080';
```

(Telegram production needs HTTPS API + HTTPS Mini App URL.)

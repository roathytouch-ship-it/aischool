# How to test AI School on your Windows laptop

Goal: see **health = postgres** and **PIN login returns a token**.  
You do **not** need Telegram or cloud for this.

---

## What you will have

```text
PostgreSQL (on your PC)
        ↓
Python API  →  http://127.0.0.1:8080
        ↓
curl / browser tests with student PIN 4821
```

---

## Step 0 — Copy the project

Put the whole **`artifacts`** folder somewhere easy, for example:

```text
C:\aischool\artifacts
```

You need at least these files inside it:

- `auth_api_sketch.py`
- `db.py`, `repositories.py`, `jwt_refresh.py`, `study_service.py`, …
- `migrations\` folder
- `seed.sql`

---

## Step 1 — Install two programs

### A) Python

1. Open https://www.python.org/downloads/
2. Download and install
3. **Check** “Add python.exe to PATH”
4. Open **PowerShell** and type:

```powershell
python --version
```

You should see `Python 3.x.x`.

### B) PostgreSQL

1. Open https://www.postgresql.org/download/windows/
2. Install with installer (EnterpriseDB is fine)
3. Remember the password you set for user **`postgres`**
4. Leave port **5432**

---

## Step 2 — Create the database (one time)

Open **SQL Shell (psql)** from the Start menu.

Press Enter for each question until it asks for password → type the `postgres` password.

Then paste:

```sql
CREATE USER aischool WITH PASSWORD 'aischool' SUPERUSER;
CREATE DATABASE aischool OWNER aischool;
\q
```

---

## Step 3 — Open PowerShell in the project folder

```powershell
cd C:\aischool\artifacts
```

(Use your real path.)

---

## Step 4 — Install Python packages (one time)

```powershell
python -m pip install fastapi uvicorn "sqlalchemy>=2" "psycopg[binary]"
```

---

## Step 5 — Run database tables + seed

If `psql` is not found, use the full path (version may be 16 or 17):

```powershell
$env:Path += ";C:\Program Files\PostgreSQL\16\bin"
```

Then:

```powershell
$env:PGPASSWORD = "aischool"

Get-ChildItem .\migrations\*.up.sql | Sort-Object Name | ForEach-Object {
  Write-Host "Running" $_.Name
  psql -h 127.0.0.1 -U aischool -d aischool -f $_.FullName
}

psql -h 127.0.0.1 -U aischool -d aischool -f .\seed.sql
```

If password fails, in SQL Shell as `postgres` run:

```sql
ALTER USER aischool WITH PASSWORD 'aischool';
```

---

## Step 6 — Start the API

Keep this window open:

```powershell
cd C:\aischool\artifacts
$env:DATABASE_URL = "postgresql+psycopg://aischool:aischool@127.0.0.1:5432/aischool"
$env:JWT_SECRET = "dev-secret-change-me-32chars-min!!"
python -m uvicorn auth_api_sketch:app --host 127.0.0.1 --port 8080
```

You should see something like: `Uvicorn running on http://127.0.0.1:8080`

---

## Step 7 — Test (new PowerShell window)

### Test A — Is the API alive?

```powershell
curl http://127.0.0.1:8080/health
```

**Success looks like:** `"storage":"postgres"` and `"ok":true`

### Test B — PIN login

```powershell
curl -X POST http://127.0.0.1:8080/v1/auth/pin -H "Content-Type: application/json" -d "{\"student_id\":\"stu_demo_sokha\",\"pin\":\"4821\"}"
```

**Success:** long JSON with `"access_token":"..."` and `"display_name":"Sokha"`

### Test C — Start a lesson (copy the token)

```powershell
# 1) login and save token
$r = Invoke-RestMethod -Method POST -Uri http://127.0.0.1:8080/v1/auth/pin -ContentType "application/json" -Body '{"student_id":"stu_demo_sokha","pin":"4821"}'
$token = $r.access_token

# 2) start coding session
$h = @{ Authorization = "Bearer $token" }
Invoke-RestMethod -Method POST -Uri http://127.0.0.1:8080/v1/sessions/start -Headers $h -ContentType "application/json" -Body '{"subject_key":"coding","mode":"lesson"}'
```

**Success:** session with `"subject_key":"coding"` and `"duration_limit_sec":1500` (25 min on Basic)

---

## If something fails

| Problem | What to do |
|---------|------------|
| `python` not found | Reinstall Python with “Add to PATH”, open **new** PowerShell |
| `psql` not found | Add `C:\Program Files\PostgreSQL\16\bin` to PATH (see Step 5) |
| `connection refused` on 5432 | Start **PostgreSQL** service in Windows Services |
| `password authentication failed` | Reset password for user `aischool` |
| `relation accounts does not exist` | Migrations did not run — repeat Step 5 |
| API error on import | You are not in the `artifacts` folder |
| curl weird on Windows | Use the `Invoke-RestMethod` examples in Test C |

---

## What you are NOT testing yet

- Telegram real login (needs bot + HTTPS later)
- Cloud database
- Public website

**Local PIN path is enough to prove the backend works.**

---

## Checklist

- [ ] Python works (`python --version`)
- [ ] Postgres installed and running
- [ ] Database `aischool` created
- [ ] Migrations + seed run
- [ ] `uvicorn` running
- [ ] `/health` shows postgres
- [ ] PIN login returns `access_token`
- [ ] Start session returns coding lesson

When all boxes are checked, local testing **succeeded**.

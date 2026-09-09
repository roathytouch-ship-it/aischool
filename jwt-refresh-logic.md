# AI School — JWT Refresh Logic

**Status:** Implementation reference  
**Matches:** `openapi-auth.yaml` (`POST /auth/refresh`, `POST /auth/logout`)  
**Date:** 2026-08-05  

---

## 1. Goals

| Token | Lifetime (recommended) | Stored where | Purpose |
|-------|------------------------|--------------|---------|
| **Access JWT** | **90 min** (locked 2026-08-22) | Client memory only | API calls (`Authorization: Bearer`) |
| **Refresh token** | **14 days** (Web App) | HttpOnly cookie *or* secure client storage | Get new access token without re-login |
| **Telegram Mini App** | Prefer re-`/auth/telegram` with fresh `initData` | — | Refresh optional; Telegram session is source of truth |

**PIN Web App children** need refresh most.  
**Telegram users** can skip refresh and call `/auth/telegram` again when access expires.

---

## 2. Rules (locked for this implementation)

1. Access token is a **signed JWT** (HS256 or RS256). Claims are minimal.  
2. Refresh token is an **opaque random string** (32+ bytes), **not** a JWT.  
3. Server stores only **SHA-256 hash** of refresh token + metadata.  
4. **Refresh token rotation:** every successful refresh **revokes** the old token and issues a new pair.  
5. Reuse of an already-rotated refresh token → **revoke entire family** (theft detection).  
6. Logout deletes the session row (and family if using family ids).  
7. Access JWT is **not** stored server-side (stateless). Revocation of access before expiry is best-effort (short TTL).  
8. **Silent refresh required** in Mini App + test-app: on 401 expired access, `POST /v1/auth/refresh` then retry the same request. Student must not see “token expired” mid-lesson. Access 90 min ≠ study-day length. Refresh stays **14 days**.  

---

## 3. Access JWT claims

```json
{
  "sub": "acc_01HXYZ...",
  "role": "student",
  "student_id": "stu_emma_7x2",
  "parent_id": null,
  "auth_method": "pin",
  "sid": "sess_01H...",
  "iat": 1710000000,
  "exp": 1710001800,
  "iss": "ai-school",
  "typ": "access"
}
```

| Claim | Meaning |
|-------|---------|
| `sub` | `account_id` |
| `role` | `student` \| `parent` |
| `student_id` / `parent_id` | As in OpenAPI `Principal` |
| `auth_method` | `telegram` \| `pin` |
| `sid` | Session id (links to refresh row) |
| `typ` | Always `access` |

Do **not** put plan tier or pool balances in the JWT — read from DB on `/me` and session routes.

---

## 4. Server session row (refresh store)

```text
auth_sessions (
  id              TEXT PRIMARY KEY,          -- sess_...
  account_id      TEXT NOT NULL,
  family_id       TEXT NOT NULL,             -- shared across rotated tokens
  token_hash      TEXT NOT NULL UNIQUE,      -- sha256(refresh_token)
  auth_method     TEXT NOT NULL,             -- telegram | pin
  student_id      TEXT NULL,
  parent_id       TEXT NULL,
  role            TEXT NOT NULL,
  expires_at      TIMESTAMPTZ NOT NULL,
  revoked_at      TIMESTAMPTZ NULL,
  replaced_by     TEXT NULL,                 -- new session id after rotate
  user_agent      TEXT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

Index: `(account_id)`, `(token_hash)`, `(family_id)`.

---

## 5. Issue tokens (after Telegram or PIN login)

```
1. Create session id = sess_...
2. family_id = fam_... (new family on login)
3. refresh_raw = random_urlsafe(32)
4. INSERT auth_sessions (token_hash=sha256(refresh_raw), expires_at=now+14d, ...)
5. access = JWT.sign(claims + sid=session_id, exp=now+30m)
6. Return {
     access_token, token_type: "Bearer", expires_in: 1800,
     refresh_token: refresh_raw,
     principal: {...}
   }
```

---

## 6. `POST /auth/refresh` algorithm

```
Input: { refresh_token }

1. hash = sha256(refresh_token)
2. row = SELECT * FROM auth_sessions WHERE token_hash = hash
3. If no row → 401 invalid_refresh_token
4. If row.revoked_at IS NOT NULL:
     // Possible theft: token already rotated
     REVOKE all sessions WHERE family_id = row.family_id
     → 401 refresh_token_reuse
5. If row.expires_at < now() → 401 refresh_token_expired
6. // Rotate
   new_raw = random_urlsafe(32)
   new_sid = sess_...
   UPDATE row SET revoked_at=now(), replaced_by=new_sid
   INSERT new session (same family_id, account_id, role, ...)
7. access = JWT.sign(..., sid=new_sid, exp=now+30m)
8. Return TokenPair { access_token, expires_in: 1800, refresh_token: new_raw }
```

**Client must replace stored refresh token** with the new one every time.

---

## 7. `POST /auth/logout`

```
1. Require Bearer access JWT (or accept refresh_token body as alternative)
2. sid = jwt.sid
3. UPDATE auth_sessions SET revoked_at=now() WHERE id=sid
   // Optional: revoke whole family_id
4. 204 No Content
```

---

## 8. Middleware (every protected route)

```
1. Parse Authorization: Bearer <access>
2. Verify signature + exp + typ==access + iss
3. Optional: if sid revoked in cache/DB → 401 (only if you check sessions)
4. Attach principal to request context
```

Keep access TTL short so skipping server-side access revoke is acceptable.

---

## 9. Client behavior

| Client | Strategy |
|--------|----------|
| **Telegram Mini App** | On 401, call `/auth/telegram` with current `initData` again |
| **Web App (PIN child)** | Store refresh token; on 401 call `/auth/refresh`; if refresh fails → PIN login screen |
| **Parent Web** | Same as Web; or Telegram re-auth if linked |

Do **not** put refresh token in `localStorage` if you can use **HttpOnly Secure SameSite=Strict cookie**. If SPA cannot use cookies cross-site, use memory + sessionStorage with rotation.

---

## 10. Constants (defaults)

```text
ACCESS_TTL_SECONDS      = 5400        # 90 minutes (locked 2026-08-22)
REFRESH_TTL_SECONDS     = 1209600     # 14 days
REFRESH_BYTES           = 32
JWT_ALG                 = HS256       # or RS256 in production
HASH                    = SHA-256
```

---

## 11. Error codes

| HTTP | `error` | When |
|------|---------|------|
| 401 | `invalid_refresh_token` | Unknown token |
| 401 | `refresh_token_expired` | Past expires_at |
| 401 | `refresh_token_reuse` | Used after rotation (family revoked) |
| 401 | `unauthorized` | Bad/missing access JWT |

---

## 12. Reference code

See `jwt_refresh.py` — pure logic (no web framework). Wire into FastAPI/Flask/Express handlers matching OpenAPI paths.

---

*Companion to openapi-auth.yaml*

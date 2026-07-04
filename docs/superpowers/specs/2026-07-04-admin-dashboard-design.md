# Admin Dashboard Design

## Goal

Add a separate management dashboard at `/admin` so the owner can log in with a management account and review all users, their Chaoxing identity, seat query history, and seat watch history.

The dashboard must not fetch Chaoxing curriculum data every time it is opened. Chaoxing real names are persisted in the database and refreshed only at controlled moments.

## Current Context

The project is a Python `http.server` application with inline HTML and JavaScript in `app.py`. It stores data in MySQL through `database.py`.

Existing tables:

- `users`: internal users, currently created from Chaoxing account login.
- `chaoxing_sessions`: one saved Chaoxing cookie set per user.
- `seat_query_history`: user query history and saved seat snapshots.
- `seat_watch_tasks`: seat watch tasks and outcomes.

Existing auth covers normal users through `search_seat_session`. The admin dashboard needs a separate admin login and admin cookie.

## Recommended Approach

Use a dedicated admin surface:

- `GET /admin`: admin page.
- `POST /api/admin/login`: login with configured admin credentials.
- `POST /api/admin/logout`: clear admin session.
- `GET /api/admin/me`: check admin session.
- `GET /api/admin/users`: list users with persisted profile and aggregate activity.
- `GET /api/admin/users/<id>`: user detail with recent query and watch history.
- `POST /api/admin/users/<id>/sync-profile`: refresh one user's Chaoxing profile.
- `POST /api/admin/users/sync-missing-profiles`: refresh users missing a persisted Chaoxing real name.

Admin credentials come from environment variables:

- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`

If either is missing, `/admin` still renders, but login returns a clear configuration error.

Admin authentication uses a separate signed cookie such as `search_seat_admin_session`. It must not reuse the normal user cookie, because normal users must not access `/api/admin/*`.

## Persisted Chaoxing Profile

Add columns to `chaoxing_sessions`:

- `cx_user_name VARCHAR(128) NULL`: real name from Chaoxing curriculum API.
- `curriculum_synced_at DATETIME NULL`: last successful sync time.
- `curriculum_sync_error TEXT NULL`: last failure reason.

The dashboard reads these fields directly. Opening `/admin` or loading `/api/admin/users` must not call Chaoxing.

## Chaoxing Curriculum Sync

Add a helper in `chaoxing.py`:

- Build a session from saved `cookies_json`.
- Request `https://kb.chaoxing.com/pc/curriculum/getMyLessons?curTime=<current milliseconds>`.
- Use headers compatible with the browser request:
  - `Accept: application/json, text/javascript, */*; q=0.01`
  - `X-Requested-With: XMLHttpRequest`
  - `Referer: https://kb.chaoxing.com/res/pc/curriculum/schedule.html`
  - existing configured `User-Agent`
- Parse `data.curriculum.userName`.
- Return the parsed name and raw success/failure state.

Sync moments:

1. After a normal user logs in with Chaoxing successfully, save the cookie and immediately attempt curriculum profile sync.
2. If sync succeeds, persist `cx_user_name` and `curriculum_synced_at`, and clear `curriculum_sync_error`.
3. If sync fails, keep login successful, persist only `curriculum_sync_error`, and let the admin retry later.
4. For existing users missing `cx_user_name`, the admin can sync one user or batch sync missing users.

Batch sync should have a conservative limit, such as 20 users per click, to avoid a long request and unnecessary third-party traffic.

## Admin User List

The user list shows one row per user:

- Internal user id.
- Internal username and Chaoxing account.
- Persisted Chaoxing real name.
- Cookie validity state and last cookie update time.
- Last normal login time.
- Query count.
- Watch task count.
- Last query time.
- Last watch task time.
- Curriculum sync time or sync error.
- Actions: view detail, sync profile.

The query should use aggregate subqueries or grouped joins so the list does not load all history rows.

## Admin User Detail

The detail endpoint/page shows:

- User identity and persisted Chaoxing profile fields.
- Recent seat query history, newest first, limited to a practical number such as 50.
- Recent seat watch tasks, newest first, limited to a practical number such as 50.

The first version does not need admin-side delete or edit actions. The dashboard is read-mostly, with only profile sync as a write action.

## UI

Use a separate inline `ADMIN_HTML` in `app.py`, matching the existing simple single-file architecture.

The `/admin` page has:

- Login panel when not authenticated.
- User table after admin login.
- Detail panel or modal for the selected user.
- Buttons for refresh, sync one user, sync missing users, and logout.

The UI should be dense and operational, not a marketing page.

## Error Handling

- Missing admin config returns a clear login error.
- Invalid admin credentials return 401.
- Unauthenticated admin API requests return 401.
- Failed curriculum sync records the error in `curriculum_sync_error` and returns a non-fatal response.
- Cookie/session failures for one user must not prevent listing other users.

## Security Notes

- Do not display raw cookie values in the admin UI.
- Do not expose `/api/admin/*` without admin auth.
- Keep admin and normal user cookies separate.
- Sign admin cookies with `APP_SECRET`.
- Avoid storing plaintext admin password in the database. For this local project, environment variables are acceptable.

## Testing

Add focused tests where practical around pure functions:

- Admin session signing and verification.
- Curriculum response parsing.
- Admin SQL/data shaping helpers if extracted.

Manual verification:

- `/admin` renders login.
- Wrong admin login fails.
- Correct admin login shows users.
- User list loads from database without triggering Chaoxing requests.
- Normal Chaoxing login attempts profile sync but does not fail login if sync fails.
- Sync-one and sync-missing update persisted fields.

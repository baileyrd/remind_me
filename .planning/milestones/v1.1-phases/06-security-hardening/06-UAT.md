---
status: complete
phase: 06-security-hardening
source: [06-01-SUMMARY.md, 06-02-SUMMARY.md]
started: 2026-02-24T21:30:00Z
updated: 2026-02-24T21:55:00Z
---

## Current Test

[testing complete]

## Tests

### 1. CORS allows localhost origin
expected: Start the dashboard and run `curl -s -D- -o /dev/null -H "Origin: http://localhost:5199" http://localhost:5199/api/reminders`. Response headers should include `access-control-allow-origin: http://localhost:5199`.
result: pass

### 2. CORS blocks external origin
expected: Run `curl -s -D- -o /dev/null -H "Origin: http://evil.com" http://localhost:5199/api/reminders`. Response headers should NOT include any `access-control-allow-origin` header.
result: pass

### 3. Import rejects path outside home directory
expected: Run `curl -s http://localhost:5199/api/import -X POST -H "Content-Type: application/json" -d '{"file_path": "/etc/passwd"}'`. Response should be an error containing "not in allowed import roots" — the file is never read.
result: pass

### 4. Import allows valid path inside home directory
expected: Create a test JSON file in your home directory (e.g., `echo '[]' > ~/test_import.json`), then `curl -s http://localhost:5199/api/import -X POST -H "Content-Type: application/json" -d '{"file_path": "'$HOME'/test_import.json"}'`. The request should be processed (not blocked by the path guard). Clean up with `rm ~/test_import.json`.
result: pass

### 5. Bearer auth rejects unauthenticated request
expected: Stop the dashboard, set `export REMIND_ME_API_KEY=test-secret-123`, restart it. Run `curl -s -w "\n%{http_code}" http://localhost:5199/api/reminders`. Should return HTTP 401 with an unauthorized error message.
result: pass

### 6. Bearer auth accepts valid token
expected: With `REMIND_ME_API_KEY=test-secret-123` still set, run `curl -s -w "\n%{http_code}" -H "Authorization: Bearer test-secret-123" http://localhost:5199/api/reminders`. Should return HTTP 200 with your reminders data.
result: pass

### 7. Dashboard accessible without auth
expected: With `REMIND_ME_API_KEY=test-secret-123` still set, open `http://localhost:5199/` in a browser. The dashboard HTML page should load normally — no 401 error. The dashboard route is excluded from auth.
result: pass

### 8. No API key means open access
expected: Stop the dashboard, `unset REMIND_ME_API_KEY`, restart it. Run `curl -s -w "\n%{http_code}" http://localhost:5199/api/reminders`. Should return HTTP 200 — all routes are open when no API key is configured (backward compatible).
result: pass

## Summary

total: 8
passed: 8
issues: 0
pending: 0
skipped: 0

## Gaps

[none]

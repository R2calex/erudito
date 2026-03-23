# IR-2026-03-16: Erudito Health Check Timeouts

**Date:** 2026-03-16
**Severity:** Medium (intermittent alerts, no data loss)
**Duration:** ~2 hours of flapping (10:58 - 12:31 UTC)

## Symptom

Kubo's mesh-monitor reported Erudito (hanzo:8095) as DOWN repeatedly with "timed out", alternating with RECOVERED:

```
16:58 UTC — DOWN (timed out)
17:16 UTC — RECOVERED
17:46 UTC — DOWN (timed out)
18:04 UTC — DOWN (timed out)
18:22 UTC — DOWN (timed out)
18:31 UTC — RECOVERED
```

Erudito was never actually crashed — `systemctl --user status erudito.service` showed `active (running)` throughout, and local health checks (`curl localhost:8095/health`) returned 200 OK.

## Root Cause

Erudito ran uvicorn with **1 worker** (single-threaded async). The `erudito-scan.timer` fires `POST /scan` every 15 minutes. The scan operation is blocking — it reads files from disk, generates embeddings via Ollama, and writes to Qdrant. While the scan was running, the single worker could not respond to health check requests.

Mesh-monitor on Kubo has a `CHECK_TIMEOUT = 5` seconds. The scan regularly exceeded this, causing the health check to time out. The DOWN/RECOVERED flapping correlated exactly with the scan timer's 15-minute cycle.

**Evidence from logs:**
- Health checks logged normally every ~4 minutes
- A 49-minute gap in health check logs (11:39 → 12:28) coincided with the DOWN alerts
- The gap ended when the scan completed (`POST /scan 200 OK` at 12:28)

## Fix

Added `--workers 2` to the uvicorn command in the systemd service:

**File:** `~/.config/systemd/user/erudito.service`

```diff
- ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8095
+ ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8095 --workers 2
```

Applied:
```bash
systemctl --user daemon-reload
systemctl --user restart erudito.service
```

With 2 workers, one worker handles the scan while the other responds to health checks.

## Verification

```bash
# Confirm 2 workers running
ps --ppid $(pgrep -f "uvicorn main:app.*8095")
# Shows 2 child worker processes

# Health still works
curl -s http://localhost:8095/health
# {"status": "ok", ...}
```

## Prevention

This is a general pattern for any single-worker service that has both long-running operations and health checks. Services in the mesh that have periodic scan/sync operations should run with at least 2 uvicorn workers.

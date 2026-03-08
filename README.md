# Jellyfin Health Check

A deep health check script for Jellyfin media servers. Runs five sequential checks and reports overall server health.

## Checks

1. **Reachability** — `/health` returns 200
2. **Authentication** — real login with session token
3. **Library** — fetches random video items
4. **Direct stream** — requests raw file bytes (`Static=true`)
5. **Transcode** — forces an HLS transcode pipeline and reads output bytes

## Usage

```bash
# Basic (uses defaults: localhost:8096, admin user)
python3 jellyfin_healthcheck.py

# With explicit options
python3 jellyfin_healthcheck.py \
  --host http://192.168.1.100:8096 \
  --user admin --pass secret \
  --transcode-timeout 60

# Using environment variables
export JELLYFIN_HOST=http://192.168.1.100:8096
export JELLYFIN_USER=admin
export JELLYFIN_PASS=secret
python3 jellyfin_healthcheck.py
```

## Options

| Flag | Env Var | Default | Description |
|------|---------|---------|-------------|
| `--host` | `JELLYFIN_HOST` | `http://localhost:8096` | Jellyfin server URL |
| `--user` | `JELLYFIN_USER` | `admin` | Username |
| `--pass` | `JELLYFIN_PASS` | *(empty)* | Password |
| `--timeout` | `JELLYFIN_TIMEOUT` | `10` | General request timeout (seconds) |
| `--transcode-timeout` | `JELLYFIN_TC_TIMEOUT` | `45` | Transcode startup timeout (seconds) |
| `--log` | `JELLYFIN_LOG` | *(stdout only)* | Log file path |

## Exit Codes

- `0` — all checks passed (HEALTHY)
- `1` — one or more checks failed (DEGRADED)

## Requirements

Python 3.6+ (no external dependencies).

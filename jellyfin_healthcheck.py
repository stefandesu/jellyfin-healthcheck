#!/usr/bin/env python3
"""
Jellyfin Deep Health Check
--------------------------
Runs five sequential checks:
  1. Reachability    — /health returns 200
  2. Authentication  — real login, gets session token
  3. Library         — fetches two random video items
  4. Direct stream   — requests raw file bytes (Static=true)
  5. Transcode       — forces a transcode pipeline and reads output bytes

Usage:
  python3 jellyfin_healthcheck.py
  python3 jellyfin_healthcheck.py --host http://192.168.1.x:8096 --user admin --pass secret

Exit codes:
  0 = all checks passed
  1 = one or more checks failed
"""

import argparse
import json
import logging
import os
import random
import sys
import time
import uuid

import urllib.request
import urllib.error
import urllib.parse

# ── Config (override via CLI args or env vars) ─────────────────────────────────
DEFAULTS = {
    "host":              os.getenv("JELLYFIN_HOST",           "http://localhost:8096"),
    "user":              os.getenv("JELLYFIN_USER",           "admin"),
    "password":          os.getenv("JELLYFIN_PASS",           ""),
    "timeout":           int(os.getenv("JELLYFIN_TIMEOUT",    "10")),   # seconds, general
    "transcode_timeout": int(os.getenv("JELLYFIN_TC_TIMEOUT", "45")),   # seconds, transcode startup
    "log_file":          os.getenv("JELLYFIN_LOG",            ""),       # blank = stdout only
    "stream_bytes":      65536,   # 64 KB — enough to confirm bytes are flowing
}

# Unique device ID so Jellyfin doesn't confuse repeated runs
_DEVICE_ID = f"healthcheck-{uuid.uuid4().hex[:8]}"

def _auth_header(token=None):
    h = (
        f'MediaBrowser Client="HealthCheck", Device="Script", '
        f'DeviceId="{_DEVICE_ID}", Version="1.0.0"'
    )
    if token:
        h += f', Token="{token}"'
    return {"Authorization": h}


# ── Logging setup ──────────────────────────────────────────────────────────────
def setup_logging(log_file: str) -> logging.Logger:
    log = logging.getLogger("jellyfin_hc")
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    return log


# ── HTTP helpers ───────────────────────────────────────────────────────────────
def _request(method, url, headers, body=None, timeout=10):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(req, timeout=timeout)


def api_get(host, path, token, timeout, params=None):
    if params:
        path += "?" + urllib.parse.urlencode(params)
    resp = _request("GET", host.rstrip("/") + path, _auth_header(token), timeout=timeout)
    return json.loads(resp.read())


def api_post(host, path, body, timeout):
    resp = _request("POST", host.rstrip("/") + path, _auth_header(), body, timeout=timeout)
    return json.loads(resp.read())


# ── Individual checks ──────────────────────────────────────────────────────────

def check_reachable(host, timeout, log):
    """[1/5] Basic HTTP reachability via /health."""
    t0 = time.time()
    try:
        resp = _request("GET", host.rstrip("/") + "/health", _auth_header(), timeout=timeout)
        ms = (time.time() - t0) * 1000
        log.info(f"[1/5] Reachable   ✓  ({ms:.0f} ms)  status={resp.status}")
        return True
    except Exception as e:
        log.error(f"[1/5] Reachable   ✗  {e}")
        return False


def check_auth(host, user, password, timeout, log):
    """[2/5] Authenticate and return (token, user_id)."""
    t0 = time.time()
    try:
        data    = api_post(host, "/Users/AuthenticateByName",
                           {"Username": user, "Pw": password}, timeout)
        token   = data["AccessToken"]
        user_id = data["User"]["Id"]
        ms = (time.time() - t0) * 1000
        log.info(f"[2/5] Auth        ✓  ({ms:.0f} ms)  user_id={user_id[:8]}…")
        return token, user_id
    except urllib.error.HTTPError as e:
        log.error(f"[2/5] Auth        ✗  HTTP {e.code} — check credentials")
    except Exception as e:
        log.error(f"[2/5] Auth        ✗  {e}")
    return None, None


def check_library(host, token, user_id, timeout, log):
    """[3/5] Fetch two distinct random video items for independent stream checks."""
    t0 = time.time()
    try:
        data = api_get(host, f"/Users/{user_id}/Items", token, timeout, params={
            "IncludeItemTypes": "Movie,Episode",
            "Recursive":        "true",
            "Fields":           "MediaSources",
            "SortBy":           "Random",
            "Limit":            10,
        })
        items = data.get("Items", [])
        if not items:
            log.warning("[3/5] Library    ✗  No video items found")
            return None, None

        if len(items) >= 2:
            direct_item, transcode_item = random.sample(items, 2)
        else:
            direct_item = transcode_item = items[0]

        ms = (time.time() - t0) * 1000
        log.info(
            f"[3/5] Library    ✓  ({ms:.0f} ms)  "
            f"direct='{direct_item.get('Name','?')}' ({direct_item.get('Type')})  "
            f"transcode='{transcode_item.get('Name','?')}' ({transcode_item.get('Type')})"
        )
        return direct_item, transcode_item
    except Exception as e:
        log.error(f"[3/5] Library    ✗  {e}")
        return None, None


def check_direct_stream(host, token, item, cfg, log):
    """
    [4/5] Direct / static stream check.
    Static=true tells Jellyfin to serve the raw container file with no
    transcoding.  A Range header grabs only the first 64 KB; a 206 response
    with actual bytes is the success condition.
    """
    t0 = time.time()
    item_id = item["Id"]
    url = (
        f"{host.rstrip('/')}/Videos/{item_id}/stream"
        f"?Static=true&api_key={token}"
    )
    headers = {
        **_auth_header(token),
        "Range": f"bytes=0-{cfg['stream_bytes'] - 1}",
    }
    try:
        req   = urllib.request.Request(url, headers=headers, method="GET")
        resp  = urllib.request.urlopen(req, timeout=cfg["timeout"])
        chunk = resp.read(cfg["stream_bytes"])
        ms    = (time.time() - t0) * 1000

        if not chunk:
            log.error(f"[4/5] Direct     ✗  Got 0 bytes (status={resp.status})")
            return False

        log.info(
            f"[4/5] Direct     ✓  ({ms:.0f} ms)  "
            f"status={resp.status}  bytes={len(chunk)}"
        )
        return True

    except urllib.error.HTTPError as e:
        body = e.read(256).decode(errors="replace")
        log.error(f"[4/5] Direct     ✗  HTTP {e.code}  {body[:120]}")
    except Exception as e:
        log.error(f"[4/5] Direct     ✗  {e}")
    return False


def check_transcode_stream(host, token, item, cfg, log):
    """
    [5/5] Transcode pipeline check via the HLS pathway.

    Flow:
      a) GET /Videos/{id}/master.m3u8  — asks Jellyfin to build an HLS
         transcode session (VideoCodec=h264, AudioCodec=aac).  Jellyfin
         returns a playlist even before FFmpeg finishes its first segment.
      b) Parse the playlist to find the highest-bandwidth variant stream URL.
      c) GET that variant's .m3u8 to get the segment list.
      d) Fetch the first .ts segment and read bytes — this is the proof that
         FFmpeg ran and produced real output.

    Why HLS and not the raw stream endpoint:
      The /stream endpoint requires Jellyfin to infer a MediaSourceId and
      PlaySessionId internally; getting those wrong produces a 500.  The HLS
      master playlist endpoint is the canonical transcode entry point in
      Jellyfin and handles session management automatically.

    Uses a longer timeout (default 45 s) for FFmpeg startup latency.
    Sessions are abandoned after the probe; Jellyfin auto-cleans them.
    """
    t0 = time.time()
    item_id = item["Id"]

    def _get_raw(url, timeout):
        """GET a URL and return (response_object, body_bytes)."""
        req  = urllib.request.Request(url, headers=_auth_header(token), method="GET")
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp, resp.read()

    # ── (pre) Get PlaybackInfo — Jellyfin returns the canonical TranscodingUrl ─
    try:
        playback_info = _request(
            "POST",
            host.rstrip("/") + f"/Items/{item_id}/PlaybackInfo?UserId={cfg.get('user_id', '')}",
            {**_auth_header(token), "Content-Type": "application/json"},
            body={"DeviceProfile": {
                "MaxStreamingBitrate": 8000000,
                "TranscodingProfiles": [{
                    "Type": "Video",
                    "Container": "ts",
                    "VideoCodec": "h264",
                    "AudioCodec": "aac",
                    "Protocol": "hls",
                }],
            }},
            timeout=cfg["timeout"],
        )
        pb_data = json.loads(playback_info.read())
        media_sources = pb_data.get("MediaSources", [])
        transcoding_url = None
        for ms in media_sources:
            if ms.get("TranscodingUrl"):
                transcoding_url = ms["TranscodingUrl"]
                break
        if not transcoding_url:
            log.error("[5/5] Transcode  ✗  PlaybackInfo returned no TranscodingUrl (item may direct-play)")
            return False
        log.debug(f"[5/5]   PlaybackInfo OK  TranscodingUrl present")
    except Exception as e:
        log.error(f"[5/5] Transcode  ✗  PlaybackInfo → {e}")
        return False

    # ── (a) Request the HLS master playlist using Jellyfin's own URL ──────────
    master_url = host.rstrip("/") + transcoding_url

    try:
        resp, master_body = _get_raw(master_url, cfg["transcode_timeout"])
        master_text = master_body.decode("utf-8", errors="replace")
        log.debug(f"[5/5]   master.m3u8 status={resp.status}  bytes={len(master_body)}")
    except urllib.error.HTTPError as e:
        body = e.read(256).decode(errors="replace")
        log.error(f"[5/5] Transcode  ✗  master.m3u8 → HTTP {e.code}  {body[:120]}")
        return False
    except urllib.error.URLError as e:
        ms = (time.time() - t0) * 1000
        if "timed out" in str(e).lower():
            log.error(
                f"[5/5] Transcode  ✗  master.m3u8 timed out after {ms:.0f} ms "
                f"(limit={cfg['transcode_timeout']}s) — FFmpeg may be overloaded or missing"
            )
        else:
            log.error(f"[5/5] Transcode  ✗  master.m3u8 → {e}")
        return False
    except Exception as e:
        log.error(f"[5/5] Transcode  ✗  master.m3u8 → {e}")
        return False

    # ── (b) Parse master playlist → variant stream URL ────────────────────────
    # M3U8 master playlists list variant streams after #EXT-X-STREAM-INF lines.
    # We pick the first (or highest bandwidth) variant.
    base_url = master_url.rsplit("?", 1)[0].rsplit("/", 1)[0]
    variant_url = None
    lines = master_text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            # Next non-comment line is the variant URI
            for j in range(i + 1, len(lines)):
                candidate = lines[j].strip()
                if candidate and not candidate.startswith("#"):
                    variant_url = (
                        candidate if candidate.startswith("http")
                        else f"{base_url}/{candidate}"
                    )
                    break
            if variant_url:
                break

    if not variant_url:
        # Some Jellyfin versions return a media playlist directly (no master)
        # Check if this IS already a media playlist
        if "#EXTINF" in master_text or "#EXT-X-TARGETDURATION" in master_text:
            log.debug("[5/5]   master.m3u8 is already a media playlist — using directly")
            variant_url = master_url
        else:
            log.error(
                f"[5/5] Transcode  ✗  Could not parse a variant stream from master.m3u8\n"
                f"      Playlist content (first 300 chars): {master_text[:300]}"
            )
            return False

    log.debug(f"[5/5]   variant → {variant_url}")

    # ── (c) Fetch the media playlist (variant .m3u8) to get segment list ─────
    try:
        _, variant_body = _get_raw(variant_url, cfg["transcode_timeout"])
        variant_text = variant_body.decode("utf-8", errors="replace")
    except Exception as e:
        log.error(f"[5/5] Transcode  ✗  variant.m3u8 → {e}")
        return False

    # ── (d) Find first .ts segment and read bytes ─────────────────────────────
    segment_url = None
    variant_base = variant_url.rsplit("?", 1)[0].rsplit("/", 1)[0]
    for line in variant_text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            segment_url = (
                line if line.startswith("http")
                else f"{variant_base}/{line}"
            )
            break

    if not segment_url:
        # Playlist may still be empty — FFmpeg hasn't written a segment yet.
        # Retry a few times before giving up.
        log.debug("[5/5]   No segments yet — waiting for FFmpeg to write first segment…")
        for attempt in range(6):
            time.sleep(3)
            try:
                _, variant_body = _get_raw(variant_url, cfg["timeout"])
                variant_text = variant_body.decode("utf-8", errors="replace")
                for line in variant_text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        segment_url = (
                            line if line.startswith("http")
                            else f"{variant_base}/{line}"
                        )
                        break
                if segment_url:
                    log.debug(f"[5/5]   Got first segment after {(attempt+1)*3}s")
                    break
            except Exception:
                pass

    if not segment_url:
        ms = (time.time() - t0) * 1000
        log.error(
            f"[5/5] Transcode  ✗  FFmpeg produced no segments after {ms:.0f} ms — "
            "check Jellyfin logs for FFmpeg errors"
        )
        return False

    log.debug(f"[5/5]   segment → {segment_url}")

    # Retry segment fetch — FFmpeg may still be encoding the first segment
    last_err = None
    for attempt in range(10):
        try:
            req = urllib.request.Request(
                segment_url,
                headers=_auth_header(token),
                method="GET",
            )
            resp  = urllib.request.urlopen(req, timeout=cfg["timeout"])
            chunk = resp.read(cfg["stream_bytes"])
            ms    = (time.time() - t0) * 1000

            if not chunk:
                log.error(f"[5/5] Transcode  ✗  Segment returned 0 bytes (status={resp.status})")
                return False

            log.info(
                f"[5/5] Transcode  ✓  ({ms:.0f} ms)  "
                f"status={resp.status}  bytes={len(chunk)}"
            )
            return True

        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}  {e.read(256).decode(errors='replace')[:120]}"
            if e.code in (500, 503) and (time.time() - t0) < cfg["transcode_timeout"]:
                log.debug(f"[5/5]   segment attempt {attempt+1} → {last_err} — retrying in 3s")
                time.sleep(3)
                continue
            break
        except Exception as e:
            last_err = str(e)
            break

    log.error(f"[5/5] Transcode  ✗  segment → {last_err}")
    return False


# ── Orchestrator ───────────────────────────────────────────────────────────────
def run(cfg: dict) -> int:
    log = setup_logging(cfg["log_file"])
    log.info("=" * 60)
    log.info(f"Jellyfin health check  →  {cfg['host']}")
    log.info("=" * 60)

    results = {}

    results["reachable"] = check_reachable(cfg["host"], cfg["timeout"], log)
    if not results["reachable"]:
        log.error("Server unreachable — aborting.")
        _summary(results, log)
        return 1

    token, user_id = check_auth(cfg["host"], cfg["user"], cfg["password"], cfg["timeout"], log)
    results["auth"] = token is not None
    if not results["auth"]:
        _summary(results, log)
        return 1

    direct_item, transcode_item = check_library(
        cfg["host"], token, user_id, cfg["timeout"], log)
    results["library"] = direct_item is not None
    if not results["library"]:
        _summary(results, log)
        return 1

    # Both stream checks always run — failures are independent
    results["direct_stream"] = check_direct_stream(
        cfg["host"], token, direct_item, cfg, log)

    cfg["user_id"] = user_id
    results["transcode"] = check_transcode_stream(
        cfg["host"], token, transcode_item, cfg, log)

    _summary(results, log)
    return 0 if all(results.values()) else 1


def _summary(results: dict, log):
    log.info("-" * 60)
    ok = all(results.values())
    log.info(f"Result: {'HEALTHY ✓' if ok else 'DEGRADED ✗'}")
    for k, v in results.items():
        log.info(f"  {k:<18} {'✓' if v else '✗'}")
    log.info("=" * 60)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Jellyfin deep health check")
    p.add_argument("--host",              default=DEFAULTS["host"])
    p.add_argument("--user",              default=DEFAULTS["user"])
    p.add_argument("--pass",              dest="password", default=DEFAULTS["password"])
    p.add_argument("--timeout",           type=int, default=DEFAULTS["timeout"],
                   help="Request timeout in seconds (default: 10)")
    p.add_argument("--transcode-timeout", type=int, default=DEFAULTS["transcode_timeout"],
                   dest="transcode_timeout",
                   help="Transcode startup timeout in seconds (default: 45)")
    p.add_argument("--log",               default=DEFAULTS["log_file"],
                   help="Append output to this log file")
    args = p.parse_args()

    cfg = {
        "host":              args.host,
        "user":              args.user,
        "password":          args.password,
        "timeout":           args.timeout,
        "transcode_timeout": args.transcode_timeout,
        "log_file":          args.log,
        "stream_bytes":      DEFAULTS["stream_bytes"],
    }

    sys.exit(run(cfg))

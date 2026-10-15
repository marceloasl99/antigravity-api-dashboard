"""
Token usage tracking module for Antigravity API.
Maintains persistent, accumulative token usage by hour, day, and month in /root/agy_token_usage.json.
Safe across multi-process Gunicorn workers using file locking (fcntl).
"""

import fcntl
import json
import os
from datetime import datetime, timezone

TOKEN_USAGE_FILE = os.getenv("TOKEN_USAGE_FILE", "/root/agy_token_usage.json")
LOCK_FILE = os.getenv("TOKEN_LOCK_FILE", "/root/agy_token_usage.json.lock")


def _empty_bucket():
    return {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def _init_data():
    return {
        "all_time": _empty_bucket(),
        "by_hour": {},
        "by_day": {},
        "by_month": {},
        "by_profile": {
            "p1": _empty_bucket(),
            "p2": _empty_bucket(),
            "p3": _empty_bucket(),
        },
        "recent_requests": [],
        "last_updated": None,
    }


def record_tokens(profile_id: str, model: str, prompt_tokens: int, completion_tokens: int):
    """
    Safely accumulates token usage across all time horizons (hour, day, month, all-time, profile).
    """
    p_tok = max(0, int(prompt_tokens or 0))
    c_tok = max(0, int(completion_tokens or 0))
    tot = p_tok + c_tok
    now = datetime.now(timezone.utc)
    hour_key = now.strftime("%Y-%m-%d %H:00 UTC")
    day_key = now.strftime("%Y-%m-%d")
    month_key = now.strftime("%Y-%m")
    ts_iso = now.isoformat()

    try:
        with open(LOCK_FILE, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                data = None
                if os.path.exists(TOKEN_USAGE_FILE):
                    try:
                        with open(TOKEN_USAGE_FILE, "r") as f:
                            data = json.load(f)
                    except Exception:
                        data = None

                if not data or not isinstance(data, dict):
                    data = _init_data()

                # Ensure base keys exist
                for key in ("all_time", "by_hour", "by_day", "by_month", "by_profile", "recent_requests"):
                    if key not in data:
                        data[key] = [] if key == "recent_requests" else {}

                # 1. All-time
                at = data["all_time"]
                at["requests"] = at.get("requests", 0) + 1
                at["prompt_tokens"] = at.get("prompt_tokens", 0) + p_tok
                at["completion_tokens"] = at.get("completion_tokens", 0) + c_tok
                at["total_tokens"] = at.get("total_tokens", 0) + tot

                # 2. By hour
                if hour_key not in data["by_hour"]:
                    data["by_hour"][hour_key] = _empty_bucket()
                h = data["by_hour"][hour_key]
                h["requests"] += 1
                h["prompt_tokens"] += p_tok
                h["completion_tokens"] += c_tok
                h["total_tokens"] += tot

                # Prune old hours if > 168 (7 days)
                if len(data["by_hour"]) > 168:
                    sorted_h = sorted(data["by_hour"].keys())
                    for old_h in sorted_h[:-168]:
                        data["by_hour"].pop(old_h, None)

                # 3. By day
                if day_key not in data["by_day"]:
                    data["by_day"][day_key] = _empty_bucket()
                d = data["by_day"][day_key]
                d["requests"] += 1
                d["prompt_tokens"] += p_tok
                d["completion_tokens"] += c_tok
                d["total_tokens"] += tot

                # 4. By month
                if month_key not in data["by_month"]:
                    data["by_month"][month_key] = _empty_bucket()
                m = data["by_month"][month_key]
                m["requests"] += 1
                m["prompt_tokens"] += p_tok
                m["completion_tokens"] += c_tok
                m["total_tokens"] += tot

                # 5. By profile
                pid = str(profile_id or "p1")
                if pid not in data["by_profile"]:
                    data["by_profile"][pid] = _empty_bucket()
                prof = data["by_profile"][pid]
                prof["requests"] = prof.get("requests", 0) + 1
                prof["prompt_tokens"] = prof.get("prompt_tokens", 0) + p_tok
                prof["completion_tokens"] = prof.get("completion_tokens", 0) + c_tok
                prof["total_tokens"] = prof.get("total_tokens", 0) + tot

                # 6. Recent requests
                recent = data.get("recent_requests", [])
                recent.insert(0, {
                    "timestamp": ts_iso,
                    "profile": pid,
                    "model": model or "gemini-3.8-flash",
                    "prompt_tokens": p_tok,
                    "completion_tokens": c_tok,
                    "total_tokens": tot,
                })
                data["recent_requests"] = recent[:20]
                data["last_updated"] = ts_iso

                # Atomic write
                tmp_path = TOKEN_USAGE_FILE + ".tmp"
                with open(tmp_path, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, TOKEN_USAGE_FILE)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception as e:
        print(f"[token_tracker] Error recording tokens: {e}")


def get_token_usage_summary() -> dict:
    """
    Returns the token usage summary ready for JSON response and UI display.
    Computes current_hour, today, this_month based on current UTC time.
    """
    now = datetime.now(timezone.utc)
    hour_key = now.strftime("%Y-%m-%d %H:00 UTC")
    day_key = now.strftime("%Y-%m-%d")
    month_key = now.strftime("%Y-%m")

    data = None
    if os.path.exists(TOKEN_USAGE_FILE):
        try:
            with open(TOKEN_USAGE_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            data = None

    if not data or not isinstance(data, dict):
        data = _init_data()

    all_time = data.get("all_time", _empty_bucket())
    by_hour = data.get("by_hour", {})
    by_day = data.get("by_day", {})
    by_month = data.get("by_month", {})
    by_profile = data.get("by_profile", {})
    recent_requests = data.get("recent_requests", [])

    current_hour_data = by_hour.get(hour_key, _empty_bucket())
    today_data = by_day.get(day_key, _empty_bucket())
    this_month_data = by_month.get(month_key, _empty_bucket())

    # Build sorted lists for tables (newest first)
    sorted_hours = [
        {"period": k, **v} for k, v in sorted(by_hour.items(), reverse=True)
    ]
    sorted_days = [
        {"period": k, **v} for k, v in sorted(by_day.items(), reverse=True)
    ]
    sorted_months = [
        {"period": k, **v} for k, v in sorted(by_month.items(), reverse=True)
    ]

    return {
        "ok": True,
        "current_hour": {
            "period": hour_key,
            **current_hour_data,
        },
        "today": {
            "period": day_key,
            **today_data,
        },
        "this_month": {
            "period": month_key,
            **this_month_data,
        },
        "all_time": all_time,
        "by_hour_list": sorted_hours,
        "by_day_list": sorted_days,
        "by_month_list": sorted_months,
        "by_profile": by_profile,
        "recent_requests": recent_requests,
        "last_updated": data.get("last_updated"),
        "raw_by_hour": by_hour,
        "raw_by_day": by_day,
        "raw_by_month": by_month,
    }

#!/usr/bin/env python3
"""Read-only, aggregate OneLog snapshot for the daily Teams report.

Installed as a forced SSH command on the production EC2. It emits only counts
and health state; account names, request IDs, IPs, and cookie values stay local.
"""

import collections
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo


TZ = ZoneInfo("America/Fortaleza")
UTC = dt.timezone.utc
APP_ID = "vo4k08cwc444sgkgsgog8gcs"
REUSE_MINUTES = 22
ERROR_OUTCOMES = {
    "ad_denied": "Autenticação Windows/AD (401)",
    "missing_credentials": "Credenciais ausentes (400)",
    "no_account": "Conta do setor ausente",
    "not_found": "Sessão não encontrada",
    "api_error": "Erro interno da API",
}
REQUEST_RE = re.compile(r"\[REQ\] endpoint=(\S+) outcome=(\S+)")


def run(*args):
    result = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=45, check=True,
    )
    return result.stdout


def windows(slot, now):
    day = now.date()
    if slot == "morning":
        end = dt.datetime.combine(day, dt.time(8), TZ)
        start = end - dt.timedelta(hours=11)
    elif slot == "midday":
        start = dt.datetime.combine(day, dt.time(8), TZ)
        end = dt.datetime.combine(day, dt.time(13), TZ)
    elif slot == "evening":
        start = dt.datetime.combine(day, dt.time(13), TZ)
        end = dt.datetime.combine(day, dt.time(21), TZ)
    else:
        raise ValueError("invalid slot")
    if now < end:
        raise ValueError("report window has not ended")
    return start, end


def container_name(role):
    names = run("sudo", "docker", "ps", "--format", "{{.Names}}").splitlines()
    matches = [name for name in names if name.startswith(f"{role}-{APP_ID}-")]
    if len(matches) != 1:
        raise RuntimeError(f"expected one running OneLog {role} container, found {len(matches)}")
    return matches[0]


def shared_directory(container):
    mounts = json.loads(run("sudo", "docker", "inspect", "--format", "{{json .Mounts}}", container))
    matches = [Path(item["Source"]) for item in mounts if item.get("Destination") == "/app/shared"]
    if len(matches) != 1:
        raise RuntimeError("OneLog shared log volume unavailable")
    return matches[0]


def log_lines(directory, channel, start, end):
    if os.geteuid() != 0:
        raise PermissionError("snapshot must run as root to read persistent logs")
    paths = sorted(directory.glob(f"{channel}_debug.log*"))
    if not paths:
        raise RuntimeError(f"{channel} persistent log missing")
    eligible = [path for path in paths if path.stat().st_mtime >= start.timestamp()]
    for path in eligible:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if channel == "worker":
                    if not any(marker in line for marker in (
                        "=== TENTATIVA ", "Acesso concedido e salvo no Pool!",
                        "FALHA DEFINITIVA APÓS", "Armadilha Cloudflare: campo de senha ausente",
                    )):
                        continue
                elif "[REQ]" not in line:
                    continue
                try:
                    moment = dt.datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)
                except ValueError:
                    continue
                if start <= moment < end:
                    yield moment, line[26:]


def count_worker(lines, start, end):
    attempts = successes = exhausted = challenge_failures = 0
    saved = []
    exhausted_at = []
    for moment, message in lines:
        if "Acesso concedido e salvo no Pool!" in message:
            saved.append(moment)
        if not start <= moment < end:
            continue
        attempts += "=== TENTATIVA " in message
        successes += "Acesso concedido e salvo no Pool!" in message
        if "FALHA DEFINITIVA APÓS" in message:
            exhausted += 1
            exhausted_at.append(moment)
        challenge_failures += "Armadilha Cloudflare: campo de senha ausente" in message
    saved.sort()
    previous = [stamp for stamp in saved if stamp < start]
    current = [stamp for stamp in saved if start <= stamp < end]
    series = (previous[-1:] if previous else []) + current
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(series, series[1:])]
    last_login = saved[-1] if saved else None
    return {
        "attempts": attempts,
        "successes": successes,
        "exhausted": exhausted,
        "successes_after_last_exhausted": (
            sum(stamp > max(exhausted_at) for stamp in current) if exhausted_at else 0
        ),
        "challenge_failures": challenge_failures,
        "last_login": last_login.astimezone(TZ).isoformat() if last_login else None,
        "last_login_age_minutes": round((end - last_login).total_seconds() / 60, 1) if last_login else None,
        "max_login_gap_minutes": round(max(gaps), 1) if gaps else None,
    }


def count_api(lines, start, end):
    errors = collections.Counter()
    requests = 0
    client_requests = 0
    after_19_requests = 0
    for moment, message in lines:
        if not start <= moment < end:
            continue
        match = REQUEST_RE.search(message)
        if not match:
            continue
        requests += 1
        endpoint, outcome = match.groups()
        if endpoint in {"login", "renew", "session"}:
            client_requests += 1
            if moment.astimezone(TZ).hour >= 19:
                after_19_requests += 1
        if outcome in ERROR_OUTCOMES:
            errors[outcome] += 1
    return {
        "requests": requests,
        "client_requests": client_requests,
        "after_19_requests": after_19_requests,
        "errors": dict(errors),
    }


def api_status():
    url = "http://127.0.0.1:5000/api/zerocore/status?setor=BB_Robos"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.load(response)
            return {"http": response.status, "session_ready": bool(payload.get("concluido"))}
    except (OSError, ValueError, urllib.error.HTTPError):
        return {"http": None, "session_ready": None}


def queue_status(redis_container):
    commands = {
        "immediate": ("LLEN", "queue:login_requests"),
        "priority": ("LLEN", "queue:priority_logins"),
        "delayed": ("ZCARD", "queue:delayed_login_requests"),
    }
    return {
        key: int(run("sudo", "docker", "exec", redis_container, "redis-cli", *command).strip())
        for key, command in commands.items()
    }


def main():
    slot = os.environ.get("SSH_ORIGINAL_COMMAND") or (sys.argv[1] if len(sys.argv) == 2 else "")
    if slot not in {"morning", "midday", "evening"}:
        raise ValueError("expected morning, midday, or evening")
    now = dt.datetime.now(TZ)
    start, end = windows(slot, now)
    worker = container_name("worker")
    api = container_name("api")
    redis = container_name("redis")
    # A predecessor login is needed to measure the first gap in the window.
    worker_start = start - dt.timedelta(hours=36)
    worker_stats = count_worker(log_lines(shared_directory(worker), "worker", worker_start, end), start, end)
    api_stats = count_api(log_lines(shared_directory(api), "api", start, end), start, end)
    snapshot = {
        "schema": 1,
        "slot": slot,
        "date": end.date().isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "collected_at": now.isoformat(),
        "reuse_minutes": REUSE_MINUTES,
        "worker": worker_stats,
        "api": {**api_stats, **api_status()},
        "queues": queue_status(redis),
    }
    print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()

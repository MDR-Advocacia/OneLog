from flask import Flask, jsonify, request, send_from_directory, g, Response
from flask_cors import CORS
import redis
import json
import os
import time
import secrets
import logging
import sys
import re
from collections import Counter, deque
from datetime import datetime, timedelta
from database import SessionLocal, Sector, AccountBB, init_db, seed_db
from ad_integration import autenticar_e_obter_setor, autenticar_admin_ad, listar_ous_bb_ad
from functools import wraps
from zoneinfo import ZoneInfo

app = Flask(__name__)
CORS(app)

APP_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.join(APP_BASE_DIR, 'shared')
if not os.path.exists(SHARED_DIR):
    os.makedirs(SHARED_DIR)

api_logger = logging.getLogger("onelog.api")
if not api_logger.handlers:
    api_logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - API - %(message)s')
    file_handler = logging.FileHandler(os.path.join(SHARED_DIR, "api_debug.log"), encoding='utf-8')
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    api_logger.addHandler(file_handler)
    api_logger.addHandler(stream_handler)
    api_logger.propagate = False

ADMIN_BREAKGLASS_TOKEN = os.getenv("ADMIN_BREAKGLASS_TOKEN", "").strip()
redis_client = redis.Redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)
COOKIE_REUSE_MINUTES = int(os.getenv("COOKIE_REUSE_MINUTES", "20"))
COOKIE_SOFT_REFRESH_MINUTES = int(os.getenv("COOKIE_SOFT_REFRESH_MINUTES", str(COOKIE_REUSE_MINUTES)))
COOKIE_HARD_DELIVERY_MINUTES = int(os.getenv("COOKIE_HARD_DELIVERY_MINUTES", "20"))
COOKIE_LOGIN_HARD_DELIVERY_MINUTES = int(os.getenv("COOKIE_LOGIN_HARD_DELIVERY_MINUTES", "2"))
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "America/Fortaleza")
RECENT_REQUESTS_LIMIT = int(os.getenv("RECENT_REQUESTS_LIMIT", "300"))
ACTIVE_CLIENT_TTL_SECONDS = int(os.getenv("ACTIVE_CLIENT_TTL_SECONDS", "2700"))
RECENT_REQUEST_WINDOW_MINUTES = int(os.getenv("RECENT_REQUEST_WINDOW_MINUTES", "30"))
ADMIN_SESSION_TTL_SECONDS = int(os.getenv("ADMIN_SESSION_TTL_SECONDS", "28800"))
LOG_TAIL_LIMIT = int(os.getenv("LOG_TAIL_LIMIT", "200"))
CACHE_SETTLE_GRACE_SECONDS = int(os.getenv("CACHE_SETTLE_GRACE_SECONDS", "45"))
CACHE_REENTRY_FORCE_FRESH_SECONDS = int(os.getenv("CACHE_REENTRY_FORCE_FRESH_SECONDS", "900"))
LOG_LINE_RE = re.compile(r'^(?P<raw_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s-\s(?P<channel>[A-Z_]+)\s-\s(?P<message>.*)$')

def get_local_now():
    try:
        return datetime.now(ZoneInfo(APP_TIMEZONE))
    except Exception:
        return datetime.utcnow() - timedelta(hours=3)

def extract_client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.headers.get("X-Real-IP") or request.remote_addr or "-"

def trim_user_agent(user_agent, max_len=120):
    if not user_agent:
        return ""
    user_agent = str(user_agent)
    return user_agent if len(user_agent) <= max_len else user_agent[:max_len - 1] + "…"

def make_requester_cache_key(requester):
    if not requester:
        return None
    base = requester.get("username") or requester.get("display_name")
    if not base:
        return None
    return re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(base).strip().lower())[:120] or None

def push_recent_request(event):
    redis_client.lpush("admin:recent_requests", json.dumps(event, ensure_ascii=False))
    redis_client.ltrim("admin:recent_requests", 0, RECENT_REQUESTS_LIMIT - 1)

def mark_live_activity(setor_nome=None, account=None, endpoint=None, outcome=None, user_agent=None, requester=None, request_id=None):
    now = datetime.utcnow()
    payload = {
        "setor": setor_nome,
        "endpoint": endpoint,
        "outcome": outcome,
        "ip": extract_client_ip(),
        "user_agent": trim_user_agent(user_agent or request.headers.get("User-Agent") or ""),
        "last_seen_at": now.isoformat(),
        "last_seen_local": get_local_now().strftime("%d/%m/%Y %H:%M:%S")
    }
    if account:
        payload["account_id"] = account.id
        payload["login"] = account.login
    if requester:
        payload["requester_username"] = requester.get("username")
        payload["requester_display_name"] = requester.get("display_name")
    if request_id:
        payload["request_id"] = request_id
    if setor_nome:
        redis_client.setex(f"live:setor:{setor_nome}", ACTIVE_CLIENT_TTL_SECONDS, json.dumps(payload, ensure_ascii=False))
    if account:
        redis_client.setex(f"live:account:{account.id}", ACTIVE_CLIENT_TTL_SECONDS, json.dumps(payload, ensure_ascii=False))
    requester_key = make_requester_cache_key(requester)
    if requester_key:
        redis_client.setex(f"live:requester:{requester_key}", ACTIVE_CLIENT_TTL_SECONDS, json.dumps(payload, ensure_ascii=False))

def make_cache_reentry_guard_key(account=None, requester=None):
    requester_key = make_requester_cache_key(requester)
    if not account or not getattr(account, "id", None) or not requester_key:
        return None
    return f"guard:recent_cache_login:{account.id}:{requester_key}"

def get_recent_cache_delivery_state(account=None, requester=None):
    key = make_cache_reentry_guard_key(account=account, requester=requester)
    if not key:
        return None
    return parse_json_safe(redis_client.get(key)) or None

def get_recent_cache_delivery_age_seconds(account=None, requester=None):
    data = get_recent_cache_delivery_state(account=account, requester=requester)
    ts = parse_iso_timestamp((data or {}).get("ts"))
    if not ts:
        return None
    ts_utc = ts.astimezone(ZoneInfo("UTC")).replace(tzinfo=None) if ts.tzinfo else ts
    return max(0, int((datetime.utcnow() - ts_utc).total_seconds()))

def mark_recent_cache_delivery(account=None, requester=None):
    key = make_cache_reentry_guard_key(account=account, requester=requester)
    if key:
        redis_client.setex(
            key,
            CACHE_REENTRY_FORCE_FRESH_SECONDS,
            json.dumps({"ts": datetime.utcnow().isoformat()}, ensure_ascii=False),
        )

def clear_recent_cache_delivery(account=None, requester=None):
    key = make_cache_reentry_guard_key(account=account, requester=requester)
    if key:
        redis_client.delete(key)

def record_request_event(endpoint, setor_nome=None, account=None, outcome=None, user_agent=None, cookie_age_minutes=None, extra=None, requester=None, request_id=None):
    now_utc = datetime.utcnow()
    today = now_utc.strftime('%Y-%m-%d')
    event = {
        "ts": now_utc.isoformat(),
        "ts_local": get_local_now().strftime("%d/%m/%Y %H:%M:%S"),
        "endpoint": endpoint,
        "setor": setor_nome,
        "outcome": outcome,
        "ip": extract_client_ip(),
        "user_agent": trim_user_agent(user_agent or request.headers.get("User-Agent") or "")
    }
    if account:
        event["account_id"] = account.id
        event["login"] = account.login
    if requester:
        event["requester_username"] = requester.get("username")
        event["requester_display_name"] = requester.get("display_name")
    if request_id:
        event["request_id"] = request_id
    if cookie_age_minutes is not None:
        event["cookie_age_minutes"] = round(cookie_age_minutes, 2)
        redis_client.incrbyfloat(f"metrics:cookie_age_sum:{today}", float(cookie_age_minutes))
        redis_client.incr(f"metrics:cookie_age_count:{today}")
    if extra:
        event.update(extra)

    redis_client.incr(f"metrics:request_total:{today}")
    redis_client.hincrby(f"metrics:request_endpoint:{today}", endpoint, 1)
    if outcome:
        redis_client.hincrby(f"metrics:request_outcome:{today}", outcome, 1)
    push_recent_request(event)
    api_logger.info(
        "[REQ] endpoint=%s outcome=%s setor=%s account=%s requester=%s req=%s ip=%s cookie_age=%s",
        endpoint,
        outcome or "-",
        setor_nome or "-",
        account.login if account else "-",
        (requester or {}).get("username") or "-",
        request_id or "-",
        event["ip"],
        event.get("cookie_age_minutes", "-"),
    )
    mark_live_activity(
        setor_nome=setor_nome,
        account=account,
        endpoint=endpoint,
        outcome=outcome,
        user_agent=user_agent,
        requester=requester,
        request_id=request_id
    )

def record_session_cycle(account, new_login_at):
    if not account or not account.last_login_at:
        return
    cycle_minutes = (new_login_at - account.last_login_at).total_seconds() / 60
    if cycle_minutes <= 0:
        return
    today = new_login_at.strftime('%Y-%m-%d')
    redis_client.incrbyfloat(f"metrics:session_cycle_sum:{today}", cycle_minutes)
    redis_client.incr(f"metrics:session_cycle_count:{today}")
    previous_max = float(redis_client.get(f"metrics:session_cycle_max:{today}") or 0)
    if cycle_minutes > previous_max:
        redis_client.set(f"metrics:session_cycle_max:{today}", round(cycle_minutes, 2))

def get_cookie_age_minutes(account):
    if not account or not account.last_login_at:
        return None
    return (datetime.utcnow() - account.last_login_at).total_seconds() / 60

def get_cookie_hard_delivery_limit_minutes(account=None, login_flow=False):
    return float(COOKIE_LOGIN_HARD_DELIVERY_MINUTES if login_flow else COOKIE_HARD_DELIVERY_MINUTES)

def can_deliver_cached_cookie(account, hard_limit_minutes=None, login_flow=False):
    if not account or not account.cookie_payload:
        return False
    cookie_age = get_cookie_age_minutes(account)
    if cookie_age is None:
        return False
    limit_minutes = (
        float(hard_limit_minutes)
        if hard_limit_minutes is not None
        else get_cookie_hard_delivery_limit_minutes(login_flow=login_flow)
    )
    return cookie_age <= limit_minutes

def should_background_refresh_cookie(account, soft_limit_minutes=COOKIE_SOFT_REFRESH_MINUTES):
    if not account or not account.cookie_payload:
        return False
    cookie_age = get_cookie_age_minutes(account)
    if cookie_age is None:
        return False
    return cookie_age >= float(soft_limit_minutes)

def enqueue_login_refresh(account, setor_nome, user_agent=None, request_id=None, requester=None, status_message=None, auto=False, force_fresh=False):
    if not account:
        return False

    lock_key = f"lock:queue:{account.id}"
    if redis_client.exists(lock_key):
        return False

    redis_client.setex(lock_key, 600, "1")
    if status_message:
        redis_client.set(
            f"status:{setor_nome}",
            json.dumps({"mensagem": status_message, "concluido": False, "erro": False})
        )

    payload = {
        "id": account.id,
        "setor": setor_nome,
        "user_agent": user_agent,
        "request_id": request_id,
        "auto": bool(auto),
        "force_fresh": bool(force_fresh),
    }
    if requester:
        payload["requester_username"] = requester.get("username")
        payload["requester_display_name"] = requester.get("display_name")

    redis_client.lpush("queue:login_requests", json.dumps(payload))
    return True

def get_queue_snapshot():
    return {
        "login_requests": redis_client.llen("queue:login_requests"),
        "priority_logins": redis_client.llen("queue:priority_logins"),
        "cooldown_active": redis_client.exists("lock:cooldown") == 1,
        "infra_cooldown_active": redis_client.exists("lock:infra_cooldown") == 1,
    }

def parse_json_safe(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

def get_requested_day(day_str=None):
    if not day_str:
        return get_local_now().strftime('%Y-%m-%d')
    try:
        return datetime.strptime(day_str, '%Y-%m-%d').strftime('%Y-%m-%d')
    except ValueError:
        return None

def parse_iso_timestamp(raw_ts):
    if not raw_ts:
        return None
    try:
        return datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
    except Exception:
        return None

def get_log_sources(source="combined"):
    selected = (source or "combined").lower()
    mapping = {
        "worker": ("worker", os.path.join(SHARED_DIR, "worker_debug.log")),
        "api": ("api", os.path.join(SHARED_DIR, "api_debug.log")),
    }
    if selected in mapping:
        return [mapping[selected]]
    return [mapping["worker"], mapping["api"]]

def iter_log_lines(file_path, source_name, day=None, contains=None):
    if not os.path.exists(file_path):
        return
    contains_filter = (contains or "").lower().strip()
    with open(file_path, 'r', encoding='utf-8', errors='replace') as handle:
        for raw_line in handle:
            line = raw_line.rstrip('\n')
            match = LOG_LINE_RE.match(line)
            if not match:
                continue
            raw_ts = match.group("raw_ts")
            if day and raw_ts[:10] != day:
                continue
            if contains_filter and contains_filter not in line.lower():
                continue
            yield {
                "source": source_name,
                "ts": raw_ts,
                "line": line,
                "message": match.group("message"),
            }

def read_log_tail(source="combined", day=None, limit=120, contains=None):
    max_lines = min(max(int(limit or 120), 1), LOG_TAIL_LIMIT)
    per_source = max_lines if source in ("api", "worker") else max_lines * 3
    items = []
    for source_name, file_path in get_log_sources(source):
        tail = deque(maxlen=per_source)
        for item in iter_log_lines(file_path, source_name, day=day, contains=contains):
            tail.append(item)
        items.extend(list(tail))
    items.sort(key=lambda item: item["ts"])
    return items[-max_lines:]

def stream_log_download(source="combined", day=None, contains=None):
    for source_name, file_path in get_log_sources(source):
        for item in iter_log_lines(file_path, source_name, day=day, contains=contains):
            yield item["line"] + "\n"

def get_queue_entries():
    items = []
    for queue_name in ("queue:priority_logins", "queue:login_requests"):
        for raw in redis_client.lrange(queue_name, 0, -1):
            payload = parse_json_safe(raw) or {}
            items.append({
                "queue": queue_name,
                "account_id": payload.get("id"),
                "setor": payload.get("setor"),
                "request_id": payload.get("request_id"),
                "requester_username": payload.get("requester_username"),
                "auto": bool(payload.get("auto")),
            })
    return items

def build_sync_pressure_snapshot(window_minutes=30):
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=window_minutes)
    recent_items = []
    for raw in redis_client.lrange("admin:recent_requests", 0, RECENT_REQUESTS_LIMIT - 1):
        data = parse_json_safe(raw)
        if not data:
            continue
        ts = parse_iso_timestamp(data.get("ts"))
        if not ts or ts < cutoff:
            continue
        recent_items.append(data)

    queue_entries = get_queue_entries()
    queue_by_sector = Counter()
    auto_queue_by_sector = Counter()
    lock_count = 0
    for key in redis_client.scan_iter("lock:queue:*"):
        if key:
            lock_count += 1
    for item in queue_entries:
        setor = item.get("setor") or "Sem setor"
        queue_by_sector[setor] += 1
        if item.get("auto"):
            auto_queue_by_sector[setor] += 1

    sectors = {}
    totals = Counter()
    for item in recent_items:
        setor = item.get("setor") or "Sem setor"
        entry = sectors.setdefault(setor, {
            "setor": setor,
            "awaiting_sync": 0,
            "status_events": 0,
            "queued": 0,
            "already_queued": 0,
            "pool_hot": 0,
            "pool_hit": 0,
            "cookie_returned": 0,
            "unique_ips": set(),
            "unique_requesters": set(),
            "last_seen": item.get("ts_local") or item.get("ts") or "-",
        })
        outcome = (item.get("outcome") or "").lower()
        endpoint = (item.get("endpoint") or "").lower()
        if outcome == "awaiting_sync":
            entry["awaiting_sync"] += 1
            totals["awaiting_sync"] += 1
        if outcome == "already_queued":
            entry["already_queued"] += 1
            totals["already_queued"] += 1
        if outcome == "queued":
            entry["queued"] += 1
            totals["queued"] += 1
        if outcome == "pool_hot":
            entry["pool_hot"] += 1
            totals["pool_hot"] += 1
        if outcome == "pool_hit":
            entry["pool_hit"] += 1
            totals["pool_hit"] += 1
        if outcome == "cookie_returned":
            entry["cookie_returned"] += 1
            totals["cookie_returned"] += 1
        if endpoint == "status":
            entry["status_events"] += 1
            totals["status_events"] += 1
        if item.get("ip"):
            entry["unique_ips"].add(item["ip"])
        requester_name = item.get("requester_username") or item.get("requester_display_name")
        if requester_name:
            entry["unique_requesters"].add(requester_name)

    sector_rows = []
    for setor, entry in sectors.items():
        queue_depth = queue_by_sector.get(setor, 0)
        auto_queue_depth = auto_queue_by_sector.get(setor, 0)
        sector_rows.append({
            "setor": setor,
            "awaiting_sync": entry["awaiting_sync"],
            "status_events": entry["status_events"],
            "queued": entry["queued"],
            "already_queued": entry["already_queued"],
            "pool_hot": entry["pool_hot"],
            "pool_hit": entry["pool_hit"],
            "cookie_returned": entry["cookie_returned"],
            "unique_ip_count": len(entry["unique_ips"]),
            "unique_requester_count": len(entry["unique_requesters"]),
            "queue_depth": queue_depth,
            "auto_queue_depth": auto_queue_depth,
            "last_seen": entry["last_seen"],
            "pressure_score": entry["awaiting_sync"] + entry["already_queued"] + (queue_depth * 3) + (auto_queue_depth * 2),
        })
    sector_rows.sort(key=lambda item: (-item["pressure_score"], -item["awaiting_sync"], item["setor"]))

    return {
        "generated_at": get_local_now().strftime("%d/%m/%Y %H:%M:%S"),
        "window_minutes": window_minutes,
        "totals": {
            "awaiting_sync": totals["awaiting_sync"],
            "status_events": totals["status_events"],
            "queued": totals["queued"],
            "already_queued": totals["already_queued"],
            "pool_hot": totals["pool_hot"],
            "pool_hit": totals["pool_hit"],
            "cookie_returned": totals["cookie_returned"],
            "queue_entries": len(queue_entries),
            "auto_queue_entries": sum(1 for item in queue_entries if item.get("auto")),
            "locked_accounts": lock_count,
        },
        "sectors": sector_rows[:20],
    }

def get_live_entries(prefix):
    items = []
    now = datetime.utcnow()
    for key in redis_client.scan_iter(prefix):
        data = parse_json_safe(redis_client.get(key))
        if not data:
            continue
        try:
            ts = datetime.fromisoformat(data["last_seen_at"])
            data["idle_seconds"] = int((now - ts).total_seconds())
        except Exception:
            data["idle_seconds"] = None
        items.append(data)
    items.sort(key=lambda x: x.get("last_seen_at", ""), reverse=True)
    return items

def build_live_requesters():
    grouped = {}
    for entry in get_live_entries("live:setor:*"):
        requester_key = entry.get("requester_username") or entry.get("requester_display_name")
        if not requester_key:
            continue
        group = grouped.setdefault(requester_key, {
            "requester_username": entry.get("requester_username"),
            "requester_display_name": entry.get("requester_display_name"),
            "idle_seconds": entry.get("idle_seconds"),
            "last_seen_at": entry.get("last_seen_at"),
            "last_seen_local": entry.get("last_seen_local"),
            "last_endpoint": entry.get("endpoint"),
            "last_outcome": entry.get("outcome"),
            "request_id": entry.get("request_id"),
            "sectors": set(),
            "accounts": set(),
            "ips": set(),
        })
        if entry.get("setor"):
            group["sectors"].add(entry["setor"])
        if entry.get("login"):
            group["accounts"].add(entry["login"])
        if entry.get("ip"):
            group["ips"].add(entry["ip"])
        idle_seconds = entry.get("idle_seconds")
        if idle_seconds is not None and (group["idle_seconds"] is None or idle_seconds < group["idle_seconds"]):
            group["idle_seconds"] = idle_seconds
            group["last_endpoint"] = entry.get("endpoint")
            group["last_outcome"] = entry.get("outcome")
            group["request_id"] = entry.get("request_id")
            group["last_seen_at"] = entry.get("last_seen_at")
            group["last_seen_local"] = entry.get("last_seen_local")

    rows = []
    for _, item in grouped.items():
        rows.append({
            "requester_username": item["requester_username"],
            "requester_display_name": item["requester_display_name"],
            "idle_seconds": item["idle_seconds"],
            "last_seen_at": item["last_seen_at"],
            "last_seen_local": item["last_seen_local"],
            "last_endpoint": item["last_endpoint"],
            "last_outcome": item["last_outcome"],
            "request_id": item["request_id"],
            "sector_count": len(item["sectors"]),
            "account_count": len(item["accounts"]),
            "ip_count": len(item["ips"]),
            "sectors": sorted(item["sectors"]),
            "accounts": sorted(item["accounts"]),
        })
    rows.sort(key=lambda x: x.get("last_seen_at", ""), reverse=True)
    return rows

def get_session_totals(accounts):
    totals = {
        "fresh": 0,
        "deliverable": 0,
        "expired": 0,
        "backoff": 0,
    }
    for acc in accounts:
        runtime = get_account_runtime(acc)
        if runtime["cookie_fresh"]:
            totals["fresh"] += 1
        if runtime["cookie_hot"]:
            totals["deliverable"] += 1
        elif acc.cookie_payload:
            totals["expired"] += 1
        if runtime["backoff_seconds"] > 0:
            totals["backoff"] += 1
    return totals

def get_worker_states():
    items = []
    now = int(time.time())
    for key in redis_client.scan_iter("worker:state:*"):
        data = parse_json_safe(redis_client.get(key))
        if not data:
            continue
        ts = int(data.get("ts") or 0)
        data["stale_seconds"] = max(0, now - ts) if ts else None
        items.append(data)
    items.sort(key=lambda x: x.get("thread_id", 0))
    return items

def get_account_runtime(account):
    cookie_age_minutes = get_cookie_age_minutes(account)
    backoff_seconds = redis_client.ttl(f"cooldown:account:{account.id}")
    cookie_count = 0
    if account.cookie_payload:
        try:
            cookie_count = len(json.loads(account.cookie_payload))
        except Exception:
            cookie_count = 0
    return {
        "cookie_age_minutes": round(cookie_age_minutes, 1) if cookie_age_minutes is not None else None,
        "cookie_hot": can_deliver_cached_cookie(account),
        "cookie_fresh": cookie_age_minutes is not None and cookie_age_minutes < COOKIE_SOFT_REFRESH_MINUTES,
        "minutes_to_refresh": round(max(0, COOKIE_SOFT_REFRESH_MINUTES - cookie_age_minutes), 1) if cookie_age_minutes is not None else None,
        "minutes_to_hard_expiry": round(max(0, COOKIE_HARD_DELIVERY_MINUTES - cookie_age_minutes), 1) if cookie_age_minutes is not None else None,
        "backoff_seconds": backoff_seconds if backoff_seconds and backoff_seconds > 0 else 0,
        "cookie_count": cookie_count
    }

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.headers.get("X-Admin-Token")
        if ADMIN_BREAKGLASS_TOKEN and token == ADMIN_BREAKGLASS_TOKEN:
            g.admin_session = {"username": "breakglass", "display_name": "Breakglass Admin"}
            g.admin_token = token
            return f(*args, **kwargs)
        if not token:
            return jsonify({"erro": "Acesso negado."}), 403
        session_raw = redis_client.get(f"admin:session:{token}")
        if not session_raw:
            return jsonify({"erro": "Acesso negado."}), 403
        g.admin_session = parse_json_safe(session_raw) or {}
        g.admin_token = token
        redis_client.expire(f"admin:session:{token}", ADMIN_SESSION_TTL_SECONDS)
        return f(*args, **kwargs)
    return decorated_function

def get_admin_actor():
    session = getattr(g, "admin_session", {}) or {}
    return session.get("username") or session.get("display_name") or "admin"

def record_admin_audit(action, target=None, extra=None):
    event = {
        "ts": datetime.utcnow().isoformat(),
        "ts_local": get_local_now().strftime("%d/%m/%Y %H:%M:%S"),
        "actor": get_admin_actor(),
        "action": action,
        "target": target,
        "ip": extract_client_ip()
    }
    if extra:
        event.update(extra)
    redis_client.lpush("admin:audit", json.dumps(event, ensure_ascii=False))
    redis_client.ltrim("admin:audit", 0, 199)

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    if not username or not password:
        return jsonify({"erro": "Usuário e senha são obrigatórios."}), 400

    auth = autenticar_admin_ad(username, password)
    if auth.get('status') != 'sucesso':
        return jsonify({"erro": auth.get('mensagem', 'Acesso negado.')}), 403

    token = secrets.token_urlsafe(32)
    session_payload = {
        "username": auth.get("usuario", username),
        "display_name": auth.get("display_name", username),
        "email": auth.get("email", ""),
        "grupo_admin": auth.get("grupo_admin", ""),
        "created_at": datetime.utcnow().isoformat()
    }
    redis_client.setex(f"admin:session:{token}", ADMIN_SESSION_TTL_SECONDS, json.dumps(session_payload))
    redis_client.lpush("admin:audit", json.dumps({
        "ts": datetime.utcnow().isoformat(),
        "ts_local": get_local_now().strftime("%d/%m/%Y %H:%M:%S"),
        "actor": session_payload["username"],
        "action": "admin_login",
        "target": "panel",
        "ip": extract_client_ip()
    }, ensure_ascii=False))
    redis_client.ltrim("admin:audit", 0, 199)
    return jsonify({
        "token": token,
        "user": session_payload,
        "ttl_seconds": ADMIN_SESSION_TTL_SECONDS
    })

@app.route('/api/admin/logout', methods=['POST'])
@admin_required
def admin_logout():
    token = getattr(g, "admin_token", None)
    if token and not (ADMIN_BREAKGLASS_TOKEN and token == ADMIN_BREAKGLASS_TOKEN):
        redis_client.delete(f"admin:session:{token}")
    record_admin_audit("admin_logout", target="panel")
    return jsonify({"ok": True})

def inicializar_sistema():
    tentativas = 10
    while tentativas > 0:
        try:
            init_db()
            seed_db()
            print("✅ Banco de Dados conectado e migrado!")
            return True
        except Exception as e:
            print(f"⚠️ Aguardando banco... {e}")
            tentativas -= 1
            time.sleep(5)
    return None

def buscar_conta_para_setor(db, setor_nome):
    account = db.query(AccountBB).filter(
        AccountBB.status.in_(['active', 'ativo', 'provisoria_recebida', 'termo_assinado']),
        AccountBB.setores.like(f"%|{setor_nome}|%")
    ).order_by(AccountBB.id.asc()).first()
    if account: return account
    
    sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
    if sector:
        return db.query(AccountBB).filter(
            AccountBB.sector_id == sector.id, 
            AccountBB.status.in_(['active', 'ativo', 'provisoria_recebida', 'termo_assinado'])
        ).order_by(AccountBB.id.asc()).first()
    return None

# --- ROTAS DE PÁGINAS WEB ---
@app.route('/admin')
@app.route('/admin.html')
def serve_admin():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(base_dir, 'admin.html')

@app.route('/privacidade')
@app.route('/privacy')
def serve_privacy():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(base_dir, 'privacy.html')

@app.route('/shared/<path:filename>')
def serve_shared(filename):
    return send_from_directory(SHARED_DIR, filename)

# --- ROTAS DE OPERAÇÃO (EXTENSÃO) ---
@app.route('/api/zerocore/status', methods=['GET'])
def get_status():
    setor_nome = request.args.get('setor')
    if not setor_nome: return jsonify({"mensagem": "Setor ausente."}), 400

    db = SessionLocal()
    try:
        account = buscar_conta_para_setor(db, setor_nome)
        if can_deliver_cached_cookie(account):
            cookie_age = get_cookie_age_minutes(account)
            hard_delivery_minutes = get_cookie_hard_delivery_limit_minutes(account=account)
            outcome = "pool_hot" if cookie_age is not None and cookie_age < COOKIE_SOFT_REFRESH_MINUTES else "pool_warm"
            record_request_event(
                "status",
                setor_nome=setor_nome,
                account=account,
                outcome=outcome,
                cookie_age_minutes=cookie_age,
                extra={"hard_delivery_minutes": round(hard_delivery_minutes, 1)},
            )
            return jsonify({"concluido": True, "mensagem": "Conexão segura estabelecida!"})
    finally:
        db.close()

    status_str = redis_client.get(f"status:{setor_nome}")
    record_request_event("status", setor_nome=setor_nome, outcome="awaiting_sync")
    return jsonify(json.loads(status_str)) if status_str else jsonify({"mensagem": "Aguardando sincronização..."})

@app.route('/api/zerocore/login', methods=['POST'])
def request_login():
    data = request.get_json(silent=True) or {}
    username, password = data.get('username'), data.get('password')
    user_agent = data.get('user_agent')
    force_fresh = bool(data.get('force_fresh'))
    
    if not username or not password:
        record_request_event("login", outcome="missing_credentials", user_agent=user_agent)
        return jsonify({"status": "erro", "mensagem": "Usuário e senha são obrigatórios."}), 400

    ad_result = autenticar_e_obter_setor(username, password)
    if ad_result['status'] == 'erro':
        record_request_event("login", outcome="ad_denied", user_agent=user_agent)
        return jsonify(ad_result), 401

    setor_nome = ad_result['setor']
    requester = {
        "username": ad_result.get("usuario") or username,
        "display_name": ad_result.get("display_name") or ad_result.get("usuario") or username
    }
    request_id = secrets.token_hex(8)
    
    # 📊 Registo de métricas com data (Histórico Diário)
    hoje = datetime.utcnow().strftime('%Y-%m-%d')
    redis_client.incr(f'metrics:logins_solicitados:{hoje}')
    redis_client.hincrby(f'metrics:sector_logins:{hoje}', setor_nome, 1)
    
    db = SessionLocal()
    try:
        if not db.query(Sector).filter(Sector.nome == setor_nome).first():
            db.add(Sector(nome=setor_nome))
            db.commit()
            
        account = buscar_conta_para_setor(db, setor_nome)
        
        if not account:
            record_request_event("login", setor_nome=setor_nome, outcome="no_account", user_agent=user_agent, requester=requester, request_id=request_id)
            return jsonify({"status": "erro", "mensagem": f"Setor {setor_nome} sem conta válida/ativa vinculada."}), 403

        recent_cache_age_seconds = get_recent_cache_delivery_age_seconds(account=account, requester=requester)
        settling_duplicate_click = recent_cache_age_seconds is not None and recent_cache_age_seconds <= CACHE_SETTLE_GRACE_SECONDS
        requires_fresh_reentry = (
            recent_cache_age_seconds is not None
            and recent_cache_age_seconds > CACHE_SETTLE_GRACE_SECONDS
            and recent_cache_age_seconds <= CACHE_REENTRY_FORCE_FRESH_SECONDS
        )

        if can_deliver_cached_cookie(account, login_flow=True) and not force_fresh and not requires_fresh_reentry:
            cookie_age = get_cookie_age_minutes(account)
            hard_delivery_minutes = get_cookie_hard_delivery_limit_minutes(account=account, login_flow=True)
            background_refresh_started = False
            if should_background_refresh_cookie(account):
                background_refresh_started = enqueue_login_refresh(
                    account,
                    setor_nome,
                    user_agent=user_agent,
                    request_id=request_id,
                    requester=requester,
                    auto=True,
                    force_fresh=False,
                )
            redis_client.incr(f'metrics:cookies_injetados:{hoje}')
            redis_client.hincrby(f'metrics:account_logins:{hoje}', str(account.login), 1)
            record_request_event(
                "login",
                setor_nome=setor_nome,
                account=account,
                outcome="pool_hit",
                user_agent=user_agent,
                cookie_age_minutes=cookie_age,
                requester=requester,
                request_id=request_id,
                extra={
                    "refresh_due": bool(cookie_age is not None and cookie_age >= COOKIE_SOFT_REFRESH_MINUTES),
                    "background_refresh_started": background_refresh_started,
                    "hard_delivery_minutes": round(hard_delivery_minutes, 1),
                    "force_fresh": force_fresh,
                    "recent_cache_age_seconds": recent_cache_age_seconds,
                    "settling_duplicate_click": settling_duplicate_click,
                },
            )
            mark_recent_cache_delivery(account=account, requester=requester)
            return jsonify({
                "status": "sucesso", "setor": setor_nome,
                "cookies": json.loads(account.cookie_payload),
                "url": "https://juridico.bb.com.br/wfj"
            })
        
        # =========================================================
        # 🔒 A TRAVA DE FILA FOI RESTAURADA AQUI
        # =========================================================
        lock_key = f"lock:queue:{account.id}"
        if redis_client.exists(lock_key):
            redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": "Sincronizando com conexão em andamento...", "concluido": False}))
            record_request_event("login", setor_nome=setor_nome, account=account, outcome="already_queued", user_agent=user_agent, requester=requester, request_id=request_id)
            return jsonify({"status": "queued", "setor": setor_nome}) 
            
        queued_now = enqueue_login_refresh(
            account,
            setor_nome,
            user_agent=user_agent,
            request_id=request_id,
            requester=requester,
            status_message="Solicitando login novo no portal..." if (force_fresh or requires_fresh_reentry) else "Iniciando robô...",
            force_fresh=True,
        )
        if not queued_now:
            record_request_event("login", setor_nome=setor_nome, account=account, outcome="already_queued", user_agent=user_agent, requester=requester, request_id=request_id)
            return jsonify({"status": "queued", "setor": setor_nome})
        
        redis_client.incr(f'metrics:robos_executados:{hoje}')
        redis_client.hincrby(f'metrics:account_logins:{hoje}', str(account.login), 1)
        record_request_event(
            "login",
            setor_nome=setor_nome,
            account=account,
            outcome="queued",
            user_agent=user_agent,
            requester=requester,
            request_id=request_id,
            extra={
                "force_fresh": force_fresh,
                "recent_cache_age_seconds": recent_cache_age_seconds,
                "settling_duplicate_click": settling_duplicate_click,
                "requires_fresh_reentry": requires_fresh_reentry,
            },
        )
        
        return jsonify({"status": "queued", "setor": setor_nome})
    except Exception as e:
        record_request_event("login", setor_nome=setor_nome, outcome="api_error", user_agent=user_agent, extra={"error": str(e)}, requester=requester, request_id=request_id)
        return jsonify({"status": "erro", "mensagem": f"Erro interno na API: {str(e)}"}), 500
    finally:
        db.close()

@app.route('/api/zerocore/renew', methods=['POST'])
def renew_session():
    data = request.get_json(silent=True) or {}
    username, password = data.get('username'), data.get('password')
    setor_nome = data.get('setor') or request.args.get('setor')
    user_agent = data.get('user_agent')
    force_fresh = bool(data.get('force_fresh'))
    requester = None
    request_id = secrets.token_hex(8)

    if not setor_nome: return jsonify({"status": "erro", "mensagem": "Setor ausente."}), 400

    if username and password:
        ad_result = autenticar_e_obter_setor(username, password)
        if ad_result['status'] == 'erro':
            record_request_event("renew", setor_nome=setor_nome, outcome="ad_denied", user_agent=user_agent)
            return jsonify({"status": "unauthorized", "mensagem": "Credenciais inválidas."}), 401
        requester = {
            "username": ad_result.get("usuario") or username,
            "display_name": ad_result.get("display_name") or ad_result.get("usuario") or username
        }

    db = SessionLocal()
    hoje = datetime.utcnow().strftime('%Y-%m-%d')
    try:
        account = buscar_conta_para_setor(db, setor_nome)
        if account:
            redis_client.hincrby(f'metrics:sector_logins:{hoje}', setor_nome, 1)
            
            if can_deliver_cached_cookie(account) and not force_fresh:
                cookie_age = get_cookie_age_minutes(account)
                hard_delivery_minutes = get_cookie_hard_delivery_limit_minutes(account=account)
                refresh_due = should_background_refresh_cookie(account)
                background_refresh_started = False
                status_message = "Sessão quente retornada do Pool."
                if refresh_due:
                    background_refresh_started = enqueue_login_refresh(
                        account,
                        setor_nome,
                        user_agent=user_agent,
                        request_id=request_id,
                        requester=requester,
                        status_message="Sessão reaproveitada do Pool. Renovação discreta em andamento.",
                        auto=True,
                        force_fresh=False,
                    )
                    if background_refresh_started:
                        status_message = "Sessão reaproveitada do Pool. Renovação discreta em andamento."
                    elif redis_client.exists(f"lock:queue:{account.id}"):
                        status_message = "Sessão reaproveitada do Pool. Já existe uma renovação em andamento."

                redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": status_message, "concluido": True, "erro": False}))
                redis_client.hincrby(f'metrics:account_logins:{hoje}', str(account.login), 1)
                record_request_event(
                    "renew",
                    setor_nome=setor_nome,
                    account=account,
                    outcome="pool_hit",
                    user_agent=user_agent,
                    cookie_age_minutes=cookie_age,
                    requester=requester,
                    request_id=request_id,
                    extra={
                        "refresh_due": refresh_due,
                        "background_refresh_started": background_refresh_started,
                        "hard_delivery_minutes": round(hard_delivery_minutes, 1),
                        "force_fresh": force_fresh,
                    },
                )
                return jsonify({"status": "queued"})
            
            # =========================================================
            # 🔒 A TRAVA DE FILA FOI RESTAURADA AQUI
            # =========================================================
            lock_key = f"lock:queue:{account.id}"
            if redis_client.exists(lock_key):
                redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": "Aguardando renovação de segurança...", "concluido": False, "erro": False}))
                record_request_event("renew", setor_nome=setor_nome, account=account, outcome="already_queued", user_agent=user_agent, requester=requester, request_id=request_id)
                return jsonify({"status": "queued"})

            queued_now = enqueue_login_refresh(
                account,
                setor_nome,
                user_agent=user_agent,
                request_id=request_id,
                requester=requester,
                status_message="Solicitando login novo após saída do portal..." if force_fresh else "Renovação de Marcapasso...",
                force_fresh=force_fresh,
            )
            if not queued_now:
                record_request_event("renew", setor_nome=setor_nome, account=account, outcome="already_queued", user_agent=user_agent, requester=requester, request_id=request_id)
                return jsonify({"status": "queued"})
            
            redis_client.incr(f'metrics:robos_executados:{hoje}')
            redis_client.hincrby(f'metrics:account_logins:{hoje}', str(account.login), 1)
            record_request_event(
                "renew",
                setor_nome=setor_nome,
                account=account,
                outcome="queued",
                user_agent=user_agent,
                requester=requester,
                request_id=request_id,
                extra={"force_fresh": force_fresh},
            )
            return jsonify({"status": "queued"})
    finally:
        db.close()
    record_request_event("renew", setor_nome=setor_nome, outcome="not_found", user_agent=user_agent, requester=requester, request_id=request_id)
    return jsonify({"status": "erro"}), 404

@app.route('/api/zerocore/session', methods=['GET', 'POST'])
def get_session():
    data = request.get_json(silent=True) or {}
    username, password = data.get('username'), data.get('password')
    setor_nome = data.get('setor') or request.args.get('setor')
    requester = None
    request_id = secrets.token_hex(8)

    if not setor_nome: return jsonify({"status": "erro", "mensagem": "Setor ausente."}), 400

    if username and password:
        ad_result = autenticar_e_obter_setor(username, password)
        if ad_result['status'] == 'erro':
            record_request_event("session", setor_nome=setor_nome, outcome="ad_denied")
            return jsonify({"status": "unauthorized", "mensagem": "Credenciais AD inválidas."}), 401
        requester = {
            "username": ad_result.get("usuario") or username,
            "display_name": ad_result.get("display_name") or ad_result.get("usuario") or username
        }
    
    db = SessionLocal()
    try:
        account = buscar_conta_para_setor(db, setor_nome)
        if can_deliver_cached_cookie(account):
            hard_delivery_minutes = get_cookie_hard_delivery_limit_minutes(account=account)
            record_request_event(
                "session",
                setor_nome=setor_nome,
                account=account,
                outcome="cookie_returned",
                cookie_age_minutes=get_cookie_age_minutes(account),
                requester=requester,
                request_id=request_id,
                extra={"hard_delivery_minutes": round(hard_delivery_minutes, 1)},
            )
            return jsonify({"status": "sucesso", "cookies": json.loads(account.cookie_payload)})
        if account and account.cookie_payload:
            hard_delivery_minutes = get_cookie_hard_delivery_limit_minutes(account=account)
            record_request_event(
                "session",
                setor_nome=setor_nome,
                account=account,
                outcome="stale_unavailable",
                cookie_age_minutes=get_cookie_age_minutes(account),
                requester=requester,
                request_id=request_id,
                extra={"hard_delivery_minutes": round(hard_delivery_minutes, 1)},
            )
    finally:
        db.close()
    record_request_event("session", setor_nome=setor_nome, outcome="not_found", requester=requester, request_id=request_id)
    return jsonify({"status": "erro"}), 404

# --- ROTAS ADMINISTRATIVAS E DASHBOARD ---
@app.route('/api/admin/ad_sectors', methods=['GET'])
@admin_required
def admin_list_ad_sectors():
    ad_ous = listar_ous_bb_ad()
    db = SessionLocal()
    try:
        db_sectors = [s.nome for s in db.query(Sector).all()]
    finally:
        db.close()
    todos_setores = sorted(list(set(ad_ous + db_sectors)))
    return jsonify(todos_setores)

@app.route('/api/admin/dashboard_stats', methods=['GET'])
@admin_required
def admin_dashboard_stats():
    db = SessionLocal()
    hoje = datetime.utcnow().strftime('%Y-%m-%d')
    try:
        live_requesters = build_live_requesters()
        live_sectors = get_live_entries("live:setor:*")
        total_accounts = db.query(AccountBB).count()
        active_accounts = db.query(AccountBB).filter(AccountBB.status.in_(['active', 'ativo', 'provisoria_recebida', 'termo_assinado'])).count()
        queue_size = redis_client.llen("queue:login_requests") + redis_client.llen("queue:priority_logins")
        
        logins_solicitados = int(redis_client.get(f'metrics:logins_solicitados:{hoje}') or 0)
        cookies_injetados = int(redis_client.get(f'metrics:cookies_injetados:{hoje}') or 0)
        robos_executados = int(redis_client.get(f'metrics:robos_executados:{hoje}') or 0)
        economia_pct = round((cookies_injetados / logins_solicitados) * 100, 1) if logins_solicitados > 0 else 0
        cookie_age_sum = float(redis_client.get(f'metrics:cookie_age_sum:{hoje}') or 0)
        cookie_age_count = int(redis_client.get(f'metrics:cookie_age_count:{hoje}') or 0)
        avg_cookie_age = round(cookie_age_sum / cookie_age_count, 1) if cookie_age_count > 0 else 0
        cycle_sum = float(redis_client.get(f'metrics:session_cycle_sum:{hoje}') or 0)
        cycle_count = int(redis_client.get(f'metrics:session_cycle_count:{hoje}') or 0)
        avg_session_cycle = round(cycle_sum / cycle_count, 1) if cycle_count > 0 else 0
        hot_pool_accounts = 0
        cookie_accounts = db.query(AccountBB).filter(AccountBB.last_login_at.isnot(None), AccountBB.cookie_payload.isnot(None)).all()
        for acc in cookie_accounts:
            if can_deliver_cached_cookie(acc):
                hot_pool_accounts += 1
        session_totals = get_session_totals(cookie_accounts)

        return jsonify({
            "active_accounts": active_accounts,
            "total_accounts": total_accounts,
            "queue_size": queue_size,
            "logins_solicitados": logins_solicitados,
            "cookies_injetados": cookies_injetados,
            "robos_executados": robos_executados,
            "economia_pct": economia_pct,
            "avg_cookie_age": avg_cookie_age,
            "avg_session_cycle": avg_session_cycle,
            "hot_pool_accounts": hot_pool_accounts,
            "active_users_now": len(live_requesters),
            "active_sectors_now": len(live_sectors),
            "workers_busy": len([w for w in get_worker_states() if w.get("state") not in ("idle", "starting")]),
            "workers_total": len(get_worker_states()),
            "queue_snapshot": get_queue_snapshot(),
            "session_totals": session_totals
        })
    finally:
        db.close()

@app.route('/api/admin/live_overview', methods=['GET'])
@admin_required
def admin_live_overview():
    db = SessionLocal()
    try:
        accounts = db.query(AccountBB).all()
        session_totals = get_session_totals(accounts)
        hot_sessions = []
        backoff_accounts = []
        for acc in accounts:
            runtime = get_account_runtime(acc)
            if runtime["cookie_hot"]:
                hot_sessions.append({
                    "account_id": acc.id,
                    "login": acc.login,
                    "titular": acc.titular or "",
                    "setores": [s for s in (acc.setores or "").split("|") if s],
                    "cookie_age_minutes": runtime["cookie_age_minutes"],
                    "minutes_to_refresh": runtime["minutes_to_refresh"],
                    "minutes_to_hard_expiry": runtime["minutes_to_hard_expiry"],
                    "cookie_fresh": runtime["cookie_fresh"],
                    "cookie_count": runtime["cookie_count"],
                    "last_login_at": acc.last_login_at.isoformat() if acc.last_login_at else None
                })
            if runtime["backoff_seconds"] > 0:
                backoff_accounts.append({
                    "account_id": acc.id,
                    "login": acc.login,
                    "titular": acc.titular or "",
                    "setores": [s for s in (acc.setores or "").split("|") if s],
                    "backoff_seconds": runtime["backoff_seconds"]
                })

        hot_sessions.sort(key=lambda x: x["minutes_to_refresh"])
        backoff_accounts.sort(key=lambda x: x["backoff_seconds"], reverse=True)

        return jsonify({
            "generated_at": get_local_now().strftime("%d/%m/%Y %H:%M:%S"),
            "cookie_reuse_minutes": COOKIE_SOFT_REFRESH_MINUTES,
            "cookie_hard_delivery_minutes": COOKIE_HARD_DELIVERY_MINUTES,
            "queue": get_queue_snapshot(),
            "workers": get_worker_states(),
            "active_requesters": build_live_requesters(),
            "active_users": build_live_requesters(),
            "active_sectors": get_live_entries("live:setor:*"),
            "active_accounts": get_live_entries("live:account:*"),
            "hot_sessions": hot_sessions[:20],
            "backoff_accounts": backoff_accounts[:20],
            "session_totals": session_totals
        })
    finally:
        db.close()

@app.route('/api/admin/recent_requests', methods=['GET'])
@admin_required
def admin_recent_requests():
    limit = min(int(request.args.get('limit', 80)), 200)
    items = []
    for raw in redis_client.lrange("admin:recent_requests", 0, limit - 1):
        data = parse_json_safe(raw)
        if data:
            items.append(data)
    return jsonify(items)

@app.route('/api/admin/sync_pressure', methods=['GET'])
@admin_required
def admin_sync_pressure():
    window_minutes = min(max(int(request.args.get('minutes', 30)), 5), 240)
    return jsonify(build_sync_pressure_snapshot(window_minutes=window_minutes))

@app.route('/api/admin/logs/live', methods=['GET'])
@admin_required
def admin_logs_live():
    source = request.args.get('source', 'combined')
    day = get_requested_day(request.args.get('day'))
    if day is None:
        return jsonify({"erro": "Data inválida. Use YYYY-MM-DD"}), 400
    limit = min(max(int(request.args.get('limit', 120)), 20), LOG_TAIL_LIMIT)
    contains = (request.args.get('contains') or '').strip()
    return jsonify({
        "generated_at": get_local_now().strftime("%d/%m/%Y %H:%M:%S"),
        "source": source,
        "day": day,
        "limit": limit,
        "contains": contains,
        "items": read_log_tail(source=source, day=day, limit=limit, contains=contains)
    })

@app.route('/api/admin/logs/download', methods=['GET'])
@admin_required
def admin_logs_download():
    source = request.args.get('source', 'combined')
    day = get_requested_day(request.args.get('day'))
    if day is None:
        return jsonify({"erro": "Data inválida. Use YYYY-MM-DD"}), 400
    contains = (request.args.get('contains') or '').strip()
    filename = f"onelog_{source}_{day}.log"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    return Response(stream_log_download(source=source, day=day, contains=contains), mimetype='text/plain; charset=utf-8', headers=headers)

@app.route('/api/admin/error_images', methods=['GET'])
@admin_required
def admin_error_images():
    if not os.path.exists(SHARED_DIR):
        return jsonify([])

    images = []
    for name in os.listdir(SHARED_DIR):
        if not name.lower().endswith('.png'):
            continue
        lower_name = name.lower()
        if 'erro' not in lower_name and 'error' not in lower_name:
            continue
        path = os.path.join(SHARED_DIR, name)
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            continue
        images.append({
            "name": name,
            "url": f"/shared/{name}",
            "date_str": mtime.strftime("%d/%m/%Y %H:%M"),
            "ts": mtime.isoformat()
        })
    images.sort(key=lambda x: x["ts"], reverse=True)
    return jsonify(images[:60])

@app.route('/api/admin/analytics', methods=['GET'])
@admin_required
def admin_analytics():
    # Agora a rota capta também a saúde dos robôs e os motivos das falhas de infraestrutura.
    start_str = request.args.get('start', datetime.utcnow().strftime('%Y-%m-%d'))
    end_str = request.args.get('end', datetime.utcnow().strftime('%Y-%m-%d'))
    
    try:
        start_date = datetime.strptime(start_str, '%Y-%m-%d')
        end_date = datetime.strptime(end_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD"}), 400

    total_logins = 0
    total_cookies = 0
    total_cookie_age_sum = 0.0
    total_cookie_age_count = 0
    total_session_cycle_sum = 0.0
    total_session_cycle_count = 0
    max_session_cycle = 0.0
    
    sector_stats = {}
    account_stats = {}
    robot_success_stats = {}
    robot_error_stats = {}
    error_reasons_stats = {}
    request_endpoint_stats = {}
    request_outcome_stats = {}
    
    current_date = start_date
    while current_date <= end_date:
        day_str = current_date.strftime('%Y-%m-%d')
        
        total_logins += int(redis_client.get(f'metrics:logins_solicitados:{day_str}') or 0)
        total_cookies += int(redis_client.get(f'metrics:cookies_injetados:{day_str}') or 0)
        total_cookie_age_sum += float(redis_client.get(f'metrics:cookie_age_sum:{day_str}') or 0)
        total_cookie_age_count += int(redis_client.get(f'metrics:cookie_age_count:{day_str}') or 0)
        total_session_cycle_sum += float(redis_client.get(f'metrics:session_cycle_sum:{day_str}') or 0)
        total_session_cycle_count += int(redis_client.get(f'metrics:session_cycle_count:{day_str}') or 0)
        max_session_cycle = max(max_session_cycle, float(redis_client.get(f'metrics:session_cycle_max:{day_str}') or 0))
        
        for sec, count in redis_client.hgetall(f'metrics:sector_logins:{day_str}').items():
            sector_stats[sec] = sector_stats.get(sec, 0) + int(count)
            
        for acc, count in redis_client.hgetall(f'metrics:account_logins:{day_str}').items():
            account_stats[acc] = account_stats.get(acc, 0) + int(count)
            
        # Telemetria dos Robôs
        for robo, count in redis_client.hgetall(f'metrics:robot_success:{day_str}').items():
            robot_success_stats[robo] = robot_success_stats.get(robo, 0) + int(count)
            
        for robo, count in redis_client.hgetall(f'metrics:robot_error:{day_str}').items():
            robot_error_stats[robo] = robot_error_stats.get(robo, 0) + int(count)
            
        for reason, count in redis_client.hgetall(f'metrics:error_reasons:{day_str}').items():
            error_reasons_stats[reason] = error_reasons_stats.get(reason, 0) + int(count)

        for endpoint, count in redis_client.hgetall(f'metrics:request_endpoint:{day_str}').items():
            request_endpoint_stats[endpoint] = request_endpoint_stats.get(endpoint, 0) + int(count)

        for outcome, count in redis_client.hgetall(f'metrics:request_outcome:{day_str}').items():
            request_outcome_stats[outcome] = request_outcome_stats.get(outcome, 0) + int(count)
            
        current_date += timedelta(days=1)

    sorted_sectors = [{"name": k, "count": v} for k, v in sorted(sector_stats.items(), key=lambda x: x[1], reverse=True)]
    sorted_accounts = [{"name": k, "count": v} for k, v in sorted(account_stats.items(), key=lambda x: x[1], reverse=True)]
    
    # Prepara o Array de Robôs mesclando os sucessos e erros
    all_robots = set(list(robot_success_stats.keys()) + list(robot_error_stats.keys()))
    robots_data = []
    for robo in all_robots:
        s = robot_success_stats.get(robo, 0)
        e = robot_error_stats.get(robo, 0)
        robots_data.append({
            "name": robo, "success": s, "error": e, 
            "success_rate": round((s / (s + e)) * 100, 1) if (s + e) > 0 else 0
        })
    robots_data.sort(key=lambda x: x['name'])
    
    sorted_reasons = [{"reason": k, "count": v} for k, v in sorted(error_reasons_stats.items(), key=lambda x: x[1], reverse=True)]
    sorted_endpoints = [{"name": k, "count": v} for k, v in sorted(request_endpoint_stats.items(), key=lambda x: x[1], reverse=True)]
    sorted_outcomes = [{"name": k, "count": v} for k, v in sorted(request_outcome_stats.items(), key=lambda x: x[1], reverse=True)]
    economia_pct = round((total_cookies / total_logins) * 100, 1) if total_logins > 0 else 0
    avg_cookie_age = round(total_cookie_age_sum / total_cookie_age_count, 1) if total_cookie_age_count > 0 else 0
    avg_session_cycle = round(total_session_cycle_sum / total_session_cycle_count, 1) if total_session_cycle_count > 0 else 0

    return jsonify({
        "period": f"{start_str} a {end_str}",
        "efficiency": {
            "total_logins_requested": total_logins,
            "total_cookies_injected": total_cookies,
            "economia_pct": economia_pct,
            "avg_cookie_age_minutes": avg_cookie_age,
            "avg_session_cycle_minutes": avg_session_cycle,
            "max_session_cycle_minutes": round(max_session_cycle, 1)
        },
        "sectors": sorted_sectors,
        "accounts": sorted_accounts,
        "robots_performance": robots_data,
        "error_diagnostics": sorted_reasons,
        "request_endpoints": sorted_endpoints,
        "request_outcomes": sorted_outcomes
    })

@app.route('/api/admin/accounts', methods=['GET', 'POST'])
@admin_required
def gerenciar_contas():
    db = SessionLocal()
    try:
        if request.method == 'GET':
            accounts = db.query(AccountBB).all()
            result = []
            for acc in accounts:
                lista_setores = [s for s in (acc.setores or "").split("|") if s]
                if not lista_setores and acc.sector:
                    lista_setores = [acc.sector.nome]
                runtime = get_account_runtime(acc)

                result.append({
                    "id": acc.id,
                    "login": acc.login,
                    "titular": acc.titular or "Não informado",
                    "setores": lista_setores,
                    "status": acc.status,
                    "data_validade": acc.data_validade,
                    "status_updated_at": acc.status_updated_at.isoformat() if acc.status_updated_at else None,
                    "last_login": acc.last_login_at.strftime("%d/%m/%Y %H:%M") if acc.last_login_at else "Nunca conectou",
                    "last_login_at": acc.last_login_at.isoformat() if acc.last_login_at else None,
                    "user_agent_used": trim_user_agent(acc.user_agent_used or "", 90),
                    "cookie_hot": runtime["cookie_hot"],
                    "cookie_age_minutes": runtime["cookie_age_minutes"],
                    "minutes_to_refresh": runtime["minutes_to_refresh"],
                    "backoff_seconds": runtime["backoff_seconds"],
                    "cookie_count": runtime["cookie_count"]
                })
            return jsonify(result)
            
        elif request.method == 'POST':
            data = request.get_json() or {}
            if not data.get('login') or not data.get('senha'):
                return jsonify({"erro": "Login e Senha são obrigatórios"}), 400
                
            account = db.query(AccountBB).filter(AccountBB.login == data['login']).first()
            if not account:
                account = AccountBB(login=data['login'])
                db.add(account)
                
            account.senha = data['senha']
            account.titular = data.get('titular', '')
            account.status = data.get('status', 'cadastro_inicial')
            account.data_validade = data.get('data_validade', '')
            
            if data.get('status_updated_at'):
                try: account.status_updated_at = datetime.strptime(data['status_updated_at'], "%Y-%m-%d")
                except ValueError: account.status_updated_at = datetime.utcnow()
            else: account.status_updated_at = datetime.utcnow() 
            
            setores_lista = data.get('setores', [])
            account.setores = "|" + "|".join(setores_lista) + "|" if setores_lista else ""
            
            db.commit()
            record_admin_audit("account_create", target=account.login, extra={"account_id": account.id})
            return jsonify({"mensagem": "Conta criada com sucesso!"})
    except Exception as e:
        db.rollback()
        return jsonify({"erro": str(e)}), 500
    finally:
        db.close()

@app.route('/api/admin/accounts/<int:account_id>', methods=['PUT', 'DELETE'])
@admin_required
def editar_conta(account_id):
    db = SessionLocal()
    try:
        acc = db.query(AccountBB).filter(AccountBB.id == account_id).first()
        if not acc: return jsonify({"erro": "Conta não encontrada."}), 404
        
        if request.method == 'DELETE':
            login_ref = acc.login
            db.delete(acc)
            db.commit()
            record_admin_audit("account_delete", target=login_ref, extra={"account_id": account_id})
            return jsonify({"mensagem": "Conta excluída."})
            
        elif request.method == 'PUT':
            data = request.get_json() or {}
            
            if data.get('senha'): acc.senha = data['senha']
            if 'titular' in data: acc.titular = data['titular']
            if 'data_validade' in data: acc.data_validade = data['data_validade']
            
            mudou_status = 'status' in data and acc.status != data['status']
            if 'status' in data: acc.status = data['status']
            
            if mudou_status:
                if data.get('status_updated_at'):
                    try: acc.status_updated_at = datetime.strptime(data['status_updated_at'], "%Y-%m-%d")
                    except ValueError: acc.status_updated_at = datetime.utcnow()
                else: acc.status_updated_at = datetime.utcnow()
            else:
                if data.get('status_updated_at'):
                    try:
                        nova_data = datetime.strptime(data['status_updated_at'], "%Y-%m-%d")
                        acc.status_updated_at = nova_data
                    except ValueError: pass
                
            if 'setores' in data:
                setores_lista = data['setores']
                acc.setores = "|" + "|".join(setores_lista) + "|" if setores_lista else ""
            
            db.commit()
            record_admin_audit("account_update", target=acc.login, extra={"account_id": acc.id})
            return jsonify({"mensagem": "Conta atualizada com sucesso!"})
    except Exception as e:
        db.rollback()
        return jsonify({"erro": str(e)}), 500
    finally:
        db.close()

@app.route('/api/admin/accounts/<int:account_id>/clear', methods=['POST'])
@admin_required
def clear_account_cookies(account_id):
    db = SessionLocal()
    try:
        acc = db.query(AccountBB).filter(AccountBB.id == account_id).first()
        if not acc: return jsonify({"erro": "Conta não encontrada."}), 404
        
        acc.cookie_payload = None
        acc.last_login_at = None
        
        db.commit()
        record_admin_audit("account_clear_cookies", target=acc.login, extra={"account_id": acc.id})
        return jsonify({"mensagem": "Sessão purgada com sucesso!"})
    except Exception as e:
        db.rollback()
        return jsonify({"erro": str(e)}), 500
    finally:
        db.close()

@app.route('/api/admin/accounts/<int:account_id>/secret', methods=['GET'])
@admin_required
def get_account_secret(account_id):
    db = SessionLocal()
    try:
        acc = db.query(AccountBB).filter(AccountBB.id == account_id).first()
        if not acc:
            return jsonify({"erro": "Conta não encontrada."}), 404
        record_admin_audit("account_reveal_secret", target=acc.login, extra={"account_id": acc.id})
        return jsonify({"login": acc.login, "senha": acc.senha})
    finally:
        db.close()

@app.route('/api/admin/audit', methods=['GET'])
@admin_required
def admin_audit():
    limit = min(int(request.args.get('limit', 40)), 200)
    items = []
    for raw in redis_client.lrange("admin:audit", 0, limit - 1):
        data = parse_json_safe(raw)
        if data:
            items.append(data)
    return jsonify(items)

@app.route('/api/zerocore/reset', methods=['POST'])
def api_reset():
    redis_client.delete(f"status:{request.args.get('setor', 'GERAL')}")
    return jsonify({"status": "resetado"})

if __name__ == '__main__':
    if not os.path.exists('static'): os.makedirs('static')
    if inicializar_sistema():
        app.run(host='0.0.0.0', port=5000)

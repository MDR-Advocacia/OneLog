import redis
import json
import os
import time
import multiprocessing
import signal
import psutil
import random
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from seleniumbase import SB
import logging
import sys
from database import SessionLocal, AccountBB

if not os.path.exists('shared'): os.makedirs('shared')

# Configuração de Logs (Salva no terminal e no arquivo público)
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - WORKER - %(message)s',
    handlers=[
        logging.FileHandler("shared/worker_debug.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

BASE_URL = "https://api-onelog.mdradvocacia.com"

# MODO TURBO CONTROLADO POR VARIÁVEL DE AMBIENTE (Padrão: True)
DEBUG_MODE = os.getenv("DEBUG_MODE", "True").lower() == "true"

# Define quantos robôs vão rodar ao mesmo tempo (Padrão: 3)
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "3"))
LOCAL_TIMEZONE = os.getenv("APP_TIMEZONE", "America/Fortaleza")
AUTO_DISPATCH_START_HOUR = int(os.getenv("AUTO_DISPATCH_START_HOUR", "7"))
AUTO_DISPATCH_END_HOUR = int(os.getenv("AUTO_DISPATCH_END_HOUR", "19"))
AUTO_DISPATCH_WEEKDAYS = {
    int(day.strip())
    for day in os.getenv("AUTO_DISPATCH_WEEKDAYS", "0,1,2,3,4").split(",")
    if day.strip()
}
AUTO_DISPATCH_RESPECT_WINDOW = os.getenv("AUTO_DISPATCH_RESPECT_WINDOW", "true").lower() == "true"
WATCHDOG_STALE_SECONDS = int(os.getenv("WATCHDOG_STALE_SECONDS", "420"))
INFRA_COOLDOWN_SECONDS = int(os.getenv("INFRA_COOLDOWN_SECONDS", "600"))
ACCOUNT_RETRY_BACKOFF_SECONDS = int(os.getenv("ACCOUNT_RETRY_BACKOFF_SECONDS", "900"))
ACCOUNT_INFRA_BACKOFF_SECONDS = int(os.getenv("ACCOUNT_INFRA_BACKOFF_SECONDS", "1800"))
CHROME_STARTUP_LOCK_TTL = int(os.getenv("CHROME_STARTUP_LOCK_TTL", "120"))
HEARTBEAT_TTL_SECONDS = max(WATCHDOG_STALE_SECONDS * 2, 900)
PRE_DISPATCH_PURGE_LEAD_MINUTES = int(os.getenv("PRE_DISPATCH_PURGE_LEAD_MINUTES", "15"))
PURGE_INFRA_COOLDOWN_SECONDS = int(os.getenv("PURGE_INFRA_COOLDOWN_SECONDS", "90"))
IDLE_PURGE_MIN_IDLE_SECONDS = int(os.getenv("IDLE_PURGE_MIN_IDLE_SECONDS", "1200"))
IDLE_PURGE_INTERVAL_SECONDS = int(os.getenv("IDLE_PURGE_INTERVAL_SECONDS", "5400"))
COOKIE_REUSE_MINUTES = int(os.getenv("COOKIE_REUSE_MINUTES", "22"))
COOKIE_SOFT_REFRESH_MINUTES = int(os.getenv("COOKIE_SOFT_REFRESH_MINUTES", str(COOKIE_REUSE_MINUTES)))
TASK_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("TASK_HEARTBEAT_INTERVAL_SECONDS", "15"))
CLOUDFLARE_PASSIVE_WAIT_SECONDS = int(os.getenv("CLOUDFLARE_PASSIVE_WAIT_SECONDS", "18"))
CLOUDFLARE_POST_CLICK_WAIT_SECONDS = int(os.getenv("CLOUDFLARE_POST_CLICK_WAIT_SECONDS", "8"))
CLOUDFLARE_SECOND_LOOK_SECONDS = int(os.getenv("CLOUDFLARE_SECOND_LOOK_SECONDS", "10"))
CLOUDFLARE_PASSWORD_WAIT_SECONDS = int(os.getenv("CLOUDFLARE_PASSWORD_WAIT_SECONDS", "35"))
RESOURCE_GUARD_MIN_AVAILABLE_MB = int(os.getenv("RESOURCE_GUARD_MIN_AVAILABLE_MB", "900"))
RESOURCE_GUARD_MAX_BROWSER_PROCS = int(os.getenv("RESOURCE_GUARD_MAX_BROWSER_PROCS", str(max(18, MAX_WORKERS * 10))))
RESOURCE_GUARD_HARD_BROWSER_PROCS = int(os.getenv("RESOURCE_GUARD_HARD_BROWSER_PROCS", str(max(24, MAX_WORKERS * 14))))
RESOURCE_GUARD_BROWSER_PRESSURE_MB = int(os.getenv("RESOURCE_GUARD_BROWSER_PRESSURE_MB", str(max(1400, RESOURCE_GUARD_MIN_AVAILABLE_MB + 350))))
RESOURCE_GUARD_MAX_WAIT_SECONDS = int(os.getenv("RESOURCE_GUARD_MAX_WAIT_SECONDS", "75"))
RESOURCE_GUARD_COOLDOWN_SECONDS = int(os.getenv("RESOURCE_GUARD_COOLDOWN_SECONDS", "180"))
AUTO_DISPATCH_TARGET_AGE_MINUTES = int(os.getenv("AUTO_DISPATCH_TARGET_AGE_MINUTES", "480"))
AUTO_DISPATCH_REFRESH_MARGIN_MINUTES = int(os.getenv("AUTO_DISPATCH_REFRESH_MARGIN_MINUTES", "30"))
AUTO_DISPATCH_MAX_ENQUEUE_PER_CYCLE = int(os.getenv("AUTO_DISPATCH_MAX_ENQUEUE_PER_CYCLE", "1"))
AUTO_DISPATCH_INCLUDE_EMPTY = os.getenv("AUTO_DISPATCH_INCLUDE_EMPTY", "false").lower() == "true"
WORKER_RECYCLE_AFTER_TASKS = int(os.getenv("WORKER_RECYCLE_AFTER_TASKS", "8"))
WORKER_RECYCLE_AFTER_SECONDS = int(os.getenv("WORKER_RECYCLE_AFTER_SECONDS", "7200"))

# ====================================================================
# SISTEMA DE ROTAÇÃO DE PROXIES (PREPARAÇÃO PARA O MIKROTIK)
# ====================================================================
PROXY_ENV = os.getenv("PROXY_LIST", "socks5://189.124.176.141:45123")
PROXY_LIST = [p.strip() for p in PROXY_ENV.split(',') if p.strip()]
BROWSER_PROCESS_TERMS = ("chrome", "chromedriver", "chrome_crashpad", "xvfb")
BROWSER_PROFILE_BASE_DIR = os.getenv("BROWSER_PROFILE_BASE_DIR", "/tmp/onelog_browser_profiles")
BOT_COOKIE_NAMES = (
    "cf_clearance",
    "__cf_bm",
    "__cfseq",
    "cf_ob_info",
    "cf_use_ob",
)
BOT_COOKIE_PREFIXES = ("cf_", "__cf", "cf_chl", "cf-chl")
BOT_COOKIE_DOMAIN_MARKERS = ("bb.com.br", "loginweb.bb.com.br", "juridico.bb.com.br")

def get_random_proxy():
    if not PROXY_LIST:
        return None
    return random.choice(PROXY_LIST)

def get_account_profile_dir(account_id):
    profile_dir = os.path.join(BROWSER_PROFILE_BASE_DIR, f"account_{account_id}")
    os.makedirs(profile_dir, exist_ok=True)
    return profile_dir

def _is_bot_cookie(cookie):
    name = (cookie.get("name") or "").lower()
    domain = (cookie.get("domain") or "").lower()
    if name in BOT_COOKIE_NAMES:
        return True
    if any(name.startswith(prefix) for prefix in BOT_COOKIE_PREFIXES):
        return any(marker in domain for marker in BOT_COOKIE_DOMAIN_MARKERS)
    return False

def clear_cloudflare_artifacts(sb, thread_id, setor, reason):
    removed = []
    try:
        sb.driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass

    try:
        all_cookies = sb.driver.execute_cdp_cmd("Network.getAllCookies", {}).get("cookies", [])
        for cookie in all_cookies:
            if not _is_bot_cookie(cookie):
                continue
            params = {
                "name": cookie.get("name"),
                "domain": cookie.get("domain"),
                "path": cookie.get("path") or "/",
            }
            if cookie.get("secure") is not None:
                params["secure"] = cookie.get("secure")
            if cookie.get("httpOnly") is not None:
                params["httpOnly"] = cookie.get("httpOnly")
            if cookie.get("sameSite"):
                params["sameSite"] = cookie.get("sameSite")
            try:
                sb.driver.execute_cdp_cmd("Network.deleteCookies", params)
                removed.append(f"{cookie.get('name')}@{cookie.get('domain')}")
            except Exception as delete_error:
                logger.warning(
                    f"[ROBÔ {thread_id} | {setor}] Falha ao remover cookie de bot "
                    f"{cookie.get('name')}@{cookie.get('domain')}: {delete_error}"
                )
    except Exception as cookie_error:
        logger.warning(f"[ROBÔ {thread_id} | {setor}] Não foi possível inspecionar cookies do Cloudflare: {cookie_error}")

    try:
        sb.execute_script(
            """
            try { window.localStorage.removeItem("cf_chl_prog"); } catch (e) {}
            try { window.sessionStorage.removeItem("cf_chl_prog"); } catch (e) {}
            """
        )
    except Exception as storage_error:
        logger.warning(f"[ROBÔ {thread_id} | {setor}] Falha ao limpar storage do Cloudflare: {storage_error}")

    logger.info(
        f"[ROBÔ {thread_id} | {setor}] Faxina cirúrgica do Cloudflare ({reason}) "
        f"removeu {len(removed)} cookie(s): {', '.join(removed) if removed else 'nenhum'}"
    )
    return removed

redis_client = None

def get_redis():
    global redis_client
    if redis_client is None:
        redis_client = redis.Redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)
    return redis_client

def get_local_now():
    try:
        return datetime.now(ZoneInfo(LOCAL_TIMEZONE))
    except Exception:
        return datetime.utcnow() - timedelta(hours=3)

def is_auto_dispatch_window(now=None):
    now = now or get_local_now()
    return now.weekday() in AUTO_DISPATCH_WEEKDAYS and AUTO_DISPATCH_START_HOUR <= now.hour < AUTO_DISPATCH_END_HOUR

def touch_heartbeat(thread_id):
    if thread_id:
        get_redis().set(f"heartbeat:{thread_id}", str(int(time.time())), ex=HEARTBEAT_TTL_SECONDS)

def set_worker_state(
    thread_id,
    state,
    setor=None,
    message=None,
    account_id=None,
    requester_username=None,
    requester_display_name=None,
    request_id=None
):
    if not thread_id:
        return
    existing = {}
    raw_existing = get_redis().get(f"worker:state:{thread_id}")
    if raw_existing:
        try:
            existing = json.loads(raw_existing)
        except Exception:
            existing = {}
    clear_context = state == "idle"
    payload = {
        "thread_id": thread_id,
        "state": state,
        "setor": setor if setor is not None else existing.get("setor"),
        "message": message if message is not None else existing.get("message"),
        "account_id": None if clear_context else (account_id if account_id is not None else existing.get("account_id")),
        "requester_username": None if clear_context else (requester_username if requester_username is not None else existing.get("requester_username")),
        "requester_display_name": None if clear_context else (requester_display_name if requester_display_name is not None else existing.get("requester_display_name")),
        "request_id": None if clear_context else (request_id if request_id is not None else existing.get("request_id")),
        "ts": int(time.time())
    }
    get_redis().set(f"worker:state:{thread_id}", json.dumps(payload), ex=HEARTBEAT_TTL_SECONDS)

def mark_system_activity():
    get_redis().set("system:last_activity_ts", str(int(time.time())), ex=86400)

def activate_infra_cooldown(reason, seconds=INFRA_COOLDOWN_SECONDS):
    get_redis().setex("lock:infra_cooldown", seconds, reason)
    logger.warning(f"🛑 Infra-cooldown ativado por {seconds}s: {reason}")

def arm_account_backoff(account_id, reason, seconds):
    get_redis().setex(f"lock:queue:{account_id}", seconds, "1")
    get_redis().setex(f"cooldown:account:{account_id}", seconds, reason)

def get_account_backoff_seconds(account_id):
    ttl = get_redis().ttl(f"cooldown:account:{account_id}")
    return ttl if ttl and ttl > 0 else 0

def has_recent_other_heartbeats(exclude_thread_id=None, freshness_seconds=120):
    now = int(time.time())
    for key in get_redis().scan_iter("heartbeat:*"):
        try:
            thread_id = key.split(":")[-1]
            if exclude_thread_id and str(thread_id) == str(exclude_thread_id):
                continue
            hb_str = get_redis().get(key)
            if hb_str and (now - int(hb_str)) <= freshness_seconds:
                return True
        except Exception:
            continue
    return False

def has_recent_heartbeats(freshness_seconds=120):
    return has_recent_other_heartbeats(exclude_thread_id=None, freshness_seconds=freshness_seconds)

def get_idle_seconds():
    last_ts = get_redis().get("system:last_activity_ts")
    if not last_ts:
        return 10**9
    try:
        return max(0, int(time.time()) - int(last_ts))
    except Exception:
        return 0

def queues_are_empty():
    return get_redis().llen("queue:login_requests") == 0 and get_redis().llen("queue:priority_logins") == 0

def system_is_idle():
    return queues_are_empty() and not has_recent_heartbeats()

def run_maintenance_purge(reason, throttle_key=None, throttle_seconds=None):
    if not system_is_idle():
        return False
    if get_idle_seconds() < IDLE_PURGE_MIN_IDLE_SECONDS:
        return False
    if throttle_key and throttle_seconds and not get_redis().set(throttle_key, "1", ex=throttle_seconds, nx=True):
        return False
    if not get_redis().set("lock:purge_in_progress", "1", ex=300, nx=True):
        return False
    try:
        activate_infra_cooldown(reason, seconds=PURGE_INFRA_COOLDOWN_SECONDS)
        logger.info(f"🧼 [MAINTENANCE] Expurgo preventivo iniciado: {reason}")
        faxina_global_de_emergencia()
        mark_system_activity()
        return True
    finally:
        get_redis().delete("lock:purge_in_progress")

def maybe_run_pre_dispatch_purge(now_local):
    if now_local.weekday() not in AUTO_DISPATCH_WEEKDAYS:
        return False
    if PRE_DISPATCH_PURGE_LEAD_MINUTES <= 0:
        return False
    start_of_window = now_local.replace(
        hour=AUTO_DISPATCH_START_HOUR, minute=0, second=0, microsecond=0
    )
    delta_seconds = (start_of_window - now_local).total_seconds()
    if not (0 <= delta_seconds <= PRE_DISPATCH_PURGE_LEAD_MINUTES * 60):
        return False
    day_key = f"maintenance:pre_dispatch_purge:{now_local.strftime('%Y-%m-%d')}"
    return run_maintenance_purge(
        f"pré-aquecimento da memória antes da janela ativa ({now_local.strftime('%Y-%m-%d')})",
        throttle_key=day_key,
        throttle_seconds=86400,
    )

def maybe_run_idle_purge():
    return run_maintenance_purge(
        f"sistema ocioso por {get_idle_seconds()}s",
        throttle_key="lock:idle_purge",
        throttle_seconds=IDLE_PURGE_INTERVAL_SECONDS,
    )

def is_browser_process(proc_info):
    nome = (proc_info.get('name') or '').lower()
    cmdline = " ".join(proc_info.get('cmdline') or []).lower()
    status = str(proc_info.get('status') or '').lower()
    if status == 'zombie' or '<defunct>' in nome or '<defunct>' in cmdline:
        return False
    return any(term in nome or term in cmdline for term in BROWSER_PROCESS_TERMS)

def reap_finished_children():
    reaped = 0
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
            reaped += 1
        except ChildProcessError:
            break
        except OSError:
            break
    if reaped:
        logger.info(f"🧼 Reaper recolheu {reaped} processo(s) filho(s) finalizado(s).")
    return reaped

def count_browser_processes():
    total = 0
    try:
        for proc in psutil.process_iter(['name', 'cmdline', 'status']):
            try:
                if is_browser_process(proc.info):
                    total += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception:
        return 0
    return total

def get_host_pressure_snapshot():
    reasons = []
    available_mb = None
    browser_procs = count_browser_processes()
    try:
        available_mb = int(psutil.virtual_memory().available / (1024 * 1024))
        if available_mb < RESOURCE_GUARD_MIN_AVAILABLE_MB:
            reasons.append(f"memória livre baixa ({available_mb}MB)")
    except Exception:
        available_mb = None
    if browser_procs >= RESOURCE_GUARD_HARD_BROWSER_PROCS:
        reasons.append(f"excesso crítico de processos de navegador ({browser_procs})")
    elif browser_procs >= RESOURCE_GUARD_MAX_BROWSER_PROCS and (
        available_mb is None or available_mb < RESOURCE_GUARD_BROWSER_PRESSURE_MB
    ):
        reasons.append(
            f"muitos processos de navegador ({browser_procs}) com pouca folga de RAM ({available_mb if available_mb is not None else '?'}MB)"
        )
    return {
        "available_mb": available_mb,
        "browser_procs": browser_procs,
        "reasons": reasons,
        "under_pressure": bool(reasons),
    }

def wait_for_host_capacity(thread_id, setor, account_id=None):
    deadline = time.time() + max(5, RESOURCE_GUARD_MAX_WAIT_SECONDS)
    last_reason = None
    purge_attempted = False

    while time.time() < deadline:
        snapshot = get_host_pressure_snapshot()
        if not snapshot["under_pressure"]:
            return None

        reason = ", ".join(snapshot["reasons"])
        last_reason = reason
        set_worker_state(thread_id, "resource_guard", setor=setor, message=f"Host sob pressão: {reason}", account_id=account_id)
        logger.warning(f"[ROBÔ {thread_id} | {setor}] Guardião de recursos segurando nova abertura: {reason}")

        if not purge_attempted and not has_recent_other_heartbeats(exclude_thread_id=thread_id):
            purge_attempted = True
            activate_infra_cooldown(f"guardião de recursos: {reason}", seconds=RESOURCE_GUARD_COOLDOWN_SECONDS)
            faxina_global_de_emergencia()
            time.sleep(4)
            continue

        time.sleep(5)

    return last_reason or "host sob pressão"

def update_status(setor, msg, concluido=False, erro=False, imagem=None, thread_id=None):
    touch_heartbeat(thread_id)
    set_worker_state(thread_id, "running", setor=setor, message=msg)
    mark_system_activity()
    status = {"mensagem": msg, "concluido": concluido, "erro": erro, "imagem": imagem}
    get_redis().set(f"status:{setor}", json.dumps(status))
    
    prefix = f"[ROBÔ {thread_id} | {setor}]" if thread_id else f"[{setor}]"
    logger.info(f"{prefix} {msg}")

def snapshot(sb, setor, nome_arquivo, thread_id=None):
    if not DEBUG_MODE: return None
    touch_heartbeat(thread_id)
    mark_system_activity()
    if not os.path.exists('shared'): os.makedirs('shared')
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{setor}_{nome_arquivo}_{ts}.png"
    sb.save_screenshot(os.path.join("shared", filename))
    img_url = f"{BASE_URL}/shared/{filename}"
    
    prefix = f"[ROBÔ {thread_id} | {setor}]" if thread_id else f"[{setor}]"
    logger.info(f"{prefix} 📸 Snapshot gerado: {img_url}")
    return img_url

def start_task_heartbeat(thread_id):
    stop_event = threading.Event()

    def pulse():
        while not stop_event.wait(TASK_HEARTBEAT_INTERVAL_SECONDS):
            touch_heartbeat(thread_id)

    heartbeat_thread = threading.Thread(target=pulse, daemon=True)
    heartbeat_thread.start()
    return stop_event

def limpar_memoria_residual(sb_instance=None):
    """
    Desliga os motores de forma educada e silenciosa. 
    Fecha o navegador e limpa a memória RAM sem matar as conexões 
    dos advogados que estão logando em outras threads.
    """
    if sb_instance:
        try:
            sb_instance.driver.quit()
        except Exception:
            pass

    # Garante que as sobras do Xvfb (Monitor Virtual) e Chromes zumbis da própria thread acabem.
    try:
        proc = psutil.Process(os.getpid())
        for child in proc.children(recursive=True):
            try:
                child_info = {
                    "name": child.name(),
                    "cmdline": child.cmdline(),
                    "status": child.status()
                }
                if is_browser_process(child_info):
                    child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass
    finally:
        reap_finished_children()

def faxina_global_de_emergencia():
    """Vassoura nuclear: elimina qualquer Chrome do sistema (incluindo órfãos) para restaurar a RAM."""
    logger.warning("🧹 Iniciando EXPURGO GLOBAL para limpar zombies órfãos do Docker e restaurar a RAM a 100%!")
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'status']):
            try:
                if is_browser_process(proc.info):
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
    except Exception as e:
        logger.error(f"Erro no expurgo global: {e}")
    finally:
        reap_finished_children()

def processar_login(account_id, setor_solicitado, thread_id, requester_username=None, requester_display_name=None, request_id=None):
    db = SessionLocal()
    hoje = datetime.utcnow().strftime('%Y-%m-%d')
    
    try:
        account = db.query(AccountBB).filter(AccountBB.id == int(account_id)).first()
        if not account: return
        touch_heartbeat(thread_id)
        if requester_username or request_id:
            solicitante = requester_display_name or requester_username
            logger.info(
                f"[ROBÔ {thread_id} | {setor_solicitado}] Origem da solicitação: "
                f"{solicitante or 'desconhecida'}"
                f"{f' ({requester_username})' if requester_display_name and requester_username else ''}"
                f"{f' | req={request_id}' if request_id else ''}"
            )

        cooldown_restante = get_account_backoff_seconds(account_id)
        if cooldown_restante > 0:
            logger.warning(f"[ROBÔ {thread_id} | {setor_solicitado}] Conta {account_id} em quarentena de backend por mais {cooldown_restante}s. Pulando nova abertura de navegador.")
            update_status(setor_solicitado, f"Infraestrutura em estabilização. Nova tentativa automática em {max(1, cooldown_restante // 60)} min.", erro=True, thread_id=thread_id)
            return

        # =======================================================================
        # 🛑 BLINDAGEM CONTRA TRABALHO DUPLICADO (Otimização de Servidor)
        # =======================================================================
        if account.cookie_payload and account.last_login_at:
            minutos_passados = (datetime.utcnow() - account.last_login_at).total_seconds() / 60
            if minutos_passados < COOKIE_SOFT_REFRESH_MINUTES:
                logger.info(f"[ROBÔ {thread_id} | {setor_solicitado}] ♻️ Tarefa duplicada detectada! A conta {account_id} já tem cookies frescos ({minutos_passados:.1f}m). Abortando para poupar servidor.")
                get_redis().delete(f"lock:queue:{account_id}")
                update_status(setor_solicitado, "Sessão renovada e salva no Pool!", concluido=True, thread_id=thread_id)
                return
        # =======================================================================

        logger.info(f"[ROBÔ {thread_id} | {setor_solicitado}] Aplicando Jitter aleatório para despistar balanceadores do BB...")
        time.sleep(random.uniform(2.0, 7.0))

        setor = setor_solicitado
        usuario, senha = account.login, account.senha
        
        update_status(setor, "Iniciando robô stealth...", thread_id=thread_id)
        max_tentativas_gerais = 3
        
        for tentativa in range(1, max_tentativas_gerais + 1):
            logger.info(f"[ROBÔ {thread_id} | {setor}] === TENTATIVA {tentativa}/{max_tentativas_gerais} ===")
            touch_heartbeat(thread_id)
            
            sb_instance = None 
            proxy_escolhido = get_random_proxy()
            startup_lock = False
            
            try:
                host_pressure_reason = wait_for_host_capacity(thread_id, setor, account_id=account_id)
                if host_pressure_reason:
                    raise Exception(f"Host sobrecarregado antes de abrir Chrome: {host_pressure_reason}")

                logger.info(f"[ROBÔ {thread_id} | {setor}] IP/Proxy alocado para esta missão: {proxy_escolhido}")

                while not get_redis().set("lock:chrome_startup", str(thread_id), ex=CHROME_STARTUP_LOCK_TTL, nx=True):
                    update_status(setor, "Aguardando janela segura para abrir o Chrome...", thread_id=thread_id)
                    time.sleep(2)
                startup_lock = True

                profile_dir = get_account_profile_dir(account_id)
                with SB(
                    uc=True,
                    test=True,
                    headless=False,
                    xvfb=True,
                    proxy=proxy_escolhido,
                    page_load_strategy="normal",
                    user_data_dir=profile_dir,
                ) as sb:
                    sb_instance = sb 
                    if startup_lock:
                        get_redis().delete("lock:chrome_startup")
                        startup_lock = False
                    
                    logger.info(f"[ROBÔ {thread_id} | {setor}] Aguardando a Catraca do Banco liberar...")
                    
                    while not get_redis().set("lock:bb_door", "1", ex=60, nx=True):
                        touch_heartbeat(thread_id)
                        time.sleep(3)
                    
                    try:
                        update_status(setor, f"Catraca liberada! Abrindo navegador (Tentativa {tentativa}/{max_tentativas_gerais})...", thread_id=thread_id)
                        
                        logger.info(f"[ROBÔ {thread_id} | {setor}] Reaproveitando perfil persistente em {profile_dir}")
                        clear_cloudflare_artifacts(
                            sb,
                            thread_id,
                            setor,
                            reason="higiene leve antes de abrir o login",
                        )
                        sb.driver.uc_open_with_reconnect(
                            'https://loginweb.bb.com.br/sso/XUI/?realm=/paj&goto=https://juridico.bb.com.br/wfj#login',
                            reconnect_time=3,
                        )
                        sb.sleep(5)
                        
                        img = snapshot(sb, setor, f"01_inicio_T{tentativa}", thread_id=thread_id)
                        
                        update_status(setor, "Digitando usuário...", imagem=img, thread_id=thread_id)
                        sb.type("#idToken1", usuario)
                        sb.sleep(1)
                        sb.driver.uc_click("#loginButton_0", reconnect_time=1.5)
                        
                        update_status(setor, "Aguardando validação passiva do Cloudflare...", imagem=img, thread_id=thread_id)
                        password_ready = False
                        try:
                            sb.wait_for_element("#idToken3", timeout=CLOUDFLARE_PASSIVE_WAIT_SECONDS)
                            password_ready = True
                            logger.info(f"[ROBÔ {thread_id} | {setor}] >>> Campo de senha apareceu sem clique no captcha.")
                        except Exception:
                            password_ready = False

                        img = snapshot(sb, setor, f"02_antes_captcha_T{tentativa}", thread_id=thread_id)
                        
                        captcha_container = "div.cf-turnstile"
                        
                        if not password_ready and sb.is_element_visible(captcha_container):
                            update_status(setor, "Cloudflare detectado. Tentando validação stealth...", imagem=img, thread_id=thread_id)
                            sb.sleep(3)
                            
                            try:
                                sb.driver.uc_click(captcha_container, reconnect_time=2)
                                logger.info(f"[ROBÔ {thread_id} | {setor}] >>> Clique UC no captcha realizado.")
                            except Exception as e:
                                logger.warning(f"[ROBÔ {thread_id} | {setor}] Aviso no clique UC: {e}")
                            
                            update_status(setor, "Aguardando validação do clique...", thread_id=thread_id)
                            sb.sleep(CLOUDFLARE_POST_CLICK_WAIT_SECONDS)
                                
                            img = snapshot(sb, setor, f"03_pos_clique_T{tentativa}", thread_id=thread_id)
                        
                        update_status(setor, "Aguardando campo de senha...", imagem=img, thread_id=thread_id)
                        
                        # =======================================================================
                        # O DETECTOR DE ARMADILHAS DO CLOUDFLARE (POPUP)
                        # =======================================================================
                        try:
                            sb.wait_for_element("#idToken3", timeout=CLOUDFLARE_PASSWORD_WAIT_SECONDS)
                            logger.info(f"[ROBÔ {thread_id} | {setor}] >>> SUCESSO! Campo de senha apareceu!")
                        except Exception as wait_e:
                            cloudflare_ainda_visivel = False
                            try:
                                cloudflare_ainda_visivel = (
                                    sb.is_element_visible("div.cf-turnstile")
                                    or sb.is_text_visible("Verify you are human")
                                    or sb.is_text_visible("Verifying")
                                )
                            except Exception:
                                cloudflare_ainda_visivel = False

                            if cloudflare_ainda_visivel:
                                update_status(setor, "Cloudflare ainda em validação. Fazendo segunda checagem antes de abortar...", imagem=img, thread_id=thread_id)
                            else:
                                update_status(setor, "Tela lenta após desafio. Revalidando campo de senha antes de abortar...", imagem=img, thread_id=thread_id)

                            sb.sleep(CLOUDFLARE_SECOND_LOOK_SECONDS)
                            img = snapshot(sb, setor, f"03b_rechecagem_T{tentativa}", thread_id=thread_id)

                            try:
                                sb.wait_for_element("#idToken3", timeout=max(8, CLOUDFLARE_SECOND_LOOK_SECONDS))
                                logger.info(f"[ROBÔ {thread_id} | {setor}] >>> Campo de senha apareceu na segunda checagem. Falso alarme evitado.")
                            except Exception as second_wait_e:
                                logger.error(f"[ROBÔ {thread_id} | {setor}] 🚨 ARMADILHA DETECTADA! O campo de senha não carregou mesmo após rechecagem.")
                                raise Exception(
                                    "Armadilha Cloudflare: campo de senha ausente após dupla checagem. "
                                    f"Erro inicial: {wait_e}. Erro final: {second_wait_e}"
                                )
                        # =======================================================================
                        
                        # TELEMETRIA: Sucesso de Autenticação (Fura-bloqueio)
                        get_redis().hincrby(f"metrics:robot_success:{hoje}", f"ROBÔ {thread_id}", 1)
                        
                    finally:
                        get_redis().delete("lock:bb_door")
                        logger.info(f"[ROBÔ {thread_id} | {setor}] Catraca liberada para o próximo robô da fila.")
                    
                    img = snapshot(sb, setor, f"04_senha_visivel_T{tentativa}", thread_id=thread_id)
                    
                    update_status(setor, "Digitando senha...", imagem=img, thread_id=thread_id)
                    sb.type("#idToken3", senha)
                    sb.sleep(1)
                    sb.click("input#loginButton_0[name='callback_4']")
                    update_status(setor, "Validando acesso...", imagem=img, thread_id=thread_id)
                    
                    max_retries_url = 15
                    logged_in = False
                    for _ in range(max_retries_url):
                        touch_heartbeat(thread_id)
                        current_url = sb.get_current_url()
                        if "juridico.bb.com.br" in current_url and "loginweb" not in current_url:
                            logged_in = True
                            img = snapshot(sb, setor, f"05_sucesso_portal_T{tentativa}", thread_id=thread_id)
                            break
                        sb.sleep(4)
                    
                    if logged_in:
                        cookies = sb.driver.get_cookies()
                        COOKIES_BLOQUEADOS = ["PD-S-SESSION-ID", "JSESSIONID", "cf_clearance", "__cf_bm"]
                        
                        cookies_limpos = []
                        for cookie in cookies:
                            nome_cookie = cookie['name']
                            if nome_cookie in COOKIES_BLOQUEADOS or nome_cookie.startswith('TS01') or 'BIGipServer' in nome_cookie:
                                logger.info(f"[ROBÔ {thread_id} | {setor}] Filtro Ativado: Destruindo cookie tóxico -> {nome_cookie}")
                                continue
                            cookies_limpos.append(cookie)
                            
                        try: real_ua = sb.execute_script("return navigator.userAgent;")
                        except: real_ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                        
                        previous_login_at = account.last_login_at
                        account.cookie_payload = json.dumps(cookies_limpos)
                        account.last_login_at = datetime.utcnow()
                        account.user_agent_used = real_ua
                        db.commit()

                        if previous_login_at:
                            cycle_minutes = (account.last_login_at - previous_login_at).total_seconds() / 60
                            if cycle_minutes > 0:
                                get_redis().incrbyfloat(f"metrics:session_cycle_sum:{hoje}", cycle_minutes)
                                get_redis().incr(f"metrics:session_cycle_count:{hoje}")
                                previous_max = float(get_redis().get(f"metrics:session_cycle_max:{hoje}") or 0)
                                if cycle_minutes > previous_max:
                                    get_redis().set(f"metrics:session_cycle_max:{hoje}", round(cycle_minutes, 2))

                        update_status(setor, "Acesso concedido e salvo no Pool!", concluido=True, imagem=img, thread_id=thread_id)
                        
                        get_redis().set("metrics:captcha_consecutive_failures", 0)
                        get_redis().set("metrics:infra_consecutive_failures", 0)
                        get_redis().delete("lock:infra_cooldown")
                        get_redis().delete(f"cooldown:account:{account_id}")
                        get_redis().delete(f"lock:queue:{account_id}")
                        for guard_key in list(get_redis().scan_iter(f"guard:recent_cache_login:{account_id}:*")):
                            get_redis().delete(guard_key)
                        
                        limpar_memoria_residual(sb_instance)
                        return 
                        
                    else:
                        raise Exception("Timeout ao aguardar o portal jurídico carregar após a senha.")
                        
            except Exception as e:
                if startup_lock:
                    get_redis().delete("lock:chrome_startup")
                logger.warning(f"[ROBÔ {thread_id} | {setor}] Falha na tentativa {tentativa}: {e}")
                
                get_redis().delete("lock:bb_door")
                
                # TELEMETRIA: Registro de Falhas e Categorização Inteligente
                get_redis().hincrby(f"metrics:robot_error:{hoje}", f"ROBÔ {thread_id}", 1)
                
                err_msg = str(e).lower()
                is_infra_error = False
                is_cloudflare_trap = False
                fail_fast = False
                
                if (
                    "errno 11" in err_msg
                    or "resource temporarily unavailable" in err_msg
                    or "-5" in err_msg
                    or "failed to start a thread" in err_msg
                    or "host sobrecarregado" in err_msg
                    or "cannot connect to chrome" in err_msg
                    or "session not created" in err_msg
                    or "not reachable" in err_msg
                    or "unable to discover open pages" in err_msg
                    or "recursion" in err_msg
                    or "chrome not reachable" in err_msg
                    or "unexpected keyword argument" in err_msg
                ):
                    motivo_falha = "Esgotamento de Recursos (OS/Docker)"
                    is_infra_error = True
                    fail_fast = True
                elif "armadilha cloudflare" in err_msg:
                    motivo_falha = "Bloqueio Cloudflare (Armadilha/Popup)"
                    is_cloudflare_trap = True
                elif "timeout" in err_msg or "nosuchelement" in err_msg:
                    motivo_falha = "Timeout na Navegação / Elemento não encontrado"
                else:
                    motivo_falha = "Erro Desconhecido"
                
                get_redis().hincrby(f"metrics:error_reasons:{hoje}", motivo_falha, 1)
                
                # Só soma no medidor do Cloudflare se NÃO for erro de infraestrutura
                if not is_infra_error:
                    fail_count = get_redis().incr("metrics:captcha_consecutive_failures")
                    logger.info(f"[ROBÔ {thread_id} | {setor}] Medidor de bloqueios Cloudflare: {fail_count}/6")
                    
                    if fail_count >= 6: 
                        logger.error(f"[ROBÔ {thread_id} | {setor}] 🚨 NÍVEL DE AMEAÇA MÁXIMO ATINGIDO NO CLOUDFLARE!")
                        logger.error(f"[ROBÔ {thread_id} | {setor}] Acionando protocolo de Fôlego para toda a frota.")
                        get_redis().setex("lock:cooldown", 180, "true") 
                        get_redis().set("metrics:captcha_consecutive_failures", 0) 
                else:
                    logger.info(f"[ROBÔ {thread_id} | {setor}] ⚠️ Falha classificada como Infraestrutura. Medidor do Cloudflare não será acionado.")
                    infra_fail_count = get_redis().incr("metrics:infra_consecutive_failures")
                    if infra_fail_count >= 3:
                        activate_infra_cooldown(f"pico de falhas de infraestrutura ({infra_fail_count} seguidas)")
                        fail_fast = True

                if is_cloudflare_trap:
                    logger.warning(f"[ROBÔ {thread_id} | {setor}] Cloudflare explícito detectado após dupla checagem. Mantendo retry normal, sem cooldown pesado imediato.")
                    if sb_instance:
                        try:
                            clear_cloudflare_artifacts(
                                sb_instance,
                                thread_id,
                                setor,
                                reason="trap explícita antes da próxima tentativa",
                            )
                        except Exception as cleanup_error:
                            logger.warning(f"[ROBÔ {thread_id} | {setor}] Falha na faxina cirúrgica pós-trap: {cleanup_error}")
                elif is_infra_error:
                    logger.warning(f"[ROBÔ {thread_id} | {setor}] Infraestrutura do host degradada. Abortando novas tentativas imediatas para poupar RAM/threads.")
                
                if sb_instance:
                     try:
                         img = snapshot(sb_instance, setor, f"erro_tentativa_{tentativa}", thread_id=thread_id)
                     except Exception:
                         img = None
                else: img = None
                
                limpar_memoria_residual(sb_instance)
                if is_infra_error and not has_recent_other_heartbeats(exclude_thread_id=thread_id):
                    logger.warning(f"[ROBÔ {thread_id} | {setor}] Sem outros robôs ativos. Acionando faxina global automática para evitar reset manual.")
                    faxina_global_de_emergencia()

                if tentativa == max_tentativas_gerais or fail_fast:
                    logger.error(f"[ROBÔ {thread_id} | {setor}] FALHA DEFINITIVA APÓS {tentativa} TENTATIVA(S).")
                    backoff_seconds = ACCOUNT_INFRA_BACKOFF_SECONDS if is_infra_error else ACCOUNT_RETRY_BACKOFF_SECONDS
                    arm_account_backoff(account_id, motivo_falha, backoff_seconds)
                    update_status(setor, f"Falha no processo. Nova tentativa automática em {max(1, backoff_seconds // 60)} min.", erro=True, imagem=img, thread_id=thread_id)
                    if is_infra_error:
                        activate_infra_cooldown(f"{setor} em quarentena por falha de infraestrutura", seconds=INFRA_COOLDOWN_SECONDS)
                    return
                else:
                    update_status(setor, f"Sessão queimada. Reiniciando navegador do zero (Tentativa {tentativa+1})...", imagem=img, thread_id=thread_id)
                    time.sleep(3)
    finally:
        db.close()

def worker_loop(thread_id):
    logger.info(f"[ROBÔ {thread_id}] Em posição e aguardando missões...")
    set_worker_state(thread_id, "idle", message="Em posição e aguardando missões...")
    tarefas_processadas = 0
    started_at = time.time()
    
    while True:
        try:
            if get_redis().exists("lock:cooldown"):
                logger.info(f"[ROBÔ {thread_id}] 🛑 Modo fôlego ativo. Aguardando a poeira baixar...")
                set_worker_state(thread_id, "cooldown", message="Modo fôlego ativo")
                time.sleep(20)
                continue
            if get_redis().exists("lock:infra_cooldown"):
                logger.info(f"[ROBÔ {thread_id}] 🛑 Backend em estabilização. Aguardando antes de abrir novos navegadores...")
                set_worker_state(thread_id, "infra_cooldown", message="Backend em estabilização")
                time.sleep(20)
                continue

            task = get_redis().brpop(["queue:priority_logins", "queue:login_requests"], timeout=10)
            if not task:
                set_worker_state(thread_id, "idle", message="Aguardando missões...")
                continue
                
            queue_name, task_data_str = task
            
            if get_redis().exists("lock:cooldown"):
                logger.warning(f"[ROBÔ {thread_id}] Fôlego ativado no meio do caminho! Devolvendo tarefa para a fila {queue_name}...")
                set_worker_state(thread_id, "cooldown", message="Tarefa devolvida por cooldown")
                get_redis().rpush(queue_name, task_data_str)
                time.sleep(20)
                continue

            try:
                task_data = json.loads(task_data_str)
                account_id = task_data['id']
                setor = task_data['setor']
                is_priority = task_data.get('priority', False)
                requester_username = task_data.get('requester_username')
                requester_display_name = task_data.get('requester_display_name')
                request_id = task_data.get('request_id')
            except Exception:
                account_id = task_data_str
                setor = "GERAL"
                is_priority = False
                requester_username = None
                requester_display_name = None
                request_id = None

            if is_priority:
                logger.info(
                    f"[ROBÔ {thread_id} | {setor}] 🚨 EMERGÊNCIA VIP 🚨 Conta ID: {account_id} "
                    f"(Fura-fila acionado)"
                    f"{f' | solicitante={requester_username}' if requester_username else ''}"
                    f"{f' | req={request_id}' if request_id else ''}"
                )
            else:
                logger.info(
                    f"[ROBÔ {thread_id} | {setor}] Nova tarefa capturada! Conta ID: {account_id}"
                    f"{f' | solicitante={requester_username}' if requester_username else ''}"
                    f"{f' | req={request_id}' if request_id else ''}"
                )
            set_worker_state(
                thread_id,
                "running",
                setor=setor,
                message="Nova tarefa capturada",
                account_id=account_id,
                requester_username=requester_username,
                requester_display_name=requester_display_name,
                request_id=request_id
            )
            mark_system_activity()
                
            # =========================================================================
            # ❤️ MONITOR CARDÍACO: O Robô assina o ponto antes de começar
            # =========================================================================
            touch_heartbeat(thread_id)
            heartbeat_stop = start_task_heartbeat(thread_id)
            try:
                processar_login(
                    account_id,
                    setor,
                    thread_id,
                    requester_username=requester_username,
                    requester_display_name=requester_display_name,
                    request_id=request_id
                )
            finally:
                heartbeat_stop.set()
                # Retira o pulso ao concluir (com sucesso ou falha limpa)
                get_redis().delete(f"heartbeat:{thread_id}")
                set_worker_state(thread_id, "idle", message="Aguardando missões...")
                tarefas_processadas += 1
                worker_uptime = int(time.time() - started_at)
                reached_task_limit = WORKER_RECYCLE_AFTER_TASKS > 0 and tarefas_processadas >= WORKER_RECYCLE_AFTER_TASKS
                reached_time_limit = WORKER_RECYCLE_AFTER_SECONDS > 0 and worker_uptime >= WORKER_RECYCLE_AFTER_SECONDS
                if reached_task_limit or reached_time_limit:
                    motivo = (
                        f"{tarefas_processadas} tarefas concluídas"
                        if reached_task_limit
                        else f"uptime de {worker_uptime}s"
                    )
                    logger.warning(f"[ROBÔ {thread_id}] Reciclagem automática acionada após {motivo}. Encerrando processo para renascer limpo.")
                    return
            
        except Exception as e:
            logger.error(f"[ROBÔ {thread_id}] Erro no loop principal: {e}")
            set_worker_state(thread_id, "error", message=str(e))
            time.sleep(5)

def auto_dispatcher():
    mode = "janela útil configurada" if AUTO_DISPATCH_RESPECT_WINDOW else "modo contínuo e preguiçoso"
    logger.info(f"🤖 [DISPATCHER] Ativado! Manutenção automática em {mode}.")
    time.sleep(10) 
    
    while True:
        try:
            agora_local = get_local_now()

            maybe_run_pre_dispatch_purge(agora_local)
            maybe_run_idle_purge()

            if get_redis().exists("lock:cooldown") or get_redis().exists("lock:infra_cooldown"):
                time.sleep(30)
                continue

            agora_utc = datetime.utcnow()

            if AUTO_DISPATCH_RESPECT_WINDOW and not is_auto_dispatch_window(agora_local):
                time.sleep(60)
                continue

            db = SessionLocal()
            try:
                contas = db.query(AccountBB).filter(
                    AccountBB.status.in_(['active', 'ativo', 'provisoria_recebida', 'termo_assinado'])
                ).order_by(AccountBB.id.asc()).all()

                menor_tempo_para_vencer = float(AUTO_DISPATCH_TARGET_AGE_MINUTES)
                tarefas_enfileiradas = 0
                
                for acc in contas:
                    if AUTO_DISPATCH_MAX_ENQUEUE_PER_CYCLE > 0 and tarefas_enfileiradas >= AUTO_DISPATCH_MAX_ENQUEUE_PER_CYCLE:
                        break

                    setor = "GERAL"
                    if acc.setores:
                        setores_list = [s for s in acc.setores.split('|') if s]
                        if setores_list: setor = setores_list[0]

                    precisa_renovar = False
                    refresh_threshold = max(1.0, float(AUTO_DISPATCH_TARGET_AGE_MINUTES) - float(AUTO_DISPATCH_REFRESH_MARGIN_MINUTES))
                    
                    # Lógica para saber quanto tempo de paz nós temos
                    if acc.cookie_payload and acc.last_login_at:
                        minutos_passados = (agora_utc - acc.last_login_at).total_seconds() / 60
                        tempo_restante = float(AUTO_DISPATCH_TARGET_AGE_MINUTES) - minutos_passados
                        
                        if tempo_restante < menor_tempo_para_vencer:
                            menor_tempo_para_vencer = tempo_restante
                            
                        if minutos_passados >= refresh_threshold:
                            precisa_renovar = True
                    elif AUTO_DISPATCH_INCLUDE_EMPTY:
                        precisa_renovar = True
                        menor_tempo_para_vencer = 0.0
                    
                    if precisa_renovar:
                        if get_account_backoff_seconds(acc.id) > 0:
                            continue
                        lock_key = f"lock:queue:{acc.id}"
                        if not get_redis().exists(lock_key):
                            get_redis().setex(lock_key, 600, "1")
                            
                            payload = json.dumps({"id": acc.id, "setor": setor, "auto": True})
                            get_redis().lpush("queue:login_requests", payload)
                            mark_system_activity()
                            logger.info(
                                f"🔄 [DISPATCHER] Conta {acc.login} ({setor}) enfileirada para manutenção preguiçosa "
                                f"(alvo {AUTO_DISPATCH_TARGET_AGE_MINUTES}m, margem {AUTO_DISPATCH_REFRESH_MARGIN_MINUTES}m)."
                            )
                            tarefas_enfileiradas += 1

                # =========================================================================
                # 🧹 EXPURGO DINÂMICO (Faxina Preventiva nas Brechas de Tempo)
                # =========================================================================
                if tarefas_enfileiradas == 0 and menor_tempo_para_vencer >= 5.0:
                    maybe_run_idle_purge()
                # =========================================================================

            finally:
                db.close()
                
            time.sleep(60)
        except Exception as e:
            logger.error(f"Erro no Auto-Dispatcher: {e}")
            time.sleep(60)

if __name__ == "__main__":
    logger.info("Limpando filas antigas e destravando status fantasmas...")
    try:
        r = get_redis()
        r.delete("queue:login_requests")
        r.delete("queue:priority_logins") 
        r.delete("lock:cooldown") 
        r.delete("lock:infra_cooldown")
        r.delete("lock:bb_door") 
        r.delete("lock:chrome_startup")
        r.delete("lock:purge_in_progress")
        r.delete("lock:idle_purge")
        r.set("metrics:captcha_consecutive_failures", 0) 
        r.set("metrics:infra_consecutive_failures", 0)
        mark_system_activity()
        for key in r.scan_iter("lock:queue:*"): r.delete(key)
        for key in r.scan_iter("cooldown:account:*"): r.delete(key)
        for key in r.scan_iter("maintenance:pre_dispatch_purge:*"): r.delete(key)
        for key in r.scan_iter("status:*"): r.delete(key)
        for key in r.scan_iter("heartbeat:*"): r.delete(key)
        for key in r.scan_iter("worker:state:*"): r.delete(key)
    except: pass
    
    logger.info(f"🚀 Iniciando a Frota OneLog com {MAX_WORKERS} Robôs em paralelo...")
    
    workers = []
    
    for i in range(MAX_WORKERS):
        p = multiprocessing.Process(target=worker_loop, args=(i + 1,), daemon=True)
        p.start()
        workers.append({"process": p, "id": i + 1})
        
    dispatcher = multiprocessing.Process(target=auto_dispatcher, daemon=True)
    dispatcher.start()
        
    # =========================================================================
    # 🐕 O CÃO DE GUARDA (Evoluído com Monitor Cardíaco)
    # =========================================================================
    while True:
        try:
            time.sleep(30) 
            
            if not dispatcher.is_alive():
                logger.error("🚨 ALERTA: Dispatcher morreu! Ressuscitando...")
                dispatcher = multiprocessing.Process(target=auto_dispatcher, daemon=True)
                dispatcher.start()
                
            for idx, w in enumerate(workers):
                r_id = w["id"]
                ressuscitar = False

                if not w["process"].is_alive():
                    logger.error(f"🚨 ALERTA: ROBÔ {r_id} morreu inesperadamente (Provável OOM).")
                    ressuscitar = True
                else:
                    # ❤️ Leitura do Eletrocardiograma (Deteção de Coma / Deadlock)
                    hb_str = get_redis().get(f"heartbeat:{r_id}")
                    if hb_str:
                        segundos_trabalhando = int(time.time()) - int(hb_str)
                        if segundos_trabalhando > WATCHDOG_STALE_SECONDS:
                            logger.error(f"🚨 ALERTA: ROBÔ {r_id} em COMA (Deadlock) há {segundos_trabalhando}s! Puxando o cabo da tomada da força...")
                            w["process"].kill() # Assassina o processo travado
                            activate_infra_cooldown(f"robô {r_id} travado por {segundos_trabalhando}s", seconds=min(INFRA_COOLDOWN_SECONDS, 180))
                            if not has_recent_other_heartbeats(exclude_thread_id=r_id):
                                faxina_global_de_emergencia()
                            else:
                                logger.warning("🧹 Expurgo global adiado para não derrubar sessões saudáveis de outros robôs.")
                            ressuscitar = True
                            
                if ressuscitar:
                    logger.info(f"🔄 Clonando um novo ROBÔ {r_id} saudável...")
                    new_p = multiprocessing.Process(target=worker_loop, args=(r_id,), daemon=True)
                    new_p.start()
                    workers[idx]["process"] = new_p
                    get_redis().delete(f"heartbeat:{r_id}")
                    set_worker_state(r_id, "starting", message="Reiniciando robô")
                    
        except KeyboardInterrupt:
            logger.info("Encerrando sistema...")
            break
        except Exception as e:
            logger.error(f"Erro no Cão de Guarda: {e}")

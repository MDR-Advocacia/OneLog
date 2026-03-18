import redis
import json
import os
import time
import multiprocessing
import signal
import psutil
import random
from datetime import datetime
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

# ====================================================================
# SISTEMA DE ROTAÇÃO DE PROXIES (PREPARAÇÃO PARA O MIKROTIK)
# ====================================================================
PROXY_ENV = os.getenv("PROXY_LIST", "socks5://206.42.43.192:45123")
PROXY_LIST = [p.strip() for p in PROXY_ENV.split(',') if p.strip()]

def get_random_proxy():
    if not PROXY_LIST:
        return None
    return random.choice(PROXY_LIST)

redis_client = None

def get_redis():
    global redis_client
    if redis_client is None:
        redis_client = redis.Redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)
    return redis_client

def update_status(setor, msg, concluido=False, erro=False, imagem=None, thread_id=None):
    status = {"mensagem": msg, "concluido": concluido, "erro": erro, "imagem": imagem}
    get_redis().set(f"status:{setor}", json.dumps(status))
    
    prefix = f"[ROBÔ {thread_id} | {setor}]" if thread_id else f"[{setor}]"
    logger.info(f"{prefix} {msg}")

def snapshot(sb, setor, nome_arquivo, thread_id=None):
    if not DEBUG_MODE: return None
    if not os.path.exists('shared'): os.makedirs('shared')
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{setor}_{nome_arquivo}_{ts}.png"
    sb.save_screenshot(os.path.join("shared", filename))
    img_url = f"{BASE_URL}/shared/{filename}"
    
    prefix = f"[ROBÔ {thread_id} | {setor}]" if thread_id else f"[{setor}]"
    logger.info(f"{prefix} 📸 Snapshot gerado: {img_url}")
    return img_url

def mata_fantasmas_do_chrome(pid_pai):
    try:
        pai = psutil.Process(pid_pai)
        filhos = pai.children(recursive=True)
        for filho in filhos:
            try:
                nome = filho.name().lower()
                if "chrome" in nome or "chromedriver" in nome or "xvfb" in nome:
                    filho.terminate()
                    filho.kill()
            except psutil.NoSuchProcess: pass
    except Exception: pass

def processar_login(account_id, setor_solicitado, thread_id):
    meu_pid = os.getpid()
    db = SessionLocal()
    hoje = datetime.utcnow().strftime('%Y-%m-%d')
    
    try:
        account = db.query(AccountBB).filter(AccountBB.id == int(account_id)).first()
        if not account: return

        # =======================================================================
        # 🛑 BLINDAGEM CONTRA TRABALHO DUPLICADO (Otimização de Servidor)
        # Se a conta já tem um cookie fresco (menos de 15 min), o robô aborta 
        # a tarefa imediatamente para não desperdiçar RAM e Cloudflare!
        # =======================================================================
        if account.cookie_payload and account.last_login_at:
            minutos_passados = (datetime.utcnow() - account.last_login_at).total_seconds() / 60
            if minutos_passados < 15:
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
            
            sb_instance = None 
            proxy_escolhido = get_random_proxy()
            
            try:
                logger.info(f"[ROBÔ {thread_id} | {setor}] IP/Proxy alocado para esta missão: {proxy_escolhido}")
                
                with SB(uc=True, test=True, headless=False, xvfb=True, proxy=proxy_escolhido, page_load_strategy="eager") as sb:
                    sb_instance = sb 
                    
                    logger.info(f"[ROBÔ {thread_id} | {setor}] Aguardando a Catraca do Banco liberar...")
                    
                    while not get_redis().set("lock:bb_door", "1", ex=60, nx=True):
                        time.sleep(3)
                    
                    try:
                        update_status(setor, f"Catraca liberada! Abrindo navegador (Tentativa {tentativa}/{max_tentativas_gerais})...", thread_id=thread_id)
                        
                        sb.open('https://loginweb.bb.com.br/favicon.ico')
                        sb.sleep(1)
                        
                        logger.info(f"[ROBÔ {thread_id} | {setor}] Executando Faxina Nuclear de Cookies e Cache...")
                        try:
                            sb.driver.execute_cdp_cmd('Network.clearBrowserCache', {})
                            sb.driver.execute_cdp_cmd('Network.clearBrowserCookies', {})
                        except Exception as cdp_e:
                            logger.warning(f"[ROBÔ {thread_id} | {setor}] Aviso na faxina CDP: {cdp_e}")
                            
                        sb.delete_all_cookies()
                        sb.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
                        try:
                            sb.execute_script("window.indexedDB.databases().then(dbs => dbs.forEach(db => window.indexedDB.deleteDatabase(db.name)))")
                        except: pass
                        
                        sb.open('https://loginweb.bb.com.br/sso/XUI/?realm=/paj&goto=https://juridico.bb.com.br/wfj#login')
                        sb.sleep(4)
                        
                        img = snapshot(sb, setor, f"01_inicio_T{tentativa}", thread_id=thread_id)
                        
                        update_status(setor, "Digitando usuário...", imagem=img, thread_id=thread_id)
                        sb.type("#idToken1", usuario)
                        sb.sleep(1)
                        sb.click("#loginButton_0")
                        
                        update_status(setor, "Analisando Captcha...", imagem=img, thread_id=thread_id)
                        sb.sleep(6)
                        img = snapshot(sb, setor, f"02_antes_captcha_T{tentativa}", thread_id=thread_id)
                        
                        captcha_container = "div.cf-turnstile"
                        
                        if sb.is_element_visible(captcha_container):
                            update_status(setor, "Cloudflare detectado. Aguardando estabilização...", imagem=img, thread_id=thread_id)
                            sb.sleep(4) 
                            
                            try:
                                sb.click(captcha_container) 
                                logger.info(f"[ROBÔ {thread_id} | {setor}] >>> Clique bruto no captcha realizado.")
                            except Exception as e:
                                logger.warning(f"[ROBÔ {thread_id} | {setor}] Aviso no clique bruto: {e}")
                            
                            update_status(setor, "Aguardando validação do clique...", thread_id=thread_id)
                            sb.sleep(5) 
                                
                            img = snapshot(sb, setor, f"03_pos_clique_T{tentativa}", thread_id=thread_id)
                        
                        update_status(setor, "Aguardando campo de senha...", imagem=img, thread_id=thread_id)
                        
                        # =======================================================================
                        # O DETECTOR DE ARMADILHAS DO CLOUDFLARE (POPUP)
                        # =======================================================================
                        try:
                            sb.wait_for_element("#idToken3", timeout=35)
                            logger.info(f"[ROBÔ {thread_id} | {setor}] >>> SUCESSO! Campo de senha apareceu!")
                        except Exception as wait_e:
                            logger.error(f"[ROBÔ {thread_id} | {setor}] 🚨 ARMADILHA DETECTADA! O campo de senha não carregou. O Cloudflare abriu um popup inútil ou bloqueou o fluxo.")
                            raise Exception(f"Armadilha Cloudflare: Popup inútil ou loop infinito bloqueou o campo de senha. Erro original: {wait_e}")
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
                            
                        account.cookie_payload = json.dumps(cookies_limpos)
                        account.last_login_at = datetime.utcnow()
                        account.user_agent_used = real_ua
                        db.commit()
                        update_status(setor, "Acesso concedido e salvo no Pool!", concluido=True, imagem=img, thread_id=thread_id)
                        
                        get_redis().set("metrics:captcha_consecutive_failures", 0)
                        get_redis().delete(f"lock:queue:{account_id}")
                        
                        try: sb.driver.quit() 
                        except: pass
                        mata_fantasmas_do_chrome(meu_pid)
                        return 
                        
                    else:
                        raise Exception("Timeout ao aguardar o portal jurídico carregar após a senha.")
                        
            except Exception as e:
                logger.warning(f"[ROBÔ {thread_id} | {setor}] Falha na tentativa {tentativa}: {e}")
                
                get_redis().delete("lock:bb_door")
                
                # TELEMETRIA: Registro de Falhas e Categorização Inteligente
                get_redis().hincrby(f"metrics:robot_error:{hoje}", f"ROBÔ {thread_id}", 1)
                
                err_msg = str(e).lower()
                is_infra_error = False
                
                if "errno 11" in err_msg or "resource temporarily unavailable" in err_msg or "-5" in err_msg or "thread" in err_msg or "not reachable" in err_msg:
                    motivo_falha = "Esgotamento de Recursos (OS/Docker)"
                    is_infra_error = True
                elif "armadilha cloudflare" in err_msg:
                    motivo_falha = "Bloqueio Cloudflare (Armadilha/Popup)"
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
                
                if sb_instance:
                     try:
                         img = snapshot(sb_instance, setor, f"erro_tentativa_{tentativa}", thread_id=thread_id)
                     except Exception:
                         img = None
                         
                     try: sb_instance.driver.quit() 
                     except: pass
                else: img = None
                
                mata_fantasmas_do_chrome(meu_pid)

                if tentativa == max_tentativas_gerais:
                    logger.error(f"[ROBÔ {thread_id} | {setor}] FALHA DEFINITIVA APÓS {max_tentativas_gerais} TENTATIVAS.")
                    update_status(setor, "Falha no processo. O Auto-Dispatcher tentará novamente mais tarde.", erro=True, imagem=img, thread_id=thread_id)
                    get_redis().delete(f"lock:queue:{account_id}")
                else:
                    update_status(setor, f"Sessão queimada. Reiniciando navegador do zero (Tentativa {tentativa+1})...", imagem=img, thread_id=thread_id)
                    time.sleep(3)
    finally:
        db.close()

def worker_loop(thread_id):
    logger.info(f"[ROBÔ {thread_id}] Em posição e aguardando missões...")
    
    while True:
        try:
            if get_redis().exists("lock:cooldown"):
                logger.info(f"[ROBÔ {thread_id}] 🛑 Modo fôlego ativo. Aguardando a poeira baixar...")
                time.sleep(20)
                continue

            task = get_redis().brpop(["queue:priority_logins", "queue:login_requests"], timeout=10)
            if not task: continue
                
            queue_name, task_data_str = task
            
            if get_redis().exists("lock:cooldown"):
                logger.warning(f"[ROBÔ {thread_id}] Fôlego ativado no meio do caminho! Devolvendo tarefa para a fila {queue_name}...")
                get_redis().rpush(queue_name, task_data_str)
                time.sleep(20)
                continue

            try:
                task_data = json.loads(task_data_str)
                account_id = task_data['id']
                setor = task_data['setor']
                is_priority = task_data.get('priority', False)
            except Exception:
                account_id = task_data_str
                setor = "GERAL"
                is_priority = False

            if is_priority:
                logger.info(f"[ROBÔ {thread_id} | {setor}] 🚨 EMERGÊNCIA VIP 🚨 Conta ID: {account_id} (Fura-fila acionado)")
            else:
                logger.info(f"[ROBÔ {thread_id} | {setor}] Nova tarefa capturada! Conta ID: {account_id}")
                
            processar_login(account_id, setor, thread_id)
            
        except Exception as e:
            logger.error(f"[ROBÔ {thread_id}] Erro no loop principal: {e}")
            time.sleep(5)

def auto_dispatcher():
    logger.info("🤖 [DISPATCHER] Ativado! As contas serão mantidas quentes 24/7.")
    time.sleep(10) 
    
    while True:
        try:
            if get_redis().exists("lock:cooldown"):
                time.sleep(30)
                continue

            db = SessionLocal()
            try:
                contas = db.query(AccountBB).filter(
                    AccountBB.status.in_(['active', 'ativo', 'provisoria_recebida', 'termo_assinado'])
                ).order_by(AccountBB.id.asc()).all()

                agora = datetime.utcnow()
                setores_processados = set() 
                
                for acc in contas:
                    setor = "GERAL"
                    if acc.setores:
                        setores_list = [s for s in acc.setores.split('|') if s]
                        if setores_list: setor = setores_list[0]
                    
                    if setor in setores_processados:
                        continue
                        
                    setores_processados.add(setor)

                    precisa_renovar = False
                    
                    if not acc.cookie_payload:
                        precisa_renovar = True
                    elif acc.last_login_at:
                        minutos_passados = (agora - acc.last_login_at).total_seconds() / 60
                        if minutos_passados >= 18:
                            precisa_renovar = True
                    
                    if precisa_renovar:
                        lock_key = f"lock:queue:{acc.id}"
                        if not get_redis().exists(lock_key):
                            get_redis().setex(lock_key, 600, "1")
                            
                            payload = json.dumps({"id": acc.id, "setor": setor, "auto": True})
                            get_redis().lpush("queue:login_requests", payload)
                            logger.info(f"🔄 [DISPATCHER] Conta {acc.login} ({setor}) enfileirada para pré-aquecimento (Ciclo 18m).")
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
        r.delete("lock:bb_door") 
        r.set("metrics:captcha_consecutive_failures", 0) 
        for key in r.scan_iter("lock:queue:*"): r.delete(key)
        for key in r.scan_iter("status:*"): r.delete(key)
    except: pass
    
    logger.info(f"🚀 Iniciando a Frota OneLog com {MAX_WORKERS} Robôs em paralelo...")
    
    workers = []
    
    for i in range(MAX_WORKERS):
        p = multiprocessing.Process(target=worker_loop, args=(i + 1,), daemon=True)
        p.start()
        workers.append({"process": p, "id": i + 1})
        
    dispatcher = multiprocessing.Process(target=auto_dispatcher, daemon=True)
    dispatcher.start()
        
    while True:
        try:
            time.sleep(30) 
            
            if not dispatcher.is_alive():
                logger.error("🚨 ALERTA: Dispatcher morreu! Ressuscitando...")
                dispatcher = multiprocessing.Process(target=auto_dispatcher, daemon=True)
                dispatcher.start()
                
            for idx, w in enumerate(workers):
                if not w["process"].is_alive():
                    r_id = w["id"]
                    logger.error(f"🚨 ALERTA: ROBÔ {r_id} morreu inesperadamente (Provável OOM). Ressuscitando clone...")
                    
                    new_p = multiprocessing.Process(target=worker_loop, args=(r_id,), daemon=True)
                    new_p.start()
                    workers[idx]["process"] = new_p
                    
        except KeyboardInterrupt:
            logger.info("Encerrando sistema...")
            break
        except Exception as e:
            logger.error(f"Erro no Cão de Guarda: {e}")
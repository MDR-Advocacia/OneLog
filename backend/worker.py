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
    
    logger.info(f"[ROBÔ {thread_id} | {setor_solicitado}] Aplicando Jitter aleatório para despistar balanceadores do BB...")
    time.sleep(random.uniform(2.0, 7.0))

    try:
        account = db.query(AccountBB).filter(AccountBB.id == int(account_id)).first()
        if not account: return

        setor = setor_solicitado
        usuario, senha = account.login, account.senha
        
        # TEXTO UX ATUALIZADO
        update_status(setor, "Iniciando ambiente seguro na nuvem...", thread_id=thread_id)
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
                        # TEXTO UX ATUALIZADO
                        update_status(setor, f"Estabelecendo rota segura (Tentativa {tentativa}/{max_tentativas_gerais})...", thread_id=thread_id)
                        sb.open('https://loginweb.bb.com.br/sso/XUI/?realm=/paj&goto=https://juridico.bb.com.br/wfj#login')
                        
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
                        
                        sb.refresh() 
                        sb.sleep(4)
                        
                        # TEXTO UX ATUALIZADO
                        update_status(setor, "Aplicando blindagem de rede...", imagem=img if 'img' in locals() else None, thread_id=thread_id)
                        
                        img = snapshot(sb, setor, f"01_inicio_T{tentativa}", thread_id=thread_id)
                        
                        # TEXTO UX ATUALIZADO
                        update_status(setor, "Inserindo identificação no portal...", imagem=img, thread_id=thread_id)
                        sb.type("#idToken1", usuario)
                        sb.sleep(1)
                        sb.click("#loginButton_0")
                        
                        # TEXTO UX ATUALIZADO
                        update_status(setor, "Analisando requisitos de segurança...", imagem=img, thread_id=thread_id)
                        sb.sleep(6)
                        img = snapshot(sb, setor, f"02_antes_captcha_T{tentativa}", thread_id=thread_id)
                        
                        captcha_container = "div.cf-turnstile"
                        
                        if sb.is_element_visible(captcha_container):
                            # TEXTO UX ATUALIZADO
                            update_status(setor, "Validando integridade da conexão...", imagem=img, thread_id=thread_id)
                            sb.sleep(2)
                            try:
                                sb.click(captcha_container) 
                                logger.info(f"[ROBÔ {thread_id} | {setor}] >>> Clique bruto no captcha realizado.")
                            except Exception as e:
                                logger.warning(f"[ROBÔ {thread_id} | {setor}] Aviso no clique bruto: {e}")
                                
                            img = snapshot(sb, setor, f"03_pos_clique_T{tentativa}", thread_id=thread_id)
                        
                        # TEXTO UX ATUALIZADO
                        update_status(setor, "Aguardando criptografia do portal...", imagem=img, thread_id=thread_id)
                        
                        sb.wait_for_element("#idToken3", timeout=35)
                        logger.info(f"[ROBÔ {thread_id} | {setor}] >>> SUCESSO! Campo de senha apareceu!")
                        
                    finally:
                        get_redis().delete("lock:bb_door")
                        logger.info(f"[ROBÔ {thread_id} | {setor}] Catraca liberada para o próximo robô da fila.")
                    
                    img = snapshot(sb, setor, f"04_senha_visivel_T{tentativa}", thread_id=thread_id)
                    
                    # TEXTO UX ATUALIZADO
                    update_status(setor, "Autenticando credenciais de acesso...", imagem=img, thread_id=thread_id)
                    sb.type("#idToken3", senha)
                    sb.sleep(1)
                    sb.click("input#loginButton_0[name='callback_4']")
                    
                    # TEXTO UX ATUALIZADO
                    update_status(setor, "Validando liberação do sistema...", imagem=img, thread_id=thread_id)
                    
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
                        
                        # TEXTO UX ATUALIZADO
                        update_status(setor, "Conexão segura estabelecida com sucesso!", concluido=True, imagem=img, thread_id=thread_id)
                        
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
                
                fail_count = get_redis().incr("metrics:captcha_consecutive_failures")
                logger.info(f"[ROBÔ {thread_id} | {setor}] Medidor de bloqueios Cloudflare: {fail_count}/6")
                
                if fail_count >= 6: 
                    logger.error(f"[ROBÔ {thread_id} | {setor}] 🚨 NÍVEL DE AMEAÇA MÁXIMO ATINGIDO NO CLOUDFLARE!")
                    logger.error(f"[ROBÔ {thread_id} | {setor}] Acionando protocolo de Fôlego de 3 minutos para toda a frota.")
                    get_redis().setex("lock:cooldown", 180, "true") 
                    get_redis().set("metrics:captcha_consecutive_failures", 0) 
                
                if sb_instance:
                     img = snapshot(sb_instance, setor, f"erro_tentativa_{tentativa}", thread_id=thread_id)
                     try: sb_instance.driver.quit() 
                     except: pass
                else: img = None
                
                mata_fantasmas_do_chrome(meu_pid)

                if tentativa == max_tentativas_gerais:
                    logger.error(f"[ROBÔ {thread_id} | {setor}] FALHA DEFINITIVA APÓS {max_tentativas_gerais} TENTATIVAS.")
                    # TEXTO UX ATUALIZADO
                    update_status(setor, "Falha na sincronização. O sistema tentará novamente em breve.", erro=True, imagem=img, thread_id=thread_id)
                    get_redis().delete(f"lock:queue:{account_id}")
                else:
                    # TEXTO UX ATUALIZADO
                    update_status(setor, f"Instabilidade na rede detectada. Reiniciando conexão segura (Tentativa {tentativa+1})...", imagem=img, thread_id=thread_id)
                    time.sleep(3)
    finally:
        db.close()

def worker_loop(thread_id):
    logger.info(f"[ROBÔ {thread_id}] Em posição e aguardando missões da extensão...")
    
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
                logger.info(f"[ROBÔ {thread_id} | {setor}] Nova tarefa capturada (Sob Demanda)! Conta ID: {account_id}")
                
            processar_login(account_id, setor, thread_id)
            
        except Exception as e:
            logger.error(f"[ROBÔ {thread_id}] Erro no loop principal: {e}")
            time.sleep(5)

def auto_dispatcher():
    """
    MODO FAXINEIRO: O Pré-aquecimento 24/7 foi desativado para proteger o servidor do Cloudflare.
    Agora ele apenas varre o banco a cada 10 minutos para ver se há lixo de memória para limpar.
    """
    logger.info("🤖 [DISPATCHER] Modo 'Pre-Heating' DESATIVADO. Atuando apenas como faxineiro de sistema (On-Demand Mode).")
    
    while True:
        try:
            time.sleep(600) # Roda a cada 10 minutos apenas para manutenção levíssima do sistema
            logger.info("🧹 [DISPATCHER] Verificando integridade das filas (Sistema ocioso)...")
            
            # Removemos a lógica que forçava os robôs a logar a cada 13 minutos.
            # O sistema agora depende EXCLUSIVAMENTE do 'Acessar Portal' da extensão 
            # e do Marcapasso (Heartbeat) de 20 minutos que roda no computador do usuário!
            
        except Exception as e:
            logger.error(f"Erro no Faxineiro: {e}")
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
    
    logger.info(f"🚀 Iniciando a Frota OneLog com {MAX_WORKERS} Robôs em paralelo (Modo Sob Demanda)...")
    
    workers = []
    
    for i in range(MAX_WORKERS):
        p = multiprocessing.Process(target=worker_loop, args=(i + 1,), daemon=True)
        p.start()
        workers.append({"process": p, "id": i + 1})
        
    dispatcher = multiprocessing.Process(target=auto_dispatcher, daemon=True)
    dispatcher.start()
        
    # =========================================================================
    # 🐕 O CÃO DE GUARDA (Watchdog de OOM)
    # =========================================================================
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
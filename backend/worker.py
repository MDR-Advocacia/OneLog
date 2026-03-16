import redis
import json
import os
import time
import multiprocessing
import signal
import psutil
import shutil
import random
from datetime import datetime
from seleniumbase import SB
import logging
from database import SessionLocal, AccountBB

# Configuração de Logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s - WORKER - %(message)s')
logger = logging.getLogger(__name__)

BASE_URL = "https://api-onelog.mdradvocacia.com"

# MODO TURBO CONTROLADO POR VARIÁVEL DE AMBIENTE (Padrão: True)
DEBUG_MODE = os.getenv("DEBUG_MODE", "True").lower() == "true"

# Define quantos robôs vão rodar ao mesmo tempo (Padrão: 3)
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "3"))

redis_client = None

def get_redis():
    global redis_client
    if redis_client is None:
        redis_client = redis.Redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)
    return redis_client

def update_status(setor, msg, concluido=False, erro=False, imagem=None):
    status = {"mensagem": msg, "concluido": concluido, "erro": erro, "imagem": imagem}
    get_redis().set(f"status:{setor}", json.dumps(status))
    logger.info(f"[{setor}] {msg}")

def snapshot(sb, setor, nome_arquivo):
    if not DEBUG_MODE: return None
    if not os.path.exists('shared'): os.makedirs('shared')
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{setor}_{nome_arquivo}_{ts}.png"
    sb.save_screenshot(os.path.join("shared", filename))
    img_url = f"{BASE_URL}/shared/{filename}"
    logger.info(f"📸 Snapshot gerado: {img_url}")
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
    
    # MÁGICA 1: Jitter - Atraso aleatório para desincronizar os robôs
    atraso = random.uniform(2.0, 7.0)
    logger.info(f"[ROBÔ {thread_id}] Aplicando Jitter de {atraso:.1f}s para despistar balanceadores do BB...")
    time.sleep(atraso)

    # MÁGICA 2: Isolamento genético! Cada robô nasce com uma pasta virgem.
    pasta_perfil = f"/tmp/onelog_profile_robo_{thread_id}"
    if os.path.exists(pasta_perfil):
        try: shutil.rmtree(pasta_perfil)
        except: pass

    try:
        account = db.query(AccountBB).filter(AccountBB.id == int(account_id)).first()
        if not account: return

        setor = setor_solicitado
        usuario, senha = account.login, account.senha
        
        update_status(setor, "Iniciando robô stealth...")
        max_tentativas_gerais = 3
        
        for tentativa in range(1, max_tentativas_gerais + 1):
            logger.info(f"[ROBÔ {thread_id}] === TENTATIVA {tentativa}/{max_tentativas_gerais} PARA {setor} ===")
            sb_instance = None 
            
            try:
                with SB(uc=True, test=True, headless=False, xvfb=True, proxy="socks5://206.42.43.192:45123", user_data_dir=pasta_perfil) as sb:
                    sb_instance = sb 
                    try: sb.set_window_size(1366, 768)
                    except: pass
                    
                    update_status(setor, f"Abrindo navegador (Tentativa {tentativa}/{max_tentativas_gerais})...")
                    sb.open('https://loginweb.bb.com.br/sso/XUI/?realm=/paj&goto=https://juridico.bb.com.br/wfj#login')
                    sb.sleep(3)
                    
                    img = snapshot(sb, setor, f"01_inicio_T{tentativa}")
                    update_status(setor, "Digitando usuário...", imagem=img)
                    sb.wait_for_element_visible("#idToken1", timeout=20)
                    sb.type("#idToken1", usuario)
                    sb.sleep(1.5)
                    sb.click("#loginButton_0")
                    
                    update_status(setor, "Analisando Captcha...", imagem=img)
                    sb.sleep(6)
                    img = snapshot(sb, setor, f"02_antes_captcha_T{tentativa}")
                    
                    captcha_container = "div.cf-turnstile"
                    if sb.is_element_visible(captcha_container):
                        update_status(setor, "Cloudflare detectado. Aplicando força bruta padrão...", imagem=img)
                        sb.sleep(2)
                        try:
                            sb.click(captcha_container)
                            logger.info(f"[ROBÔ {thread_id}] >>> Clique Padrão executado.")
                        except Exception as e:
                            logger.warning(f"[ROBÔ {thread_id}] Aviso no clique: {e}")
                            
                        img = snapshot(sb, setor, f"03_pos_clique_T{tentativa}")
                    
                    update_status(setor, "Aguardando campo de senha...", imagem=img)
                    sb.wait_for_element("#idToken3", timeout=45)
                    logger.info(f"[ROBÔ {thread_id}] >>> SUCESSO! Campo de senha apareceu!")
                    
                    img = snapshot(sb, setor, f"04_senha_visivel_T{tentativa}")
                    update_status(setor, "Digitando senha...", imagem=img)
                    sb.type("#idToken3", senha)
                    sb.sleep(1)
                    sb.click("input#loginButton_0[name='callback_4']")
                    update_status(setor, "Validando acesso...", imagem=img)
                    
                    max_retries_url = 15
                    logged_in = False
                    for _ in range(max_retries_url):
                        current_url = sb.get_current_url()
                        if "juridico.bb.com.br" in current_url and "loginweb" not in current_url:
                            logged_in = True
                            img = snapshot(sb, setor, f"05_sucesso_portal_T{tentativa}")
                            break
                        sb.sleep(4)
                    
                    if logged_in:
                        cookies = sb.driver.get_cookies()
                        COOKIES_BLOQUEADOS = ["PD-S-SESSION-ID", "JSESSIONID", "cf_clearance", "__cf_bm"]
                        cookies_limpos = []
                        for cookie in cookies:
                            nome_cookie = cookie['name']
                            if nome_cookie in COOKIES_BLOQUEADOS or nome_cookie.startswith('TS01') or 'BIGipServer' in nome_cookie:
                                continue
                            cookies_limpos.append(cookie)
                            
                        try: real_ua = sb.execute_script("return navigator.userAgent;")
                        except: real_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                            
                        account.cookie_payload = json.dumps(cookies_limpos)
                        account.last_login_at = datetime.utcnow()
                        account.user_agent_used = real_ua
                        db.commit()
                        update_status(setor, "Acesso concedido e salvo no Pool!", concluido=True, imagem=img)
                        
                        get_redis().set("metrics:captcha_consecutive_failures", 0)
                        get_redis().delete(f"lock:queue:{account_id}")
                        
                        try: sb.driver.quit() 
                        except: pass
                        mata_fantasmas_do_chrome(meu_pid)
                        return 
                        
                    else:
                        raise Exception("Timeout ao aguardar o portal jurídico carregar após a senha.")
                        
            except Exception as e:
                logger.warning(f"[ROBÔ {thread_id}] Falha na tentativa {tentativa}: {e}")
                
                fail_count = get_redis().incr("metrics:captcha_consecutive_failures")
                logger.info(f"[ROBÔ {thread_id}] Medidor de bloqueios Cloudflare: {fail_count}/6")
                
                if fail_count >= 6: 
                    logger.error(f"[ROBÔ {thread_id}] 🚨 NÍVEL DE AMEAÇA MÁXIMO! Acionando Fôlego de 3 minutos.")
                    get_redis().setex("lock:cooldown", 180, "true") 
                    get_redis().set("metrics:captcha_consecutive_failures", 0) 
                
                if sb_instance:
                     img = snapshot(sb_instance, setor, f"erro_tentativa_{tentativa}")
                     try: sb_instance.driver.quit() 
                     except: pass
                else: img = None
                mata_fantasmas_do_chrome(meu_pid)

                if tentativa == max_tentativas_gerais:
                    logger.error(f"[ROBÔ {thread_id}] FALHA DEFINITIVA APÓS {max_tentativas_gerais} TENTATIVAS.")
                    update_status(setor, "Falha no processo. O Auto-Dispatcher tentará novamente mais tarde.", erro=True, imagem=img)
                    get_redis().delete(f"lock:queue:{account_id}")
                else:
                    update_status(setor, f"Sessão queimada. Reiniciando navegador (Tentativa {tentativa+1})...", imagem=img)
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

            # MÁGICA 3: O ROBÔ ESCUTA A PISTA VIP E A PISTA LENTA AO MESMO TEMPO
            # Se tiver algo na "priority_logins", ele pega primeiro.
            task = get_redis().brpop(["queue:priority_logins", "queue:login_requests"], timeout=10)
            if not task: continue
                
            queue_name, task_data_str = task
            
            if get_redis().exists("lock:cooldown"):
                logger.warning(f"[ROBÔ {thread_id}] Fôlego ativado no meio do caminho! Devolvendo tarefa para {queue_name}...")
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
                logger.info(f"[ROBÔ {thread_id}] 🚨 EMERGÊNCIA VIP 🚨 Conta ID: {account_id} (Fura-fila acionado)")
            else:
                logger.info(f"[ROBÔ {thread_id}] Nova tarefa de manutenção! Conta ID: {account_id} | Setor: {setor}")
                
            processar_login(account_id, setor, thread_id)
            
        except Exception as e:
            logger.error(f"[ROBÔ {thread_id}] Erro no loop principal: {e}")
            time.sleep(5)


def auto_dispatcher():
    """
    O Gerente do Pool: Roda a cada 60 segundos em background.
    Ele varre o banco de dados e joga na PISTA LENTA (login_requests).
    """
    logger.info("🤖 Auto-Dispatcher: Ativado! As contas serão mantidas quentes 24/7.")
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
                ).all()

                agora = datetime.utcnow()
                for acc in contas:
                    precisa_renovar = False
                    
                    if not acc.cookie_payload:
                        precisa_renovar = True
                    elif acc.last_login_at:
                        minutos_passados = (agora - acc.last_login_at).total_seconds() / 60
                        if minutos_passados >= 13:
                            precisa_renovar = True
                    
                    if precisa_renovar:
                        lock_key = f"lock:queue:{acc.id}"
                        if not get_redis().exists(lock_key):
                            get_redis().setex(lock_key, 300, "1")
                            
                            setor = "GERAL"
                            if acc.setores:
                                setores_list = [s for s in acc.setores.split('|') if s]
                                if setores_list: setor = setores_list[0]
                                    
                            # Envia para a pista lenta (queue:login_requests)
                            payload = json.dumps({"id": acc.id, "setor": setor, "auto": True})
                            get_redis().lpush("queue:login_requests", payload)
                            logger.info(f"🔄 Auto-Dispatcher: Conta {acc.login} enfileirada para pré-aquecimento.")
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
        r.set("metrics:captcha_consecutive_failures", 0) 
        for key in r.scan_iter("lock:queue:*"): r.delete(key)
        for key in r.scan_iter("status:*"): r.delete(key)
    except: pass
    
    logger.info(f"🚀 Iniciando a Frota OneLog com {MAX_WORKERS} Robôs em paralelo...")
    
    processes = []
    
    for i in range(MAX_WORKERS):
        p = multiprocessing.Process(target=worker_loop, args=(i + 1,), daemon=True)
        p.start()
        processes.append(p)
        
    dispatcher = multiprocessing.Process(target=auto_dispatcher, daemon=True)
    dispatcher.start()
    processes.append(dispatcher)
        
    for p in processes:
        p.join()
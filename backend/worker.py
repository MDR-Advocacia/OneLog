import redis
import json
import os
import time
import multiprocessing
import signal
import psutil
from datetime import datetime
from seleniumbase import SB
import logging
from database import SessionLocal, AccountBB

# Configuração de Logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s - WORKER - %(message)s')
logger = logging.getLogger(__name__)

BASE_URL = "https://api-onelog.mdradvocacia.com"

# MODO TURBO
DEBUG_MODE = os.getenv("DEBUG_MODE", "False").lower() == "true"

# Define quantos robôs vão rodar ao mesmo tempo (Padrão: 3)
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "3"))

# Variável global vazia. Cada "Processo Clone" criará a sua própria conexão segura com o Redis.
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
    sb.save_screenshot(os.path.join("static", filename))
    img_url = f"{BASE_URL}/static/{filename}"
    logger.info(f"📸 Snapshot gerado: {img_url}")
    return img_url

def mata_fantasmas_do_chrome(pid_pai):
    """
    Caça-Fantasmas: Varre os processos filhos a partir do PID do Python atual
    e garante que qualquer instância desgarrada de chrome ou chromedriver morra.
    """
    try:
        pai = psutil.Process(pid_pai)
        filhos = pai.children(recursive=True)
        for filho in filhos:
            try:
                nome = filho.name().lower()
                if "chrome" in nome or "chromedriver" in nome or "xvfb" in nome:
                    # Tenta matar educadamente e depois à força
                    filho.terminate()
                    filho.kill()
            except psutil.NoSuchProcess:
                pass
    except Exception:
        pass


def processar_login(account_id, setor_solicitado, thread_id):
    """Função isolada para garantir que o banco de dados seja seguro por thread/processo"""
    meu_pid = os.getpid() # Pega a ID do processo Python desta thread
    db = SessionLocal()
    try:
        account = db.query(AccountBB).filter(AccountBB.id == int(account_id)).first()
        if not account: return

        setor = setor_solicitado
        usuario, senha = account.login, account.senha
        
        update_status(setor, "Iniciando robô stealth...")
        max_tentativas_gerais = 3
        
        for tentativa in range(1, max_tentativas_gerais + 1):
            logger.info(f"[ROBÔ {thread_id}] === TENTATIVA {tentativa}/{max_tentativas_gerais} PARA {setor} ===")
            
            sb_instance = None # Controle local para o "Caça-Fantasmas"
            
            try:
                # REMOVEMOS O AGENT: O UC (Undetected Chromedriver) precisa rodar puro para enganar o Cloudflare!
                # O page_load_strategy="eager" ajuda a parar de depender de carregamentos pesados para injetar comandos
                with SB(uc=True, test=True, headless=False, xvfb=True, proxy="socks5://206.42.43.192:45123", page_load_strategy="eager") as sb:
                    sb_instance = sb 
                    
                    update_status(setor, f"Abrindo navegador (Tentativa {tentativa}/{max_tentativas_gerais})...")
                    sb.open('https://loginweb.bb.com.br/sso/XUI/?realm=/paj&goto=https://juridico.bb.com.br/wfj#login')
                    
                    logger.info(f"[ROBÔ {thread_id}] Executando faxina de cookies e cache...")
                    sb.delete_all_cookies()
                    sb.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
                    sb.refresh() 
                    sb.sleep(4)
                    
                    img = snapshot(sb, setor, f"01_inicio_T{tentativa}")
                    
                    update_status(setor, "Digitando usuário...", imagem=img)
                    sb.type("#idToken1", usuario)
                    sb.sleep(1)
                    sb.click("#loginButton_0")
                    
                    update_status(setor, "Analisando Captcha...", imagem=img)
                    sb.sleep(6)
                    img = snapshot(sb, setor, f"02_antes_captcha_T{tentativa}")
                    
                    captcha_container = "div.cf-turnstile"
                    
                    if sb.is_element_visible(captcha_container):
                        update_status(setor, "Cloudflare detectado. Clique único...", imagem=img)
                        sb.sleep(2)
                        try:
                            sb.click(captcha_container) 
                            logger.info(f"[ROBÔ {thread_id}] >>> Clique no captcha realizado.")
                        except Exception as e:
                            logger.warning(f"[ROBÔ {thread_id}] Aviso no clique: {e}")
                            
                        img = snapshot(sb, setor, f"03_pos_clique_T{tentativa}")
                    
                    update_status(setor, "Aguardando campo de senha...", imagem=img)
                    
                    sb.wait_for_element("#idToken3", timeout=35)
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
                        
                        # 🚨 A LISTA NEGRA CONTINUA AQUI FIRME E FORTE 🚨
                        COOKIES_BLOQUEADOS = ["PD-S-SESSION-ID", "JSESSIONID", "cf_clearance", "__cf_bm"]
                        
                        cookies_limpos = []
                        for cookie in cookies:
                            nome_cookie = cookie['name']
                            
                            # Filtra os cookies atrelados a IP e Balanceadores
                            if nome_cookie in COOKIES_BLOQUEADOS or nome_cookie.startswith('TS01') or 'BIGipServer' in nome_cookie:
                                logger.info(f"[ROBÔ {thread_id}] Filtro Ativado: Destruindo cookie tóxico/IP -> {nome_cookie}")
                                continue
                                
                            cookies_limpos.append(cookie)
                            
                        # Pegamos o UA real do servidor já que rodamos sem disfarces
                        try: real_ua = sb.execute_script("return navigator.userAgent;")
                        except: real_ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                            
                        account.cookie_payload = json.dumps(cookies_limpos)
                        account.last_login_at = datetime.utcnow()
                        account.user_agent_used = real_ua
                        db.commit()
                        update_status(setor, "Acesso concedido e salvo no Pool!", concluido=True, imagem=img)
                        
                        # --- SUCESSO! Zera o contador de falhas da frota ---
                        get_redis().set("metrics:captcha_consecutive_failures", 0)
                        
                        try: sb.driver.quit() 
                        except: pass
                        mata_fantasmas_do_chrome(meu_pid)
                        return 
                        
                    else:
                        raise Exception("Timeout ao aguardar o portal jurídico carregar após a senha.")
                        
            except Exception as e:
                logger.warning(f"[ROBÔ {thread_id}] Falha na tentativa {tentativa}: {e}")
                
                # --- SISTEMA DE RESILIÊNCIA (FÔLEGO) ---
                fail_count = get_redis().incr("metrics:captcha_consecutive_failures")
                logger.info(f"[ROBÔ {thread_id}] Medidor de bloqueios Cloudflare: {fail_count}/6")
                
                if fail_count >= 6: # Se a frota acumular 6 falhas consecutivas
                    logger.error(f"[ROBÔ {thread_id}] 🚨 NÍVEL DE AMEAÇA MÁXIMO ATINGIDO NO CLOUDFLARE!")
                    logger.error(f"[ROBÔ {thread_id}] Acionando protocolo de Fôlego de 3 minutos para toda a frota.")
                    get_redis().setex("lock:cooldown", 180, "true") # Cria uma trava temporária de 3 minutos
                    get_redis().set("metrics:captcha_consecutive_failures", 0) # Reseta para não travar de novo instantaneamente
                # ----------------------------------------
                
                if sb_instance:
                     img = snapshot(sb_instance, setor, f"erro_tentativa_{tentativa}")
                     try: sb_instance.driver.quit() 
                     except: pass
                else: img = None
                
                # Limpeza forçada da memória mesmo dando erro
                mata_fantasmas_do_chrome(meu_pid)

                if tentativa == max_tentativas_gerais:
                    logger.error(f"[ROBÔ {thread_id}] FALHA DEFINITIVA APÓS {max_tentativas_gerais} TENTATIVAS.")
                    update_status(setor, "Falha no processo. Tente acessar novamente.", erro=True, imagem=img)
                else:
                    update_status(setor, f"Sessão queimada. Reiniciando navegador do zero (Tentativa {tentativa+1})...", imagem=img)
                    time.sleep(3)
    finally:
        db.close()


def worker_loop(thread_id):
    """O loop infinito executado via MULTIPROCESSING (Isolado e Seguro)"""
    logger.info(f"[ROBÔ {thread_id}] Em posição e aguardando missões...")
    
    while True:
        try:
            # 1. Verifica se o Fôlego está ativado ANTES de buscar missões
            if get_redis().exists("lock:cooldown"):
                logger.info(f"[ROBÔ {thread_id}] 🛑 Modo fôlego ativo. Aguardando a poeira baixar...")
                time.sleep(20)
                continue

            # 2. Busca com timeout de 10s (para não travar eternamente se a fila estiver vazia e não checar o fôlego)
            task = get_redis().brpop("queue:login_requests", timeout=10)
            if not task:
                continue
                
            _, task_data_str = task
            
            # 3. Verifica NOVAMENTE logo após pegar a missão (evita que ele pegue missão durante o Fôlego de outro robô)
            if get_redis().exists("lock:cooldown"):
                logger.warning(f"[ROBÔ {thread_id}] Fôlego ativado no meio do caminho! Devolvendo tarefa para a fila...")
                get_redis().rpush("queue:login_requests", task_data_str)
                time.sleep(20)
                continue

            try:
                task_data = json.loads(task_data_str)
                account_id = task_data['id']
                setor = task_data['setor']
            except Exception:
                account_id = task_data_str
                setor = "GERAL"

            logger.info(f"[ROBÔ {thread_id}] Nova tarefa capturada! Conta ID: {account_id} para Setor: {setor}")
            processar_login(account_id, setor, thread_id)
            
        except Exception as e:
            logger.error(f"[ROBÔ {thread_id}] Erro no loop principal: {e}")
            time.sleep(5)


if __name__ == "__main__":
    logger.info("Limpando fila antiga e destravando status fantasmas...")
    try:
        r = get_redis()
        r.delete("queue:login_requests")
        r.delete("lock:cooldown") # Destrava o fôlego caso o container seja reiniciado
        r.set("metrics:captcha_consecutive_failures", 0) # Zera as métricas
        for key in r.scan_iter("status:*"):
            r.delete(key)
    except:
        pass
    
    logger.info(f"🚀 Iniciando a Frota OneLog com {MAX_WORKERS} Robôs em paralelo (Multiprocessing)...")
    
    processes = []
    for i in range(MAX_WORKERS):
        p = multiprocessing.Process(target=worker_loop, args=(i + 1,), daemon=True)
        p.start()
        processes.append(p)
        
    for p in processes:
        p.join()
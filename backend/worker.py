import redis
import json
import os
import time
from datetime import datetime
from seleniumbase import SB
import logging
from database import SessionLocal, AccountBB

logging.basicConfig(level=logging.INFO, format='%(asctime)s - WORKER - %(message)s')
logger = logging.getLogger(__name__)

redis_client = redis.Redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)
BASE_URL = "https://api-onelog.mdradvocacia.com"

# MODO TURBO
DEBUG_MODE = os.getenv("DEBUG_MODE", "False").lower() == "true"

def update_status(setor, msg, concluido=False, erro=False, imagem=None):
    status = {"mensagem": msg, "concluido": concluido, "erro": erro, "imagem": imagem}
    redis_client.set(f"status:{setor}", json.dumps(status))
    logger.info(f"[{setor}] {msg}")

def snapshot(sb, setor, nome_arquivo):
    if not DEBUG_MODE: return None
    
    if not os.path.exists('static'): os.makedirs('static')
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{setor}_{nome_arquivo}_{ts}.png"
    sb.save_screenshot(os.path.join("static", filename))
    img_url = f"{BASE_URL}/static/{filename}"
    logger.info(f"📸 Snapshot gerado: {img_url}")
    return img_url

def processar_login(account_id):
    db = SessionLocal()
    try:
        account = db.query(AccountBB).filter(AccountBB.id == int(account_id)).first()
        if not account or not account.sector: return

        setor = account.sector.nome
        usuario, senha = account.login, account.senha
        
        update_status(setor, "Iniciando robô...")
        max_tentativas_gerais = 3
        
        for tentativa in range(1, max_tentativas_gerais + 1):
            logger.info(f"=== INICIANDO TENTATIVA {tentativa}/{max_tentativas_gerais} PARA {setor} ===")
            
            with SB(uc=True, test=True, headless=False, xvfb=True, proxy="socks5://206.42.43.192:45123") as sb:
                try:
                    update_status(setor, f"Abrindo navegador (Tentativa {tentativa}/{max_tentativas_gerais})...")
                    sb.open('https://loginweb.bb.com.br/sso/XUI/?realm=/paj&goto=https://juridico.bb.com.br/wfj#login')
                    
                    # A MÁGICA: Limpa a sujeira do Cloudflare antes de começar a agir
                    logger.info("Executando faxina de cookies e cache (Esterilização da sessão)...")
                    sb.delete_all_cookies()
                    sb.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
                    sb.refresh() # Recarrega a página com a reputação zerada
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
                    
                    # Se o Captcha aparecer, clicamos APENAS UMA VEZ.
                    # Se falhar, é melhor fechar o navegador todo do que clicar de novo e piorar o shadowban.
                    if sb.is_element_visible(captcha_container):
                        update_status(setor, "Cloudflare detectado. Clique único e decisivo...", imagem=img)
                        sb.sleep(2)
                        try:
                            sb.click(captcha_container) 
                            logger.info(">>> Clique no captcha realizado.")
                        except Exception as e:
                            logger.warning(f"Aviso no clique: {e}")
                            
                        img = snapshot(sb, setor, f"03_pos_clique_T{tentativa}")
                    
                    update_status(setor, "Aguardando campo de senha...", imagem=img)
                    
                    # Dá 35 segundos. Se a senha não aparecer, o Try/Except estoura, fecha o Chrome e tenta do zero.
                    sb.wait_for_element("#idToken3", timeout=35)
                    logger.info(">>> SUCESSO! Campo de senha apareceu!")
                    
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
                        try: real_ua = sb.execute_script("return navigator.userAgent;")
                        except: real_ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                        
                        account.cookie_payload = json.dumps(cookies)
                        account.last_login_at = datetime.now()
                        account.user_agent_used = real_ua
                        db.commit()
                        update_status(setor, "Acesso concedido e salvo no Pool!", concluido=True, imagem=img)
                        return # SUCESSO!
                    else:
                        raise Exception("Timeout ao aguardar o portal jurídico carregar após a senha.")
                        
                except Exception as e:
                    logger.warning(f"Falha na tentativa {tentativa}: {e}")
                    img = snapshot(sb, setor, f"erro_tentativa_{tentativa}")
                    
                    if tentativa == max_tentativas_gerais:
                        logger.error(f"FALHA DEFINITIVA APÓS {max_tentativas_gerais} TENTATIVAS.")
                        update_status(setor, "Falha no processo. Tente acessar novamente.", erro=True, imagem=img)
                    else:
                        update_status(setor, f"Sessão queimada. Reiniciando navegador do zero (Tentativa {tentativa+1})...", imagem=img)
                        time.sleep(3)
    finally:
        db.close()

if __name__ == "__main__":
    logger.info("Limpando fila antiga e destravando status fantasmas...")
    redis_client.delete("queue:login_requests")
    for key in redis_client.scan_iter("status:*"):
        redis_client.delete(key)
    
    logger.info("Worker Enterprise iniciado. Aguardando missão...")
    while True:
        try:
            _, account_id = redis_client.brpop("queue:login_requests")
            logger.info(f"Nova tarefa recebida! Processando Conta ID: {account_id}")
            processar_login(account_id)
        except Exception as e:
            logger.error(f"Erro no loop principal: {e}")
            time.sleep(5)
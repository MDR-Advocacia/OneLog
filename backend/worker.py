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

def update_status(setor, msg, concluido=False, erro=False, imagem=None):
    status = {"mensagem": msg, "concluido": concluido, "erro": erro, "imagem": imagem}
    redis_client.set(f"status:{setor}", json.dumps(status))
    logger.info(f"[{setor}] {msg}")

def snapshot(sb, setor, nome_arquivo):
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
        
        with SB(uc=True, test=True, headless=False, xvfb=True, proxy="socks5://206.42.43.192:45123") as sb:
            try:
                update_status(setor, "Abrindo navegador...")
                sb.open('https://loginweb.bb.com.br/sso/XUI/?realm=/paj&goto=https://juridico.bb.com.br/wfj#login')
                img = snapshot(sb, setor, "01_inicio")
                sb.sleep(5)
                
                update_status(setor, "Digitando usuário...", imagem=img)
                sb.type("#idToken1", usuario)
                sb.sleep(1)
                sb.click("#loginButton_0")
                
                update_status(setor, "Analisando Captcha...", imagem=img)
                sb.sleep(6)
                img = snapshot(sb, setor, "02_antes_captcha")
                
                captcha_container = "div.cf-turnstile"
                if sb.is_element_visible(captcha_container):
                    update_status(setor, "Cloudflare detectado. Interagindo...", imagem=img)
                    try:
                        sb.click(captcha_container)
                    except Exception: pass
                    img = snapshot(sb, setor, "03_pos_clique")
                    update_status(setor, "Aguardando liberação automática do BB...", imagem=img)

                update_status(setor, "Aguardando campo de senha...", imagem=img)
                sb.wait_for_element("#idToken3", timeout=45)
                img = snapshot(sb, setor, "04_senha_visivel")
                
                update_status(setor, "Digitando senha...", imagem=img)
                sb.type("#idToken3", senha)
                sb.sleep(1)
                sb.click("input#loginButton_0[name='callback_4']")
                update_status(setor, "Validando acesso...", imagem=img)
                
                max_retries = 15
                logged_in = False
                for _ in range(max_retries):
                    current_url = sb.get_current_url()
                    if "juridico.bb.com.br" in current_url and "loginweb" not in current_url:
                        logged_in = True
                        img = snapshot(sb, setor, "05_sucesso_portal")
                        break
                    sb.sleep(4)
                
                if logged_in:
                    cookies = sb.driver.get_cookies()
                    try: real_ua = sb.execute_script("return navigator.userAgent;")
                    except: real_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                    
                    account.cookie_payload = json.dumps(cookies)
                    account.last_login_at = datetime.now()
                    account.user_agent_used = real_ua
                    db.commit()
                    update_status(setor, "Acesso concedido e salvo no Pool!", concluido=True, imagem=img)
                else:
                    raise Exception("Timeout ao aguardar o portal jurídico carregar após a senha.")
            except Exception as e:
                logger.error(f"FALHA: {e}")
                img = snapshot(sb, setor, "99_erro_fatal")
                update_status(setor, f"Falha no processo: {str(e)[:50]}", erro=True, imagem=img)
    finally:
        db.close()

if __name__ == "__main__":
    # O SEGREDO CONTRA O LOOP ETERNO: Limpa a fila antes de começar a ouvir
    logger.info("Limpando fila antiga para evitar loop de missões duplicadas...")
    redis_client.delete("queue:login_requests")
    
    logger.info("Worker Enterprise iniciado. Aguardando missão...")
    while True:
        try:
            # Fica em 0% de CPU dormindo até receber um novo chamado (só 1 por vez)
            _, account_id = redis_client.brpop("queue:login_requests")
            logger.info(f"Nova tarefa recebida! Processando Conta ID: {account_id}")
            processar_login(account_id)
        except Exception as e:
            logger.error(f"Erro no loop principal: {e}")
            time.sleep(5)
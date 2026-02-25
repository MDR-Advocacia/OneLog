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

# A STRING DOURADA (Camuflagem para bater com a extensão)
USER_AGENT_DOURADO = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

BASE_URL = "http://api-onelog.mdradvocacia.com"

def update_status(setor, msg, concluido=False, erro=False, imagem=None):
    status = {"mensagem": msg, "concluido": concluido, "erro": erro, "imagem": imagem}
    redis_client.set(f"status:{setor}", json.dumps(status))
    logger.info(f"[{setor}] {msg}")

def snapshot(sb, setor, nome_arquivo):
    if not os.path.exists('static'): os.makedirs('static')
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{setor}_{nome_arquivo}_{ts}.png"
    sb.save_screenshot(os.path.join("static", filename))
    return f"{BASE_URL}/static/{filename}"

def processar_login(account_id):
    db = SessionLocal()
    try:
        # Busca a conta real no banco de dados
        account = db.query(AccountBB).filter(AccountBB.id == int(account_id)).first()
        if not account or not account.sector:
            logger.error(f"Conta ID {account_id} não encontrada ou sem setor associado.")
            return

        setor = account.sector.nome
        usuario = account.login
        senha = account.senha
        
        update_status(setor, "Iniciando robô...")
        
        with SB(uc=True, test=True, headless=True, proxy="socks5://206.42.43.192:45123", agent=USER_AGENT_DOURADO) as sb:
            try:
                update_status(setor, "Acessando Portal...")
                sb.open('https://loginweb.bb.com.br/sso/XUI/?realm=/paj&goto=https://juridico.bb.com.br/wfj#login')
                img = snapshot(sb, setor, "1_inicio")
                update_status(setor, "Acessando Portal...", imagem=img)
                sb.sleep(5)
                
                sb.type("#idToken1", usuario)
                sb.sleep(1) # Pausa idêntica ao app.py
                sb.click("#loginButton_0")
                
                update_status(setor, "Verificando Cloudflare...", imagem=img)
                sb.sleep(6) # Ajustado para 6s como no original
                
                captcha_container = "div.cf-turnstile"
                if sb.is_element_visible(captcha_container):
                    update_status(setor, "Resolvendo Captcha (Lógica Original)...", imagem=img)
                    try:
                        # Usando exatamente a função que funcionou no app.py (sem hover)
                        sb.uc_click(captcha_container)
                        logger.info(">>> CLIQUE V5 REALIZADO.")
                        img = snapshot(sb, setor, "3_pos_clique")
                        sb.sleep(20) # Aguardo obrigatório de 20s
                    except Exception as e:
                        logger.warning(f"Erro no clique: {e}. Tentando fallback...")
                        sb.click(captcha_container)
                
                # 4. Verificação de Token
                token = sb.get_attribute("#clientScriptOutputData", "value")
                if token:
                    logger.info("Token OK. Forçando submit...")
                    sb.execute_script('document.querySelector("input[type=submit]").click();')
                    sb.sleep(5)
                
                update_status(setor, "Aguardando campo de senha...")
                sb.wait_for_element("#idToken3", timeout=30)
                
                update_status(setor, "Autenticando...")
                sb.type("#idToken3", senha)
                sb.sleep(1) # Pausa idêntica ao app.py
                sb.click("input#loginButton_0[name='callback_4']")
                
                # Loop de Validação idêntico ao app.py (15 tentativas de 4s)
                max_retries = 15
                for _ in range(max_retries):
                    if "juridico.bb.com.br" in sb.get_current_url() and "loginweb" not in sb.get_current_url():
                        break
                    sb.sleep(4)
                    
                cookies = sb.driver.get_cookies()
                
                # SUCESSO! Salva a sessão no Banco de Dados (Cookie Pool)
                account.cookie_payload = json.dumps(cookies)
                account.last_login_at = datetime.now()
                account.user_agent_used = USER_AGENT_DOURADO
                db.commit()
                
                img = snapshot(sb, setor, "9_sucesso")
                update_status(setor, "Sessão capturada e salva no Pool!", concluido=True, imagem=img)
                
            except Exception as e:
                img = snapshot(sb, setor, "erro_fatal")
                update_status(setor, f"Falha no processo: {str(e)[:50]}", erro=True, imagem=img)
    finally:
        db.close()

if __name__ == "__main__":
    logger.info("Worker Enterprise iniciado. Aguardando fila 'queue:login_requests'...")
    while True:
        try:
            # Escuta na fila correta e pega o ID da conta enviado pela API
            _, account_id = redis_client.brpop("queue:login_requests")
            logger.info(f"Nova tarefa recebida! Processando Conta ID: {account_id}")
            processar_login(account_id)
        except Exception as e:
            logger.error(f"Erro no loop principal: {e}")
            time.sleep(5)
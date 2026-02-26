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
    """Tira print e retorna a URL pública para o frontend ver"""
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
        if not account or not account.sector:
            logger.error(f"Conta ID {account_id} não encontrada ou sem setor associado.")
            return

        setor = account.sector.nome
        usuario = account.login
        senha = account.senha
        
        update_status(setor, "Iniciando robô...")
        
        # xvfb=True cria o monitor virtual. headless=False garante que o navegador renderize.
        # IMPORTANTE: REMOVIDO o agent=USER_AGENT_DOURADO. O UC Mode fará a camuflagem correta com o Linux!
        with SB(uc=True, test=True, headless=False, xvfb=True, proxy="socks5://206.42.43.192:45123") as sb:
            try:
                logger.info(f">>> [INÍCIO] Login BB para {setor} - Lógica V5 (Original) + Prints.")
                
                # 1. Início
                update_status(setor, "Abrindo navegador...")
                sb.open('https://loginweb.bb.com.br/sso/XUI/?realm=/paj&goto=https://juridico.bb.com.br/wfj#login')
                img = snapshot(sb, setor, "01_inicio")
                update_status(setor, "Navegador aberto.", imagem=img)
                sb.sleep(5)
                
                # 2. Usuário
                update_status(setor, "Digitando usuário...", imagem=img)
                sb.type("#idToken1", usuario)
                sb.sleep(1)
                sb.click("#loginButton_0")
                
                # 3. Cloudflare
                update_status(setor, "Analisando Captcha...", imagem=img)
                logger.info("Aguardando Cloudflare...")
                sb.sleep(6)
                img = snapshot(sb, setor, "02_antes_captcha")
                update_status(setor, "Verificando desafio...", imagem=img)
                
                captcha_container = "div.cf-turnstile"
                if sb.is_element_visible(captcha_container):
                    update_status(setor, "Tentando clique no desafio...", imagem=img)
                    logger.info("Captcha visível. Resolvendo problema de compatibilidade...")
                    try:
                        # Método oficial do SeleniumBase 4.24+ para burlar o Cloudflare Turnstile
                        sb.uc_gui_click_captcha()
                        logger.info(">>> CLIQUE REALIZADO COM SUCESSO (uc_gui_click_captcha).")
                    except Exception as e:
                        logger.warning(f"Erro no método principal: {e}. Tentando fallback...")
                        try:
                            sb.uc_click(captcha_container)
                        except:
                            sb.click(captcha_container)
                        
                    img = snapshot(sb, setor, "03_pos_clique")
                    update_status(setor, "Clique efetuado, aguardando validação...", imagem=img)
                    sb.sleep(20)
                
                img = snapshot(sb, setor, "04_esperando_token")
                update_status(setor, "Processando validação passiva...", imagem=img)

                # 4. Verificação de Token
                token = sb.get_attribute("#clientScriptOutputData", "value")
                if token:
                    logger.info("Token OK. Forçando submit...")
                    sb.execute_script('document.querySelector("input[type=submit]").click();')
                    sb.sleep(5)
                
                # 5. Senha
                update_status(setor, "Aguardando campo de senha...", imagem=img)
                img = snapshot(sb, setor, "05_aguardando_senha")
                
                sb.wait_for_element("#idToken3", timeout=30)
                logger.info(">>> SUCESSO! Campo de senha apareceu!")
                img = snapshot(sb, setor, "06_senha_visivel")
                
                update_status(setor, "Digitando senha...", imagem=img)
                sb.type("#idToken3", senha)
                sb.sleep(1)
                sb.click("input#loginButton_0[name='callback_4']")
                
                update_status(setor, "Validando acesso...", imagem=img)
                
                # Validação
                max_retries = 15
                logged_in = False
                for _ in range(max_retries):
                    current_url = sb.get_current_url()
                    if "juridico.bb.com.br" in current_url and "loginweb" not in current_url:
                        logged_in = True
                        img = snapshot(sb, setor, "07_sucesso_portal")
                        break
                    sb.sleep(4)
                
                if logged_in:
                    cookies = sb.driver.get_cookies()
                    
                    # Extrai o User-Agent REAL que passou pelo Cloudflare no servidor
                    try:
                        real_ua = sb.execute_script("return navigator.userAgent;")
                    except:
                        real_ua = USER_AGENT_DOURADO
                    
                    # Salva a sessão no Banco de Dados (Cookie Pool)
                    account.cookie_payload = json.dumps(cookies)
                    account.last_login_at = datetime.now()
                    account.user_agent_used = real_ua
                    db.commit()
                    
                    update_status(setor, "Acesso concedido e salvo no Pool!", concluido=True, imagem=img)
                else:
                    raise Exception("Timeout ao aguardar redirecionamento após a senha.")
                
            except Exception as e:
                logger.error(f"FALHA: {e}")
                img = snapshot(sb, setor, "99_erro_fatal")
                update_status(setor, f"Falha no processo: {str(e)[:50]}", erro=True, imagem=img)
    finally:
        db.close()

if __name__ == "__main__":
    logger.info("Worker Enterprise iniciado (Modo XVFB). Aguardando fila 'queue:login_requests'...")
    while True:
        try:
            _, account_id = redis_client.brpop("queue:login_requests")
            logger.info(f"Nova tarefa recebida! Processando Conta ID: {account_id}")
            processar_login(account_id)
        except Exception as e:
            logger.error(f"Erro no loop principal: {e}")
            time.sleep(5)
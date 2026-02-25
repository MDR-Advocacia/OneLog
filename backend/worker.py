import redis
import json
import os
import time
from datetime import datetime
from seleniumbase import SB
import logging
from database import SessionLocal, AccountBB

# Configuração de Logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s - WORKER - %(message)s')
logger = logging.getLogger(__name__)

# Conexão com o Redis
redis_client = redis.Redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)

# A STRING DOURADA (A Máscara que a Extensão também vai usar)
USER_AGENT_DOURADO = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

def update_status(setor_nome, msg, concluido=False, erro=False):
    """Atualiza o Redis para que o Frontend mostre o progresso em tempo real."""
    status = {"mensagem": msg, "concluido": concluido, "erro": erro}
    redis_client.set(f"status:{setor_nome}", json.dumps(status))
    logger.info(f"[{setor_nome}] {msg}")

def processar_login(account_id):
    """Lógica Core: Acessa o BB e extrai a sessão (Baseada na V5 validada do app.py)."""
    db = SessionLocal()
    try:
        account = db.query(AccountBB).filter(AccountBB.id == int(account_id)).first()
        if not account or not account.sector:
            logger.error(f"Conta ID {account_id} não encontrada ou sem setor.")
            return

        setor_nome = account.sector.nome
        usuario = account.login
        senha = account.senha
        
        update_status(setor_nome, "Abrindo navegador seguro...")
        
        # Iniciando com a configuração idêntica ao app.py (UC=True)
        # Headless=True para rodar no servidor sem interface gráfica
        with SB(uc=True, test=True, headless=True, proxy="socks5://206.42.43.192:45123", agent=USER_AGENT_DOURADO) as sb:
            try:
                # 1. Início do Acesso
                update_status(setor_nome, "Acessando o Portal do BB...")
                sb.open('https://loginweb.bb.com.br/sso/XUI/?realm=/paj&goto=https://juridico.bb.com.br/wfj#login')
                sb.sleep(5)
                
                # 2. Identificação do Usuário
                update_status(setor_nome, "Digitando usuário...")
                sb.type("#idToken1", usuario)
                sb.sleep(1)
                sb.click("#loginButton_0")
                
                # 3. Tratamento de Cloudflare / Captcha
                update_status(setor_nome, "Analisando Captcha...")
                sb.sleep(6) # Tempo de análise da lógica original
                
                captcha_container = "div.cf-turnstile"
                if sb.is_element_visible(captcha_container):
                    update_status(setor_nome, "Resolvendo desafio de segurança...")
                    try:
                        # Utiliza uc_click para simulação humana no captcha
                        sb.uc_click(captcha_container)
                        logger.info(">>> CLIQUE V5 REALIZADO.")
                        sb.sleep(20) # Aguardo obrigatório pós-clique conforme app.py
                    except Exception as e:
                        logger.warning(f"Erro no uc_click: {e}. Tentando clique normal.")
                        sb.click(captcha_container)
                
                # 4. Verificação de Token de Segurança (Lógica V5 persistente)
                # Verifica se o script de segurança do banco gerou o token necessário
                token = sb.get_attribute("#clientScriptOutputData", "value")
                if token:
                    logger.info("Token identificado. Disparando submit via script.")
                    sb.execute_script('document.querySelector("input[type=submit]").click();')
                    sb.sleep(5)

                # 5. Entrada da Senha
                update_status(setor_nome, "Aguardando campo de senha...")
                # Garante que a página redirecionou e o campo de senha está disponível
                sb.wait_for_element("#idToken3", timeout=30)
                
                update_status(setor_nome, "Autenticando credenciais...")
                sb.type("#idToken3", senha)
                sb.sleep(1)
                sb.click("input#loginButton_0[name='callback_4']")
                
                update_status(setor_nome, "Validando acesso ao sistema...")
                
                # 6. Validação do Sucesso do Login
                logged_in = False
                # Loop de verificação de URL para garantir o redirecionamento final
                for _ in range(15):
                    current_url = sb.get_current_url()
                    if "juridico.bb.com.br" in current_url and "loginweb" not in current_url:
                        logged_in = True
                        break
                    sb.sleep(4)
                
                if logged_in:
                    # Captura os cookies da sessão autenticada
                    cookies = sb.driver.get_cookies()
                    
                    # Salva a sessão no Banco de Dados (Cookie Pool)
                    account.cookie_payload = json.dumps(cookies)
                    account.last_login_at = datetime.now()
                    account.user_agent_used = USER_AGENT_DOURADO
                    db.commit()
                    
                    update_status(setor_nome, "Sessão renovada com sucesso e pronta no Pool!", concluido=True)
                else:
                    raise Exception("Não foi possível confirmar o login no tempo limite (timeout).")
                
            except Exception as e:
                update_status(setor_nome, f"Erro no login: {str(e)[:50]}", erro=True)
                logger.error(f"Erro no processamento da conta {account_id}: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    logger.info("🤖 Worker Operacional (Lógica V5 - Sem Prints). Aguardando missões...")
    while True:
        try:
            # blpop bloqueia o loop até que um trabalho entre na fila do Redis
            queue_name, account_id = redis_client.blpop("queue:login_requests")
            logger.info(f"Nova missão recebida! Processando Conta ID: {account_id}")
            processar_login(account_id)
        except Exception as e:
            logger.error(f"Erro crítico no loop do Worker: {e}")
            time.sleep(5) # Delay de segurança para retry
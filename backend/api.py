from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import redis
import json
import os
import time
from datetime import datetime
from database import SessionLocal, Sector, AccountBB, init_db, seed_db
from ad_integration import autenticar_e_obter_setor
from functools import wraps

app = Flask(__name__)
CORS(app)

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "mudar-urgente")
redis_client = redis.Redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.headers.get("X-Admin-Token")
        if not token or token != ADMIN_TOKEN:
            return jsonify({"erro": "Acesso negado: Chave administrativa inválida."}), 403
        return f(*args, **kwargs)
    return decorated_function

def inicializar_sistema():
    tentativas = 10
    while tentativas > 0:
        try:
            init_db()
            seed_db()
            print("✅ Banco de Dados conectado!")
            return True
        except Exception as e:
            print(f"⚠️ Aguardando banco... {e}")
            tentativas -= 1
            time.sleep(5)
    return False

# --- ROTAS WEB (PAINEL E IMAGENS) ---
@app.route('/admin', methods=['GET', 'POST'])
def admin_panel():
    return send_from_directory('/app', 'admin.html')

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('/app/static', filename)

# --- ROTAS DE OPERAÇÃO (EXTENSÃO) ---
@app.route('/api/zerocore/status', methods=['GET'])
def get_status():
    setor_nome = request.args.get('setor')
    if not setor_nome: return jsonify({"mensagem": "Setor ausente."}), 400
    status_str = redis_client.get(f"status:{setor_nome}")
    return jsonify(json.loads(status_str)) if status_str else jsonify({"mensagem": "Aguardando..."})

@app.route('/api/zerocore/login', methods=['POST'])
def request_login():
    data = request.get_json(silent=True) or {}
    username, password = data.get('username'), data.get('password')
    
    if not username or not password:
        return jsonify({"status": "erro", "mensagem": "Usuário e senha são obrigatórios."}), 400

    # 1. Autentica no AD (Segurança de Rede)
    ad_result = autenticar_e_obter_setor(username, password)
    if ad_result['status'] == 'erro': return jsonify(ad_result), 401

    setor_nome = ad_result['setor']
    db = SessionLocal()
    try:
        sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
        if not sector:
            sector = Sector(nome=setor_nome)
            db.add(sector)
            db.commit()
            db.refresh(sector)
            
        account = db.query(AccountBB).filter(AccountBB.sector_id == sector.id, AccountBB.status == 'active').first()
        if not account:
            return jsonify({"status": "erro", "mensagem": f"Setor {setor_nome} sem conta vinculada."}), 403

        # 2. Verifica Pool de Cookies (ECONOMIZA PROCESSAMENTO AQUI)
        if account.cookie_payload and account.last_login_at:
            if (datetime.now() - account.last_login_at).total_seconds() / 60 < 20:
                return jsonify({
                    "status": "sucesso", "setor": setor_nome,
                    "cookies": json.loads(account.cookie_payload),
                    "url": "https://juridico.bb.com.br/wfj"
                })
        
        # 3. TRAVA DO REDIS (Evita o Loop Eterno)
        # Se chegou aqui, precisa de login novo. Mas já tem alguém fazendo?
        status_str = redis_client.get(f"status:{setor_nome}")
        if status_str:
            current_status = json.loads(status_str)
            # Se a missão anterior não terminou nem deu erro, o robô está trabalhando neste exato segundo.
            if not current_status.get("concluido") and not current_status.get("erro"):
                return jsonify({
                    "status": "queued", 
                    "setor": setor_nome, 
                    "mensagem": "O robô já está trabalhando nesta conta. Aguarde..."
                })

        # 4. Aciona Robô (Apenas 1 por setor)
        redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": "Iniciando robô...", "concluido": False, "erro": False}))
        redis_client.lpush("queue:login_requests", account.id)
        
        return jsonify({"status": "queued", "setor": setor_nome})
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": f"Erro interno na API: {str(e)}"}), 500
    finally:
        db.close()

@app.route('/api/zerocore/renew', methods=['POST'])
def renew_session():
    """Aciona a fila silenciosamente a pedido do background.js (Marcapasso)"""
    setor_nome = request.args.get('setor')
    if not setor_nome: return jsonify({"status": "erro", "mensagem": "Setor não informado."}), 400
    
    db = SessionLocal()
    try:
        sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
        if sector:
            account = db.query(AccountBB).filter(AccountBB.sector_id == sector.id).first()
            if account:
                status_str = redis_client.get(f"status:{setor_nome}")
                if status_str:
                    current_status = json.loads(status_str)
                    if not current_status.get("concluido") and not current_status.get("erro"):
                        return jsonify({"status": "queued", "mensagem": "Robô já em execução."})

                redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": "Renovação de Marcapasso...", "concluido": False, "erro": False}))
                redis_client.lpush("queue:login_requests", account.id)
                return jsonify({"status": "queued"})
    finally:
        db.close()
    return jsonify({"status": "erro"}), 404

@app.route('/api/zerocore/session', methods=['GET'])
def get_session():
    """Recupera apenas os cookies (Usado pelo background.js para evitar crash)"""
    setor_nome = request.args.get('setor')
    if not setor_nome: return jsonify({"status": "erro"}), 400
    
    db = SessionLocal()
    try:
        sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
        if sector:
            account = db.query(AccountBB).filter(AccountBB.sector_id == sector.id).first()
            if account and account.cookie_payload:
                return jsonify({
                    "status": "sucesso",
                    "cookies": json.loads(account.cookie_payload)
                })
    finally:
        db.close()
    return jsonify({"status": "erro"}), 404

# --- ROTAS ADMINISTRATIVAS ---
@app.route('/api/admin/sectors', methods=['GET'])
@admin_required
def admin_list_sectors():
    db = SessionLocal()
    try:
        sectors = db.query(Sector).all()
        result = [{"id": s.id, "nome": s.nome} for s in sectors]
        return jsonify(result)
    except Exception as e:
        return jsonify({"erro": f"Erro interno: {str(e)}"}), 500
    finally:
        db.close()

@app.route('/api/admin/configure_account', methods=['POST'])
@admin_required
def admin_configure_account():
    data = request.get_json(silent=True) or {}
    setor_nome, bb_login, bb_senha = data.get('setor'), data.get('login'), data.get('senha')

    if not all([setor_nome, bb_login, bb_senha]):
        return jsonify({"erro": "Dados incompletos. Envie setor, login e senha."}), 400

    db = SessionLocal()
    try:
        sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
        if not sector:
            sector = Sector(nome=setor_nome); db.add(sector); db.commit(); db.refresh(sector)

        account = db.query(AccountBB).filter(AccountBB.login == bb_login).first()
        if not account: account = db.query(AccountBB).filter(AccountBB.sector_id == sector.id).first()
        if not account: account = AccountBB(); db.add(account)

        account.login, account.senha, account.sector_id, account.status = bb_login, bb_senha, sector.id, "active"
        db.commit()
        return jsonify({"mensagem": f"Conta vinculada ao setor {setor_nome} com sucesso!"})
    except Exception as e:
        db.rollback()
        return jsonify({"erro": f"Erro no banco de dados: {str(e)}"}), 500
    finally:
        db.close()

@app.route('/api/zerocore/reset', methods=['POST'])
def api_reset():
    redis_client.delete(f"status:{request.args.get('setor', 'GERAL')}")
    return jsonify({"status": "resetado"})

if __name__ == '__main__':
    if inicializar_sistema():
        app.run(host='0.0.0.0', port=5000)
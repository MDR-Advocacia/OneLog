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

# Configurações de Segurança
# Defina uma chave forte no Coolify em ADMIN_TOKEN. Se não definir, o padrão é 'mudar-urgente'
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "mudar-urgente")

# Conexão com o Redis
redis_client = redis.Redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)

def admin_required(f):
    """Decorator para proteger rotas administrativas"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.headers.get("X-Admin-Token")
        if not token or token != ADMIN_TOKEN:
            return jsonify({"erro": "Acesso negado: Chave administrativa inválida ou ausente."}), 403
        return f(*args, **kwargs)
    return decorated_function

def inicializar_sistema():
    """Tenta inicializar o banco de dados com retentativas"""
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

# --- ROTAS DE OPERAÇÃO (EXTENSÃO) ---

@app.route('/api/zerocore/status', methods=['GET'])
def get_status():
    setor_nome = request.args.get('setor')
    if not setor_nome: return jsonify({"mensagem": "Setor ausente."}), 400
    status_str = redis_client.get(f"status:{setor_nome}")
    return jsonify(json.loads(status_str)) if status_str else jsonify({"mensagem": "Aguardando..."})

@app.route('/api/zerocore/login', methods=['POST'])
def request_login():
    data = request.json
    username, password = data.get('username'), data.get('password')
    
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

        # 2. Verifica Pool de Cookies
        if account.cookie_payload and account.last_login_at:
            if (datetime.now() - account.last_login_at).total_seconds() / 60 < 20:
                return jsonify({
                    "status": "sucesso", "setor": setor_nome,
                    "cookies": json.loads(account.cookie_payload),
                    "url": "https://juridico.bb.com.br/wfj"
                })
        
        # 3. Aciona Robô
        redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": "Iniciando robô...", "concluido": False}))
        redis_client.lpush("queue:login_requests", account.id)
        return jsonify({"status": "queued", "setor": setor_nome})
    finally:
        db.close()

# --- ROTAS ADMINISTRATIVAS (PROTEGIDAS) ---

@app.route('/api/admin/sectors', methods=['GET'])
@admin_required
def admin_list_sectors():
    db = SessionLocal()
    sectors = db.query(Sector).all()
    result = [{"id": s.id, "nome": s.nome} for s in sectors]
    db.close()
    return jsonify(result)

@app.route('/api/admin/configure_account', methods=['POST'])
@admin_required
def admin_configure_account():
    data = request.json
    setor_nome, bb_login, bb_senha = data.get('setor'), data.get('login'), data.get('senha')

    db = SessionLocal()
    try:
        sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
        if not sector:
            sector = Sector(nome=setor_nome); db.add(sector); db.commit(); db.refresh(sector)

        account = db.query(AccountBB).filter(AccountBB.sector_id == sector.id).first()
        if not account:
            account = AccountBB(sector_id=sector.id); db.add(account)

        account.login, account.senha, account.status = bb_login, bb_senha, "active"
        db.commit()
        return jsonify({"mensagem": f"Conta {bb_login} vinculada ao setor {setor_nome}!"})
    finally:
        db.close()

@app.route('/api/zerocore/reset', methods=['POST'])
def api_reset():
    redis_client.delete(f"status:{request.args.get('setor', 'GERAL')}")
    return jsonify({"status": "resetado"})

if __name__ == '__main__':
    if not os.path.exists('static'): os.makedirs('static')
    if inicializar_sistema():
        app.run(host='0.0.0.0', port=5000)
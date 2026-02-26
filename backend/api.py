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

# --- ROTAS DA EXTENSÃO ---

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

    ad_result = autenticar_e_obter_setor(username, password)
    if ad_result['status'] == 'erro': return jsonify(ad_result), 401

    setor_nome = ad_result['setor']
    db = SessionLocal()
    try:
        sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
        if not sector:
            sector = Sector(nome=setor_nome); db.add(sector); db.commit(); db.refresh(sector)
            
        account = db.query(AccountBB).filter(AccountBB.sector_id == sector.id, AccountBB.status == 'active').first()
        if not account:
            return jsonify({"status": "erro", "mensagem": f"Setor {setor_nome} sem conta vinculada."}), 403

        if account.cookie_payload and account.last_login_at:
            if (datetime.now() - account.last_login_at).total_seconds() / 60 < 20:
                return jsonify({
                    "status": "sucesso", "setor": setor_nome,
                    "cookies": json.loads(account.cookie_payload),
                    "url": "https://juridico.bb.com.br/wfj"
                })
        
        redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": "Iniciando robô...", "concluido": False}))
        redis_client.lpush("queue:login_requests", account.id)
        return jsonify({"status": "queued", "setor": setor_nome})
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": f"Erro interno na API: {str(e)}"}), 500
    finally:
        db.close()

# --- ROTAS WEB (PAINEL E IMAGENS) ---

@app.route('/admin', methods=['GET', 'POST'])
def admin_panel():
    """Rota para exibir o painel HTML"""
    return send_from_directory('/app', 'admin.html')

@app.route('/static/<path:filename>')
def serve_static(filename):
    """Rota para servir as imagens (Prints) do robô"""
    return send_from_directory('/app/static', filename)

# --- ROTAS ADMINISTRATIVAS ---

@app.route('/api/admin/sectors', methods=['GET'])
@admin_required
def admin_list_sectors():
    db = SessionLocal()
    try:
        sectors = db.query(Sector).all()
        return jsonify([{"id": s.id, "nome": s.nome} for s in sectors])
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
    finally:
        db.close()

@app.route('/api/admin/configure_account', methods=['POST'])
@admin_required
def admin_configure_account():
    data = request.get_json(silent=True) or {}
    setor_nome, bb_login, bb_senha = data.get('setor'), data.get('login'), data.get('senha')

    if not all([setor_nome, bb_login, bb_senha]):
        return jsonify({"erro": "Dados incompletos."}), 400

    db = SessionLocal()
    try:
        sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
        if not sector:
            sector = Sector(nome=setor_nome); db.add(sector); db.commit(); db.refresh(sector)

        account = db.query(AccountBB).filter(AccountBB.login == bb_login).first()
        if not account:
            account = db.query(AccountBB).filter(AccountBB.sector_id == sector.id).first()
        if not account:
            account = AccountBB(); db.add(account)

        account.login, account.senha, account.sector_id, account.status = bb_login, bb_senha, sector.id, "active"
        db.commit()
        return jsonify({"mensagem": f"Conta {bb_login} vinculada ao setor {setor_nome} com sucesso!"})
    except Exception as e:
        db.rollback()
        return jsonify({"erro": str(e)}), 500
    finally:
        db.close()

@app.route('/api/zerocore/reset', methods=['POST'])
def api_reset():
    redis_client.delete(f"status:{request.args.get('setor', 'GERAL')}")
    return jsonify({"status": "resetado"})

if __name__ == '__main__':
    # Garante que a pasta static existe na inicialização da API
    if not os.path.exists('static'): os.makedirs('static')
    
    if inicializar_sistema():
        app.run(host='0.0.0.0', port=5000)
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import redis
import json
import os
import time
from datetime import datetime
from database import SessionLocal, Sector, AccountBB, init_db, seed_db
from ad_integration import autenticar_e_obter_setor, listar_ous_bb_ad
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
            return jsonify({"erro": "Acesso negado: Chave administrativa inválida ou ausente."}), 403
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

# --- ROTAS DE PÁGINAS WEB ---
@app.route('/admin')
@app.route('/admin.html')
def serve_admin():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(base_dir, 'admin.html')

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

    ad_result = autenticar_e_obter_setor(username, password)
    if ad_result['status'] == 'erro': return jsonify(ad_result), 401

    setor_nome = ad_result['setor']
    redis_client.incr('metrics:logins_solicitados')
    
    db = SessionLocal()
    try:
        sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
        if not sector:
            sector = Sector(nome=setor_nome)
            db.add(sector)
            db.commit()
            db.refresh(sector)
            
        account = db.query(AccountBB).filter(AccountBB.sector_id == sector.id, AccountBB.status == 'active').order_by(AccountBB.id.asc()).first()
        
        if not account:
            return jsonify({"status": "erro", "mensagem": f"Setor {setor_nome} sem conta vinculada ou ativa."}), 403

        if account.cookie_payload and account.last_login_at:
            if (datetime.now() - account.last_login_at).total_seconds() / 60 < 20:
                redis_client.incr('metrics:cookies_injetados')
                return jsonify({
                    "status": "sucesso", "setor": setor_nome,
                    "cookies": json.loads(account.cookie_payload),
                    "url": "https://juridico.bb.com.br/wfj"
                })
        
        redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": "Iniciando robô...", "concluido": False}))
        redis_client.lpush("queue:login_requests", account.id)
        redis_client.incr('metrics:robos_executados')
        
        return jsonify({"status": "queued", "setor": setor_nome})
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": f"Erro interno na API: {str(e)}"}), 500
    finally:
        db.close()

@app.route('/api/zerocore/renew', methods=['POST'])
def renew_session():
    data = request.get_json(silent=True) or {}
    username = data.get('username')
    password = data.get('password')
    setor_nome = data.get('setor') or request.args.get('setor')

    if not setor_nome:
        return jsonify({"status": "erro", "mensagem": "Setor ausente."}), 400

    if username and password:
        ad_result = autenticar_e_obter_setor(username, password)
        if ad_result['status'] == 'erro':
            return jsonify({"status": "unauthorized", "mensagem": "Credenciais AD inválidas."}), 401

    db = SessionLocal()
    try:
        sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
        if sector:
            account = db.query(AccountBB).filter(AccountBB.sector_id == sector.id, AccountBB.status == 'active').order_by(AccountBB.id.asc()).first()
            if account:
                status_str = redis_client.get(f"status:{setor_nome}")
                if status_str:
                    current_status = json.loads(status_str)
                    if not current_status.get("concluido") and not current_status.get("erro"):
                        return jsonify({"status": "queued", "mensagem": "Robô já em execução."})

                redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": "Renovação de Marcapasso...", "concluido": False, "erro": False}))
                redis_client.lpush("queue:login_requests", account.id)
                redis_client.incr('metrics:robos_executados')
                return jsonify({"status": "queued"})
    finally:
        db.close()
    return jsonify({"status": "erro"}), 404

@app.route('/api/zerocore/session', methods=['GET', 'POST'])
def get_session():
    data = request.get_json(silent=True) or {}
    username = data.get('username')
    password = data.get('password')
    setor_nome = data.get('setor') or request.args.get('setor')

    if not setor_nome:
        return jsonify({"status": "erro", "mensagem": "Setor ausente."}), 400

    if username and password:
        ad_result = autenticar_e_obter_setor(username, password)
        if ad_result['status'] == 'erro':
            return jsonify({"status": "unauthorized", "mensagem": "Credenciais AD inválidas."}), 401
    
    db = SessionLocal()
    try:
        sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
        if sector:
            account = db.query(AccountBB).filter(AccountBB.sector_id == sector.id, AccountBB.status == 'active').order_by(AccountBB.id.asc()).first()
            if account and account.cookie_payload:
                return jsonify({
                    "status": "sucesso",
                    "cookies": json.loads(account.cookie_payload)
                })
    finally:
        db.close()
    return jsonify({"status": "erro"}), 404


# --- ROTAS ADMINISTRATIVAS E DASHBOARD ---

@app.route('/api/admin/ad_sectors', methods=['GET'])
@admin_required
def admin_list_ad_sectors():
    """Retorna todas as OUs do AD + OUs já registradas no banco (Garante lista preenchida)"""
    ad_ous = listar_ous_bb_ad()

    db = SessionLocal()
    try:
        db_sectors = [s.nome for s in db.query(Sector).all()]
    finally:
        db.close()

    # Une as duas listas sem duplicatas e ordena alfabeticamente
    todos_setores = sorted(list(set(ad_ous + db_sectors)))

    # Fallback caso o AD falhe e o banco esteja zerado
    if not todos_setores:
        todos_setores = ["BB_Acordos", "BB_Civel", "BB_Trabalhista", "GERAL"]

    return jsonify(todos_setores)

@app.route('/api/admin/dashboard_stats', methods=['GET'])
@admin_required
def admin_dashboard_stats():
    db = SessionLocal()
    try:
        total_accounts = db.query(AccountBB).count()
        active_accounts = db.query(AccountBB).filter(AccountBB.status == 'active').count()
        
        queue_size = redis_client.llen("queue:login_requests")
        
        logins_solicitados = int(redis_client.get('metrics:logins_solicitados') or 0)
        cookies_injetados = int(redis_client.get('metrics:cookies_injetados') or 0)
        robos_executados = int(redis_client.get('metrics:robos_executados') or 0)
        
        economia_pct = 0
        if logins_solicitados > 0:
            economia_pct = round((cookies_injetados / logins_solicitados) * 100, 1)

        return jsonify({
            "active_accounts": active_accounts,
            "total_accounts": total_accounts,
            "queue_size": queue_size,
            "logins_solicitados": logins_solicitados,
            "cookies_injetados": cookies_injetados,
            "robos_executados": robos_executados,
            "economia_pct": economia_pct
        })
    finally:
        db.close()

@app.route('/api/admin/sectors', methods=['GET'])
@admin_required
def admin_list_sectors():
    db = SessionLocal()
    try:
        sectors = db.query(Sector).all()
        return jsonify([{"id": s.id, "nome": s.nome} for s in sectors])
    finally:
        db.close()

@app.route('/api/admin/configure_account', methods=['POST'])
@admin_required
def admin_configure_account():
    data = request.get_json(silent=True) or {}
    setor_nome = data.get('setor')
    bb_login = data.get('login')
    bb_senha = data.get('senha')

    if not all([setor_nome, bb_login, bb_senha]):
        return jsonify({"erro": "Dados incompletos."}), 400

    db = SessionLocal()
    try:
        sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
        if not sector:
            sector = Sector(nome=setor_nome)
            db.add(sector)
            db.commit()
            db.refresh(sector)

        account = db.query(AccountBB).filter(AccountBB.login == bb_login).first()
        if not account:
            account = db.query(AccountBB).filter(AccountBB.sector_id == sector.id).first()
        if not account:
            account = AccountBB()
            db.add(account)

        account.login = bb_login
        account.senha = bb_senha
        account.sector_id = sector.id
        account.status = "active"
        
        db.commit()
        return jsonify({"mensagem": "Sucesso!"})
    except Exception as e:
        db.rollback() 
        return jsonify({"erro": f"Erro BD: {str(e)}"}), 500
    finally:
        db.close()

@app.route('/api/admin/accounts', methods=['GET'])
@admin_required
def admin_list_accounts():
    db = SessionLocal()
    try:
        accounts = db.query(AccountBB).all()
        result = []
        for acc in accounts:
            sector_nome = acc.sector.nome if acc.sector else "Sem Setor"
            result.append({
                "id": acc.id,
                "login": acc.login,
                "setor": sector_nome,
                "status": acc.status,
                "last_login": acc.last_login_at.strftime("%d/%m/%Y %H:%M") if acc.last_login_at else "Nunca conectou"
            })
        return jsonify(result)
    finally:
        db.close()

@app.route('/api/admin/accounts/<int:account_id>/status', methods=['PUT'])
@admin_required
def admin_update_status(account_id):
    data = request.get_json(silent=True) or {}
    new_status = data.get('status')
    
    if new_status not in ['active', 'maintenance', 'disabled']:
        return jsonify({"erro": "Status inválido."}), 400
        
    db = SessionLocal()
    try:
        acc = db.query(AccountBB).filter(AccountBB.id == account_id).first()
        if not acc: return jsonify({"erro": "Conta não encontrada."}), 404
            
        acc.status = new_status
        db.commit()
        return jsonify({"mensagem": "Status atualizado com sucesso!"})
    finally:
        db.close()

@app.route('/api/admin/accounts/<int:account_id>', methods=['DELETE'])
@admin_required
def admin_delete_account(account_id):
    db = SessionLocal()
    try:
        acc = db.query(AccountBB).filter(AccountBB.id == account_id).first()
        if not acc: return jsonify({"erro": "Conta não encontrada."}), 404
            
        db.delete(acc)
        db.commit()
        return jsonify({"mensagem": "Conta excluída permanentemente."})
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
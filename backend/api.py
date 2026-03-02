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
            return jsonify({"erro": "Acesso negado."}), 403
        return f(*args, **kwargs)
    return decorated_function

def inicializar_sistema():
    tentativas = 10
    while tentativas > 0:
        try:
            init_db()
            seed_db()
            print("✅ Banco de Dados conectado e migrado!")
            return True
        except Exception as e:
            print(f"⚠️ Aguardando banco... {e}")
            tentativas -= 1
            time.sleep(5)
    return None

# 👇 ESTA É A FUNÇÃO QUE TINHA SIDO ENGOLIDA! 👇
def buscar_conta_para_setor(db, setor_nome):
    """Lógica inteligente que busca a conta vinculada ao setor (nova arquitetura Múltiplos Setores ou Fallback antigo)"""
    # 1. Tenta buscar pela nova estrutura de múltiplos setores (Ex: "|BB_Acordos|BB_Civel|")
    account = db.query(AccountBB).filter(
        AccountBB.status.in_(['active', 'ativo', 'provisoria_recebida', 'termo_assinado']),
        AccountBB.setores.like(f"%|{setor_nome}|%")
    ).order_by(AccountBB.id.asc()).first()
    
    if account: return account
    
    # 2. Fallback de transição (Se for uma conta cadastrada no modelo antigo)
    sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
    if sector:
        return db.query(AccountBB).filter(
            AccountBB.sector_id == sector.id, 
            AccountBB.status.in_(['active', 'ativo', 'provisoria_recebida', 'termo_assinado'])
        ).order_by(AccountBB.id.asc()).first()
    
    return None

# --- ROTAS DE PÁGINAS WEB ---
@app.route('/admin')
@app.route('/admin.html')
def serve_admin():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(base_dir, 'admin.html')

@app.route('/privacidade')
@app.route('/privacy')
def serve_privacy():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(base_dir, 'privacy.html')

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
        if not db.query(Sector).filter(Sector.nome == setor_nome).first():
            db.add(Sector(nome=setor_nome))
            db.commit()
            
        account = buscar_conta_para_setor(db, setor_nome)
        
        if not account:
            return jsonify({"status": "erro", "mensagem": f"Setor {setor_nome} sem conta válida/ativa vinculada."}), 403

        if account.cookie_payload and account.last_login_at:
            if (datetime.utcnow() - account.last_login_at).total_seconds() / 60 < 20:
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
    username, password = data.get('username'), data.get('password')
    setor_nome = data.get('setor') or request.args.get('setor')

    if not setor_nome: return jsonify({"status": "erro", "mensagem": "Setor ausente."}), 400

    if username and password:
        ad_result = autenticar_e_obter_setor(username, password)
        if ad_result['status'] == 'erro':
            return jsonify({"status": "unauthorized", "mensagem": "Credenciais inválidas."}), 401

    db = SessionLocal()
    try:
        account = buscar_conta_para_setor(db, setor_nome)
        if account:
            if account.cookie_payload and account.last_login_at:
                if (datetime.utcnow() - account.last_login_at).total_seconds() / 60 < 15:
                    redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": "Sessão quente retornada do Pool.", "concluido": True, "erro": False}))
                    return jsonify({"status": "queued"})
            
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
    username, password = data.get('username'), data.get('password')
    setor_nome = data.get('setor') or request.args.get('setor')

    if not setor_nome: return jsonify({"status": "erro", "mensagem": "Setor ausente."}), 400

    if username and password:
        ad_result = autenticar_e_obter_setor(username, password)
        if ad_result['status'] == 'erro':
            return jsonify({"status": "unauthorized", "mensagem": "Credenciais AD inválidas."}), 401
    
    db = SessionLocal()
    try:
        account = buscar_conta_para_setor(db, setor_nome)
        if account and account.cookie_payload:
            return jsonify({"status": "sucesso", "cookies": json.loads(account.cookie_payload)})
    finally:
        db.close()
    return jsonify({"status": "erro"}), 404

# --- ROTAS ADMINISTRATIVAS E DASHBOARD ---
@app.route('/api/admin/ad_sectors', methods=['GET'])
@admin_required
def admin_list_ad_sectors():
    ad_ous = listar_ous_bb_ad()
    db = SessionLocal()
    try:
        db_sectors = [s.nome for s in db.query(Sector).all()]
    finally:
        db.close()
    todos_setores = sorted(list(set(ad_ous + db_sectors)))
    return jsonify(todos_setores)

@app.route('/api/admin/dashboard_stats', methods=['GET'])
@admin_required
def admin_dashboard_stats():
    db = SessionLocal()
    try:
        total_accounts = db.query(AccountBB).count()
        active_accounts = db.query(AccountBB).filter(AccountBB.status.in_(['active', 'ativo', 'provisoria_recebida', 'termo_assinado'])).count()
        queue_size = redis_client.llen("queue:login_requests")
        logins_solicitados = int(redis_client.get('metrics:logins_solicitados') or 0)
        cookies_injetados = int(redis_client.get('metrics:cookies_injetados') or 0)
        robos_executados = int(redis_client.get('metrics:robos_executados') or 0)
        economia_pct = round((cookies_injetados / logins_solicitados) * 100, 1) if logins_solicitados > 0 else 0

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

@app.route('/api/admin/accounts', methods=['GET', 'POST'])
@admin_required
def gerenciar_contas():
    db = SessionLocal()
    try:
        if request.method == 'GET':
            accounts = db.query(AccountBB).all()
            result = []
            for acc in accounts:
                lista_setores = [s for s in (acc.setores or "").split("|") if s]
                if not lista_setores and acc.sector:
                    lista_setores = [acc.sector.nome]

                result.append({
                    "id": acc.id,
                    "login": acc.login,
                    "titular": acc.titular or "Não informado",
                    "setores": lista_setores,
                    "status": acc.status,
                    "data_validade": acc.data_validade,
                    "status_updated_at": acc.status_updated_at.isoformat() if acc.status_updated_at else None,
                    "last_login": acc.last_login_at.strftime("%d/%m/%Y %H:%M") if acc.last_login_at else "Nunca conectou"
                })
            return jsonify(result)
            
        elif request.method == 'POST':
            data = request.get_json() or {}
            if not data.get('login') or not data.get('senha'):
                return jsonify({"erro": "Login e Senha são obrigatórios"}), 400
                
            account = db.query(AccountBB).filter(AccountBB.login == data['login']).first()
            if not account:
                account = AccountBB(login=data['login'])
                db.add(account)
                
            account.senha = data['senha']
            account.titular = data.get('titular', '')
            account.status = data.get('status', 'cadastro_inicial')
            account.status_updated_at = datetime.utcnow()
            account.data_validade = data.get('data_validade', '')
            
            setores_lista = data.get('setores', [])
            account.setores = "|" + "|".join(setores_lista) + "|" if setores_lista else ""
            
            db.commit()
            return jsonify({"mensagem": "Conta criada com sucesso!"})
    except Exception as e:
        db.rollback()
        return jsonify({"erro": str(e)}), 500
    finally:
        db.close()

@app.route('/api/admin/accounts/<int:account_id>', methods=['PUT', 'DELETE'])
@admin_required
def editar_conta(account_id):
    db = SessionLocal()
    try:
        acc = db.query(AccountBB).filter(AccountBB.id == account_id).first()
        if not acc: return jsonify({"erro": "Conta não encontrada."}), 404
        
        if request.method == 'DELETE':
            db.delete(acc)
            db.commit()
            return jsonify({"mensagem": "Conta excluída."})
            
        elif request.method == 'PUT':
            data = request.get_json() or {}
            
            if data.get('senha'): acc.senha = data['senha']
            if 'titular' in data: acc.titular = data['titular']
            if 'data_validade' in data: acc.data_validade = data['data_validade']
            
            if 'status' in data and acc.status != data['status']:
                acc.status = data['status']
                acc.status_updated_at = datetime.utcnow()
                
            if 'setores' in data:
                setores_lista = data['setores']
                acc.setores = "|" + "|".join(setores_lista) + "|" if setores_lista else ""
            
            db.commit()
            return jsonify({"mensagem": "Conta atualizada com sucesso!"})
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
    if not os.path.exists('static'): os.makedirs('static')
    if inicializar_sistema():
        app.run(host='0.0.0.0', port=5000)
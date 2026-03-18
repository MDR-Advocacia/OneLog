from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import redis
import json
import os
import time
from datetime import datetime, timedelta
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

def buscar_conta_para_setor(db, setor_nome):
    account = db.query(AccountBB).filter(
        AccountBB.status.in_(['active', 'ativo', 'provisoria_recebida', 'termo_assinado']),
        AccountBB.setores.like(f"%|{setor_nome}|%")
    ).order_by(AccountBB.id.asc()).first()
    if account: return account
    
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

@app.route('/shared/<path:filename>')
def serve_shared(filename):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    shared_dir = os.path.join(base_dir, 'shared')
    return send_from_directory(shared_dir, filename)

# --- ROTAS DE OPERAÇÃO (EXTENSÃO) ---
@app.route('/api/zerocore/status', methods=['GET'])
def get_status():
    setor_nome = request.args.get('setor')
    if not setor_nome: return jsonify({"mensagem": "Setor ausente."}), 400

    db = SessionLocal()
    try:
        account = buscar_conta_para_setor(db, setor_nome)
        if account and account.cookie_payload and account.last_login_at:
            if (datetime.utcnow() - account.last_login_at).total_seconds() / 60 < 19:
                return jsonify({"concluido": True, "mensagem": "Conexão segura estabelecida!"})
    finally:
        db.close()

    status_str = redis_client.get(f"status:{setor_nome}")
    return jsonify(json.loads(status_str)) if status_str else jsonify({"mensagem": "Aguardando sincronização..."})

@app.route('/api/zerocore/login', methods=['POST'])
def request_login():
    data = request.get_json(silent=True) or {}
    username, password = data.get('username'), data.get('password')
    user_agent = data.get('user_agent')
    
    if not username or not password:
        return jsonify({"status": "erro", "mensagem": "Usuário e senha são obrigatórios."}), 400

    ad_result = autenticar_e_obter_setor(username, password)
    if ad_result['status'] == 'erro': return jsonify(ad_result), 401

    setor_nome = ad_result['setor']
    
    # 📊 Registo de métricas com data (Histórico Diário)
    hoje = datetime.utcnow().strftime('%Y-%m-%d')
    redis_client.incr(f'metrics:logins_solicitados:{hoje}')
    redis_client.hincrby(f'metrics:sector_logins:{hoje}', setor_nome, 1)
    
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
                redis_client.incr(f'metrics:cookies_injetados:{hoje}')
                redis_client.hincrby(f'metrics:account_logins:{hoje}', str(account.login), 1)
                return jsonify({
                    "status": "sucesso", "setor": setor_nome,
                    "cookies": json.loads(account.cookie_payload),
                    "url": "https://juridico.bb.com.br/wfj"
                })
        
        # =========================================================
        # 🔒 A TRAVA DE FILA FOI RESTAURADA AQUI
        # =========================================================
        lock_key = f"lock:queue:{account.id}"
        if redis_client.exists(lock_key):
            redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": "Sincronizando com conexão em andamento...", "concluido": False}))
            return jsonify({"status": "queued", "setor": setor_nome}) 
            
        redis_client.setex(lock_key, 600, "1")
        # =========================================================

        redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": "Iniciando robô...", "concluido": False}))
        task_payload = json.dumps({"id": account.id, "setor": setor_nome, "user_agent": user_agent})
        redis_client.lpush("queue:login_requests", task_payload)
        
        redis_client.incr(f'metrics:robos_executados:{hoje}')
        redis_client.hincrby(f'metrics:account_logins:{hoje}', str(account.login), 1)
        
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
    user_agent = data.get('user_agent')

    if not setor_nome: return jsonify({"status": "erro", "mensagem": "Setor ausente."}), 400

    if username and password:
        ad_result = autenticar_e_obter_setor(username, password)
        if ad_result['status'] == 'erro':
            return jsonify({"status": "unauthorized", "mensagem": "Credenciais inválidas."}), 401

    db = SessionLocal()
    hoje = datetime.utcnow().strftime('%Y-%m-%d')
    try:
        account = buscar_conta_para_setor(db, setor_nome)
        if account:
            redis_client.hincrby(f'metrics:sector_logins:{hoje}', setor_nome, 1)
            
            if account.cookie_payload and account.last_login_at:
                if (datetime.utcnow() - account.last_login_at).total_seconds() / 60 < 15:
                    redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": "Sessão quente retornada do Pool.", "concluido": True, "erro": False}))
                    redis_client.hincrby(f'metrics:account_logins:{hoje}', str(account.login), 1)
                    return jsonify({"status": "queued"})
            
            # =========================================================
            # 🔒 A TRAVA DE FILA FOI RESTAURADA AQUI
            # =========================================================
            lock_key = f"lock:queue:{account.id}"
            if redis_client.exists(lock_key):
                redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": "Aguardando renovação de segurança...", "concluido": False, "erro": False}))
                return jsonify({"status": "queued"})

            redis_client.setex(lock_key, 600, "1")
            # =========================================================

            redis_client.set(f"status:{setor_nome}", json.dumps({"mensagem": "Renovação de Marcapasso...", "concluido": False, "erro": False}))
            task_payload = json.dumps({"id": account.id, "setor": setor_nome, "user_agent": user_agent})
            redis_client.lpush("queue:login_requests", task_payload)
            
            redis_client.incr(f'metrics:robos_executados:{hoje}')
            redis_client.hincrby(f'metrics:account_logins:{hoje}', str(account.login), 1)
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
    hoje = datetime.utcnow().strftime('%Y-%m-%d')
    try:
        total_accounts = db.query(AccountBB).count()
        active_accounts = db.query(AccountBB).filter(AccountBB.status.in_(['active', 'ativo', 'provisoria_recebida', 'termo_assinado'])).count()
        queue_size = redis_client.llen("queue:login_requests")
        
        logins_solicitados = int(redis_client.get(f'metrics:logins_solicitados:{hoje}') or 0)
        cookies_injetados = int(redis_client.get(f'metrics:cookies_injetados:{hoje}') or 0)
        robos_executados = int(redis_client.get(f'metrics:robos_executados:{hoje}') or 0)
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

@app.route('/api/admin/analytics', methods=['GET'])
@admin_required
def admin_analytics():
    # Agora a rota capta também a saúde dos robôs e os motivos das falhas de infraestrutura.
    start_str = request.args.get('start', datetime.utcnow().strftime('%Y-%m-%d'))
    end_str = request.args.get('end', datetime.utcnow().strftime('%Y-%m-%d'))
    
    try:
        start_date = datetime.strptime(start_str, '%Y-%m-%d')
        end_date = datetime.strptime(end_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD"}), 400

    total_logins = 0
    total_cookies = 0
    
    sector_stats = {}
    account_stats = {}
    robot_success_stats = {}
    robot_error_stats = {}
    error_reasons_stats = {}
    
    current_date = start_date
    while current_date <= end_date:
        day_str = current_date.strftime('%Y-%m-%d')
        
        total_logins += int(redis_client.get(f'metrics:logins_solicitados:{day_str}') or 0)
        total_cookies += int(redis_client.get(f'metrics:cookies_injetados:{day_str}') or 0)
        
        for sec, count in redis_client.hgetall(f'metrics:sector_logins:{day_str}').items():
            sector_stats[sec] = sector_stats.get(sec, 0) + int(count)
            
        for acc, count in redis_client.hgetall(f'metrics:account_logins:{day_str}').items():
            account_stats[acc] = account_stats.get(acc, 0) + int(count)
            
        # Telemetria dos Robôs
        for robo, count in redis_client.hgetall(f'metrics:robot_success:{day_str}').items():
            robot_success_stats[robo] = robot_success_stats.get(robo, 0) + int(count)
            
        for robo, count in redis_client.hgetall(f'metrics:robot_error:{day_str}').items():
            robot_error_stats[robo] = robot_error_stats.get(robo, 0) + int(count)
            
        for reason, count in redis_client.hgetall(f'metrics:error_reasons:{day_str}').items():
            error_reasons_stats[reason] = error_reasons_stats.get(reason, 0) + int(count)
            
        current_date += timedelta(days=1)

    sorted_sectors = [{"name": k, "count": v} for k, v in sorted(sector_stats.items(), key=lambda x: x[1], reverse=True)]
    sorted_accounts = [{"name": k, "count": v} for k, v in sorted(account_stats.items(), key=lambda x: x[1], reverse=True)]
    
    # Prepara o Array de Robôs mesclando os sucessos e erros
    all_robots = set(list(robot_success_stats.keys()) + list(robot_error_stats.keys()))
    robots_data = []
    for robo in all_robots:
        s = robot_success_stats.get(robo, 0)
        e = robot_error_stats.get(robo, 0)
        robots_data.append({
            "name": robo, "success": s, "error": e, 
            "success_rate": round((s / (s + e)) * 100, 1) if (s + e) > 0 else 0
        })
    robots_data.sort(key=lambda x: x['name'])
    
    sorted_reasons = [{"reason": k, "count": v} for k, v in sorted(error_reasons_stats.items(), key=lambda x: x[1], reverse=True)]
    economia_pct = round((total_cookies / total_logins) * 100, 1) if total_logins > 0 else 0

    return jsonify({
        "period": f"{start_str} a {end_str}",
        "efficiency": {
            "total_logins_requested": total_logins,
            "total_cookies_injected": total_cookies,
            "economia_pct": economia_pct
        },
        "sectors": sorted_sectors,
        "accounts": sorted_accounts,
        "robots_performance": robots_data,
        "error_diagnostics": sorted_reasons
    })

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
                    "senha": acc.senha,
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
            account.data_validade = data.get('data_validade', '')
            
            if data.get('status_updated_at'):
                try: account.status_updated_at = datetime.strptime(data['status_updated_at'], "%Y-%m-%d")
                except ValueError: account.status_updated_at = datetime.utcnow()
            else: account.status_updated_at = datetime.utcnow() 
            
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
            
            mudou_status = 'status' in data and acc.status != data['status']
            if 'status' in data: acc.status = data['status']
            
            if mudou_status:
                if data.get('status_updated_at'):
                    try: acc.status_updated_at = datetime.strptime(data['status_updated_at'], "%Y-%m-%d")
                    except ValueError: acc.status_updated_at = datetime.utcnow()
                else: acc.status_updated_at = datetime.utcnow()
            else:
                if data.get('status_updated_at'):
                    try:
                        nova_data = datetime.strptime(data['status_updated_at'], "%Y-%m-%d")
                        acc.status_updated_at = nova_data
                    except ValueError: pass
                
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

@app.route('/api/admin/accounts/<int:account_id>/clear', methods=['POST'])
@admin_required
def clear_account_cookies(account_id):
    db = SessionLocal()
    try:
        acc = db.query(AccountBB).filter(AccountBB.id == account_id).first()
        if not acc: return jsonify({"erro": "Conta não encontrada."}), 404
        
        acc.cookie_payload = None
        acc.last_login_at = None
        
        db.commit()
        return jsonify({"mensagem": "Sessão purgada com sucesso!"})
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
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import redis
import json
import os
from datetime import datetime
from database import SessionLocal, Sector, AccountBB, init_db, seed_db
from ad_integration import autenticar_e_obter_setor

app = Flask(__name__)
CORS(app)

# Inicializa o Banco de Dados ao iniciar a API
init_db()
seed_db()

# Conexão com o Redis (Fila e Cache Rápido)
redis_client = redis.Redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)

@app.route('/api/zerocore/status', methods=['GET'])
def get_status():
    """Consulta o status do processamento para um setor específico."""
    setor_nome = request.args.get('setor')
    if not setor_nome:
        return jsonify({"mensagem": "Setor não identificado."}), 400
        
    status_str = redis_client.get(f"status:{setor_nome}")
    if status_str:
        return jsonify(json.loads(status_str))
    return jsonify({"mensagem": "Aguardando...", "concluido": False, "erro": False, "imagem": None})

@app.route('/api/zerocore/login', methods=['POST'])
def request_login():
    """
    Endpoint Principal:
    1. Autentica no AD.
    2. Identifica o Setor (OU).
    3. Entrega cookie do Pool ou aciona o Robô.
    """
    data = request.json
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"status": "erro", "mensagem": "Usuário e senha são obrigatórios."}), 400

    # 1. Autenticação no Active Directory
    ad_result = autenticar_e_obter_setor(username, password)
    
    if ad_result['status'] == 'erro':
        return jsonify(ad_result), 401

    setor_nome = ad_result['setor']
    
    db = SessionLocal()
    try:
        # 2. Busca o Setor e a Conta vinculada no Banco de Dados
        # Se o setor do AD não existir no nosso DB, criamos automaticamente
        sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
        if not sector:
            sector = Sector(nome=setor_nome)
            db.add(sector)
            db.commit()
            db.refresh(sector)
            
        account = db.query(AccountBB).filter(AccountBB.sector_id == sector.id, AccountBB.status == 'active').first()
        
        if not account:
            return jsonify({
                "status": "erro", 
                "mensagem": f"Acesso negado: O setor {setor_nome} ainda não possui uma conta do BB vinculada pelo administrador."
            }), 403

        # 3. Verifica se existe Cookie válido (Pool de Sessões)
        if account.cookie_payload and account.last_login_at:
            age_minutes = (datetime.now() - account.last_login_at).total_seconds() / 60
            if age_minutes < 20:
                return jsonify({
                    "status": "sucesso",
                    "setor": setor_nome,
                    "cookies": json.loads(account.cookie_payload),
                    "url": "https://juridico.bb.com.br/wfj",
                    "cached": True
                })
        
        # 4. Se não houver cookie, aciona o Robô via Redis
        redis_client.set(f"status:{setor_nome}", json.dumps({
            "mensagem": f"Autenticado como {username}. Iniciando robô para o setor {setor_nome}...", 
            "concluido": False, "erro": False, "imagem": None
        }))
        
        redis_client.lpush("queue:login_requests", account.id)
        
        return jsonify({
            "status": "queued", 
            "setor": setor_nome, 
            "mensagem": "Sessão expirada. O robô foi acionado para renovação automática."
        })
        
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": f"Erro interno: {str(e)}"}), 500
    finally:
        db.close()

@app.route('/api/zerocore/renew', methods=['POST'])
def renew_session():
    """Força a renovação em background pelo Marcapasso."""
    setor_nome = request.args.get('setor')
    if not setor_nome: return jsonify({"status": "erro"}), 400
    
    db = SessionLocal()
    try:
        sector = db.query(Sector).filter(Sector.nome == setor_nome).first()
        if sector:
            account = db.query(AccountBB).filter(AccountBB.sector_id == sector.id).first()
            if account:
                redis_client.lpush("queue:login_requests", account.id)
                return jsonify({"status": "queued"})
    finally:
        db.close()
    return jsonify({"status": "erro"}), 400

@app.route('/api/zerocore/reset', methods=['POST'])
def api_reset():
    setor_nome = request.args.get('setor', 'GERAL')
    redis_client.delete(f"status:{setor_nome}")
    return jsonify({"status": "resetado"})

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

if __name__ == '__main__':
    if not os.path.exists('static'): os.makedirs('static')
    app.run(host='0.0.0.0', port=5000)
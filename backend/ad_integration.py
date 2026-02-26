import os
from ldap3 import Server, Connection, ALL, SUBTREE
import logging

logger = logging.getLogger(__name__)

AD_SERVER_IP = os.getenv("AD_SERVER_IP", "206.42.43.192")
AD_DOMAIN = os.getenv("AD_DOMAIN", "mdr.local")

def autenticar_e_obter_setor(usuario, senha):
    """
    Tenta logar no AD com as credenciais fornecidas.
    Se der certo, varre a árvore do usuário procurando uma OU que comece com 'BB_'.
    """
    server = Server(AD_SERVER_IP, port=636, use_ssl=True, get_info=ALL)
    user_principal = f"{usuario}@{AD_DOMAIN}"
    
    try:
        conn = Connection(server, user=user_principal, password=senha, auto_bind=True)
        logger.info(f"Autenticação bem-sucedida no AD para o usuário: {usuario}")
        
        base_dn = "DC=mdr,DC=local" 
        
        conn.search(
            search_base=base_dn,
            search_filter=f'(sAMAccountName={usuario})',
            search_scope=SUBTREE,
            attributes=['distinguishedName']
        )
        
        if conn.entries:
            dn = str(conn.entries[0].distinguishedName)
            partes = dn.split(',')
            for parte in partes:
                if parte.startswith('OU=BB_'):
                    setor = parte.replace('OU=', '')
                    logger.info(f"Usuário {usuario} pertence ao setor: {setor}")
                    return {"status": "sucesso", "setor": setor}
                    
            logger.warning(f"Usuário {usuario} logou, mas não pertence a nenhuma OU BB_.")
            return {"status": "erro", "mensagem": "Acesso negado: Você não está alocado em um setor do Banco do Brasil."}
            
        else:
            return {"status": "erro", "mensagem": "Dados do usuário não encontrados no diretório."}
            
    except Exception as e:
        logger.error(f"Falha de login no AD para {usuario}: {e}")
        return {"status": "erro", "mensagem": "Usuário ou senha do Windows incorretos."}

def listar_ous_bb_ad():
    """
    Varre a árvore do AD de forma global e lista todas as OUs de setores do BB.
    Para isso funcionar globalmente, você pode definir um AD_SERVICE_USER no .env
    """
    # Conta genérica de leitura no AD (Pode criar depois no servidor e adicionar no .env)
    ad_user = os.getenv("AD_SERVICE_USER")
    ad_pass = os.getenv("AD_SERVICE_PASS")

    if not ad_user or not ad_pass:
        logger.warning("Credenciais AD_SERVICE_USER não configuradas. Usando apenas setores em cache.")
        return []

    server = Server(AD_SERVER_IP, port=636, use_ssl=True, get_info=ALL)
    try:
        conn = Connection(server, user=f"{ad_user}@{AD_DOMAIN}", password=ad_pass, auto_bind=True)
        
        # Busca todas as OUs do domínio
        base_dn = "DC=mdr,DC=local"
        conn.search(
            search_base=base_dn,
            search_filter='(objectCategory=organizationalUnit)',
            search_scope=SUBTREE,
            attributes=['name']
        )
        
        setores = set()
        for entry in conn.entries:
            nome_ou = str(entry.name)
            if nome_ou.startswith('BB_'):
                setores.add(nome_ou)
                
        return sorted(list(setores))
    except Exception as e:
        logger.error(f"Falha ao listar OUs do AD: {e}")
        return []
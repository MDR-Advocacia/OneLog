import os
from ldap3 import Server, Connection, ALL, SUBTREE
from ldap3.utils.conv import escape_filter_chars
import logging

logger = logging.getLogger(__name__)

AD_SERVER_IP = os.getenv("AD_SERVER_IP", "206.42.43.192")
AD_DOMAIN = os.getenv("AD_DOMAIN", "mdr.local")
AD_BASE_DN = os.getenv("AD_BASE_DN", "DC=mdr,DC=local")
AD_ADMIN_GROUP_DN = os.getenv("AD_ADMIN_GROUP_DN", "CN=Domain Admins,CN=Users,DC=mdr,DC=local")

def _parse_int_attr(entry, attr_name, default=0):
    try:
        value = getattr(entry, attr_name, default)
        if hasattr(value, "value"):
            value = value.value
        return int(value)
    except Exception:
        return default

def _is_entry_active(entry):
    user_account_control = _parse_int_attr(entry, 'userAccountControl', 0)
    account_disabled = bool(user_account_control & 0x0002)
    if account_disabled:
        return False

    account_expires = _parse_int_attr(entry, 'accountExpires', 0)
    # 0 e 9223372036854775807 representam "não expira" no AD
    if account_expires not in (0, 9223372036854775807):
        # Convertendo de Windows FileTime para epoch simplificado
        expires_unix = (account_expires / 10_000_000) - 11644473600
        import time
        if expires_unix <= time.time():
            return False

    return True

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
        
        base_dn = AD_BASE_DN 
        
        conn.search(
            search_base=base_dn,
            search_filter=f'(sAMAccountName={usuario})',
            search_scope=SUBTREE,
            attributes=['distinguishedName', 'userAccountControl', 'accountExpires']
        )
        
        if conn.entries:
            entry = conn.entries[0]
            if not _is_entry_active(entry):
                logger.warning(f"Usuário {usuario} autenticou, mas a conta do AD está inativa/desabilitada.")
                return {"status": "erro", "mensagem": "Acesso negado: conta do Windows inativa ou desabilitada."}

            dn = str(entry.distinguishedName)
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

def autenticar_admin_ad(usuario, senha):
    """
    Autentica no AD e permite acesso administrativo apenas a membros de Domain Admins.
    Usa a regra recursiva de grupos do AD para suportar grupos aninhados.
    """
    server = Server(AD_SERVER_IP, port=636, use_ssl=True, get_info=ALL)
    user_principal = f"{usuario}@{AD_DOMAIN}"

    try:
        conn = Connection(server, user=user_principal, password=senha, auto_bind=True)
        logger.info(f"Autenticação administrativa bem-sucedida no AD para o usuário: {usuario}")

        usuario_escapado = escape_filter_chars(usuario)
        grupo_escapado = escape_filter_chars(AD_ADMIN_GROUP_DN)

        conn.search(
            search_base=AD_BASE_DN,
            search_filter=(
                f"(&(objectClass=user)"
                f"(sAMAccountName={usuario_escapado})"
                f"(memberOf:1.2.840.113556.1.4.1941:={grupo_escapado}))"
            ),
            search_scope=SUBTREE,
            attributes=['distinguishedName', 'displayName', 'cn', 'mail', 'memberOf', 'sAMAccountName', 'userAccountControl', 'accountExpires']
        )

        if not conn.entries:
            logger.warning(f"Usuário {usuario} autenticou no AD, mas não pertence ao grupo administrativo permitido.")
            return {"status": "erro", "mensagem": "Acesso negado: usuário sem permissão administrativa."}

        entry = conn.entries[0]
        if not _is_entry_active(entry):
            logger.warning(f"Usuário {usuario} pertence ao grupo administrativo, mas a conta do AD está inativa/desabilitada.")
            return {"status": "erro", "mensagem": "Acesso negado: conta do Windows inativa ou desabilitada."}

        display_name = str(getattr(entry, 'displayName', '') or getattr(entry, 'cn', '') or usuario)
        email = str(getattr(entry, 'mail', '') or "")

        return {
            "status": "sucesso",
            "usuario": str(getattr(entry, 'sAMAccountName', usuario) or usuario),
            "display_name": display_name,
            "email": email,
            "grupo_admin": AD_ADMIN_GROUP_DN
        }

    except Exception as e:
        logger.error(f"Falha de login administrativo no AD para {usuario}: {e}")
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
        base_dn = AD_BASE_DN
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

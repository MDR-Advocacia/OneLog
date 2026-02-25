import os
from ldap3 import Server, Connection, ALL, SUBTREE
import logging

logger = logging.getLogger(__name__)

# Configurações do AD (Devem estar no seu .env da AWS)
# IMPORTANTE: Na AWS, o IP será o IP Público do seu escritório (206.42.43.192)
AD_SERVER_IP = os.getenv("AD_SERVER_IP", "206.42.43.192")
AD_DOMAIN = os.getenv("AD_DOMAIN", "mdr.local")

def autenticar_e_obter_setor(usuario, senha):
    """
    Tenta logar no AD com as credenciais fornecidas.
    Se der certo, varre a árvore do usuário procurando uma OU que comece com 'BB_'.
    """
    # Define o servidor usando LDAPS (Porta 636 com SSL)
    server = Server(AD_SERVER_IP, port=636, use_ssl=True, get_info=ALL)
    
    # Formato de login do Windows (ex: rildon.silva@mdr.local ou mdr\rildon.silva)
    user_principal = f"{usuario}@{AD_DOMAIN}"
    
    try:
        # 1. Tenta fazer o "Bind" (Login)
        # Se a senha estiver errada, vai disparar uma exceção e cair no except
        conn = Connection(server, user=user_principal, password=senha, auto_bind=True)
        
        logger.info(f"Autenticação bem-sucedida no AD para o usuário: {usuario}")
        
        # 2. Busca os dados do usuário para ler a árvore de OUs dele
        base_dn = "DC=mdr,DC=local" # Ajuste para o seu domínio base
        
        conn.search(
            search_base=base_dn,
            search_filter=f'(sAMAccountName={usuario})',
            search_scope=SUBTREE,
            attributes=['distinguishedName']
        )
        
        if conn.entries:
            # Exemplo de dn: "CN=Rildon Silva,OU=BB_Acordos,OU=01_Passivo_Reu,OU=01_Juridico,OU=MDR,DC=mdr,DC=local"
            dn = str(conn.entries[0].distinguishedName)
            
            # 3. Varre as pastas de trás pra frente procurando a OU do Banco
            partes = dn.split(',')
            for parte in partes:
                if parte.startswith('OU=BB_'):
                    # Encontramos! Limpa o texto (remove 'OU=')
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
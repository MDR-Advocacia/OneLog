🚀 OneLog Enterprise RPA

Plataforma de acesso automatizado, concorrente e imortal para o Portal Jurídico do Banco do Brasil.

🏗️ Arquitetura do Sistema

Este repositório está dividido em dois microsserviços principais:

/backend (AWS / Coolify)

API Manager (api.py): Interface HTTP (Flask) que gerencia pedidos de login, distribui cookies e interage com o Active Directory.

Worker (worker.py): Robô (SeleniumBase) que opera em background, consumindo a fila do Redis para acessar o portal do banco.

Redis: Fila de processamento e Pool de Cookies.

PostgreSQL: Banco de dados de contas, setores e logs de auditoria.

/extension (Chrome Extension V3)

Intercepta requisições e camufla o User-Agent (Spoofing).

Mantém um Heartbeat (Marcapasso) com o Backend para renovar cookies silenciosamente antes da expiração.

Injeta cookies no navegador sem derrubar a navegação do usuário.

🚀 Como subir o ambiente de Dev

1. Backend

cd backend
# Renomeie o arquivo de variáveis de ambiente e preencha suas credenciais
cp .env.example .env

# Suba os containers usando o Docker Compose
docker-compose up -d --build


A API estará rodando em http://localhost:5000.

2. Extensão (Frontend)

Abra o Google Chrome e acesse chrome://extensions/.

Ative o Modo do desenvolvedor no canto superior direito.

Clique em Carregar sem compactação e selecione a pasta extension deste repositório.

🛡️ Segurança e Variáveis de Ambiente

NUNCA faça commit do arquivo .env contendo credenciais reais do Active Directory ou senhas bancárias. Use sempre o .env.example como template.
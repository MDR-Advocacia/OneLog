import os
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime

# Busca a URL do Banco de Dados no .env (ou usa SQLite por padrão para testes locais)
DB_URL = os.getenv("DB_URL", "sqlite:///onelog_local.db")

# Substitui 'postgres://' por 'postgresql://' se necessário (exigência do SQLAlchemy atual)
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DB_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- MODELOS DAS TABELAS ---

class Sector(Base):
    __tablename__ = "sectors"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, index=True) # Ex: "Trabalhista", "Civel", "Geral"
    
    accounts = relationship("AccountBB", back_populates="sector")

class AccountBB(Base):
    __tablename__ = "accounts_bb"
    id = Column(Integer, primary_key=True, index=True)
    login = Column(String, unique=True, index=True)
    senha = Column(String) # Em um cenário real de produção, isso deve ser criptografado!
    status = Column(String, default="active") # active, maintenance, locked
    cookie_payload = Column(Text, nullable=True) # JSON com os cookies
    user_agent_used = Column(String, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    
    # Novas colunas de estatísticas
    login_requests = Column(Integer, default=0)
    cache_hits = Column(Integer, default=0)
    robot_runs = Column(Integer, default=0)
    
    sector_id = Column(Integer, ForeignKey("sectors.id"), nullable=True)
    sector = relationship("Sector", back_populates="accounts")

# --- FUNÇÕES DE INICIALIZAÇÃO ---

def init_db():
    """Cria as tabelas no banco de dados se não existirem."""
    Base.metadata.create_all(bind=engine)

def seed_db():
    """Insere dados de teste iniciais para você poder testar sem o Painel Admin."""
    db = SessionLocal()
    if not db.query(Sector).first():
        geral = Sector(nome="GERAL")
        db.add(geral)
        db.commit()
        db.refresh(geral)
        
        # IMPORTANTE: Altere aqui para as suas credenciais de teste temporárias.
        # Depois gerenciaremos isso pelo painel Admin.
        conta_teste = AccountBB(
            login="12345678", 
            senha="TUA_SENHA_AQUI", 
            sector_id=geral.id,
            status="active"
        )
        db.add(conta_teste)
        db.commit()
    db.close()

if __name__ == "__main__":
    print("Inicializando o Banco de Dados...")
    init_db()
    seed_db()
    print("Banco de Dados pronto!")
import os
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DB_URL = os.getenv("DB_URL", "sqlite:///onelog_local.db")

if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DB_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Sector(Base):
    __tablename__ = "sectors"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, index=True)
    accounts = relationship("AccountBB", back_populates="sector")

class AccountBB(Base):
    __tablename__ = "accounts_bb"
    id = Column(Integer, primary_key=True, index=True)
    
    # NOVOS CAMPOS
    titular = Column(String, nullable=True) # Nome do dono da conta
    setores = Column(String, nullable=True) # Setores vinculados. Ex: "|BB_Acordos|BB_Civel|"
    
    login = Column(String, unique=True, index=True)
    senha = Column(String)
    status = Column(String, default="active") 
    cookie_payload = Column(String, nullable=True)
    user_agent_used = Column(String, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    
    # Mantido para compatibilidade reversa com as contas antigas
    sector_id = Column(Integer, ForeignKey("sectors.id"), nullable=True)
    sector = relationship("Sector", back_populates="accounts")

def init_db():
    Base.metadata.create_all(bind=engine)
    
    # Auto-Migrate: Adiciona as colunas novas na tabela já existente automaticamente
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE accounts_bb ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'active'"))
            conn.execute(text("ALTER TABLE accounts_bb ADD COLUMN IF NOT EXISTS titular VARCHAR"))
            conn.execute(text("ALTER TABLE accounts_bb ADD COLUMN IF NOT EXISTS setores VARCHAR"))
    except Exception as e:
        print(f"Migração ignorada ou já aplicada: {e}")

def seed_db():
    pass
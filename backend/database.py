import os
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# O ERRO ESTAVA AQUI: Voltamos a usar DB_URL, que é a variável correta do seu docker-compose.yml
DB_URL = os.getenv("DB_URL", "sqlite:///onelog_local.db")

# Substitui 'postgres://' por 'postgresql://' se necessário
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
    login = Column(String, unique=True, index=True)
    senha = Column(String)
    
    # Status da conta para o Painel Admin (active, maintenance, disabled)
    status = Column(String, default="active") 
    
    cookie_payload = Column(String, nullable=True)
    user_agent_used = Column(String, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    sector_id = Column(Integer, ForeignKey("sectors.id"))
    sector = relationship("Sector", back_populates="accounts")

def init_db():
    Base.metadata.create_all(bind=engine)
    
    # Auto-Migrate de Segurança
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE accounts_bb ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'active'"))
    except Exception as e:
        print(f"Migração ignorada ou já aplicada: {e}")

def seed_db():
    pass
import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DB_URL = os.getenv("DB_URL", "sqlite:///onelog_local.db")

if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DB_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

ACCOUNT_COLUMN_MIGRATIONS = {
    "status": "ALTER TABLE accounts_bb ADD COLUMN status VARCHAR DEFAULT 'active'",
    "titular": "ALTER TABLE accounts_bb ADD COLUMN titular VARCHAR",
    "setores": "ALTER TABLE accounts_bb ADD COLUMN setores VARCHAR",
    "data_validade": "ALTER TABLE accounts_bb ADD COLUMN data_validade VARCHAR",
    "status_updated_at": (
        "ALTER TABLE accounts_bb ADD COLUMN status_updated_at "
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    ),
}

class Sector(Base):
    __tablename__ = "sectors"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, index=True)
    accounts = relationship("AccountBB", back_populates="sector")

class AccountBB(Base):
    __tablename__ = "accounts_bb"
    id = Column(Integer, primary_key=True, index=True)
    
    # Gestão Enterprise (Contas e Vínculos)
    titular = Column(String, nullable=True) 
    setores = Column(String, nullable=True) 
    
    # Credenciais e Status
    login = Column(String, unique=True, index=True)
    senha = Column(String)
    status = Column(String, default="active") 
    
    # Controle de Validade (Planilha do Google)
    data_validade = Column(String, nullable=True) 
    status_updated_at = Column(DateTime, default=datetime.utcnow) 
    
    # Dados de Sessão
    cookie_payload = Column(String, nullable=True)
    user_agent_used = Column(String, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    
    # Legado (Mantido para retrocompatibilidade)
    sector_id = Column(Integer, ForeignKey("sectors.id"), nullable=True)
    sector = relationship("Sector", back_populates="accounts")

def init_db():
    Base.metadata.create_all(bind=engine)

    # PostgreSQL takes an ACCESS EXCLUSIVE lock even for
    # `ADD COLUMN IF NOT EXISTS`. Running all five statements on every API
    # startup can therefore leave the API unavailable while a worker holds a
    # normal read transaction during browser login. Inspect first and execute
    # DDL only when the schema is genuinely missing a column.
    try:
        existing_columns = {
            column["name"] for column in inspect(engine).get_columns("accounts_bb")
        }
        pending_migrations = [
            statement
            for column_name, statement in ACCOUNT_COLUMN_MIGRATIONS.items()
            if column_name not in existing_columns
        ]
        if not pending_migrations:
            return

        with engine.begin() as conn:
            for statement in pending_migrations:
                conn.execute(text(statement))
    except Exception as e:
        print(f"Migração ignorada ou já aplicada: {e}")

def seed_db():
    pass

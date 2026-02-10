from fastapi import FastAPI
import os
import redis
import psycopg2
from psycopg2 import OperationalError

app = FastAPI()

# Pega as configurações das variáveis de ambiente
DB_URL = os.getenv("DATABASE_URL", "postgresql://user:password@db:5432/bot_db")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

@app.get("/")
def read_root():
    return {"message": "Bot WhatsApp API v1.0"}

@app.get("/health")
def health_check():
    status = {"api": "online", "postgres": "disconnected", "redis": "disconnected"}
    
    # Testa conexão com Redis
    try:
        r = redis.from_url(REDIS_URL)
        if r.ping():
            status["redis"] = "connected"
    except Exception as e:
        status["redis_error"] = str(e)

    # Testa conexão com Postgres
    try:
        conn = psycopg2.connect(DB_URL)
        conn.close()
        status["postgres"] = "connected"
    except OperationalError as e:
        status["postgres_error"] = str(e)

    return status   
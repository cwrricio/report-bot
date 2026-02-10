from fastapi import FastAPI, Request
import os
import redis
import psycopg2
from psycopg2 import OperationalError
import requests
import json

app = FastAPI()

# --- Configurações ---
DB_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")
# Token da Whapi (Você pega no painel deles, no topo ou em Configurações)
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN") 

# Conecta ao Redis
try:
    r = redis.from_url(REDIS_URL, decode_responses=True)
except:
    print("Aviso: Redis não conectado (verifique a variável REDIS_URL)")
    r = None

# Opções da Enquete
OPCOES = {"1": "Bundudo", "2": "Divo", "3": "Todas as alternativas"}

# --- Função de Envio (Adaptada para Whapi) ---
def send_whapi_message(chat_id, message):
    """Envia mensagem usando a Whapi.cloud"""
    url = "https://gate.whapi.cloud/messages/text"
    
    headers = {
        "Authorization": f"Bearer {WHAPI_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "to": chat_id, # Whapi precisa do ID completo (ex: 551199...@s.whatsapp.net)
        "body": message,
        "typing_time": 0
    }
    
    try:
        print(f"📤 Enviando para {chat_id}: {message}")
        response = requests.post(url, headers=headers, json=payload)
        # Se der erro 4xx ou 5xx, vai avisar no log
        if response.status_code >= 400:
            print(f"❌ Erro Whapi: {response.text}")
    except Exception as e:
        print(f"❌ Erro de conexão ao enviar: {e}")

@app.get("/")
def home():
    return {"status": "Bot Whapi Online 🚀"}

# --- ROTA DO WEBHOOK (Simplificada) ---
@app.post("/webhook")
async def receive_webhook(request: Request):
    try:
        body = await request.json()
        
        # A Whapi manda uma lista de 'messages'
        messages = body.get("messages", [])
        
        # Se não tiver mensagem (pode ser evento de status), ignora
        if not messages:
            return {"status": "ignored"}

        for message_data in messages:
            # Ignora se for mensagem que EU mesmo enviei (from_me)
            if message_data.get("from_me"):
                continue

            # Pega o ID de quem mandou (ex: 55559999@s.whatsapp.net)
            chat_id = message_data.get("chat_id")
            
            # Pega o texto. A Whapi põe o texto dentro de 'text' -> 'body'
            text_body = message_data.get("text", {}).get("body", "")
            
            if not text_body:
                continue # Pula se for imagem/audio sem legenda

            texto = text_body.strip().lower()
            
            # Pega o nome do contato (opcional)
            nome = message_data.get("from_name", "Anônimo")

            print(f"📩 Recebido de {nome}: {texto}")

            # --- LÓGICA DA ENQUETE (Redis + Postgres) ---
            if r:
                state_key = f"voto:{chat_id}:status"
                estado_atual = r.get(state_key)

                # CENÁRIO 1: Início
                if not estado_atual:
                    r.set(state_key, "AGUARDANDO_VOTO", ex=300)
                    msg = "🧐 *Enquete Oficial*\nO que o Ciocca é?\n1️⃣ - Bundudo\n2️⃣ - Divo\n3️⃣ - Todas\n_(Responda com o número)_"
                    send_whapi_message(chat_id, msg)
                
                # CENÁRIO 2: Recebendo o Voto
                elif estado_atual == "AGUARDANDO_VOTO":
                    if texto in OPCOES:
                        escolha = OPCOES[texto]
                        
                        # Salva no Banco Neon
                        try:
                            conn = psycopg2.connect(DB_URL)
                            cur = conn.cursor()
                            # Garante tabela
                            cur.execute("CREATE TABLE IF NOT EXISTS votos_ciocca (id SERIAL PRIMARY KEY, nome VARCHAR(100), voto VARCHAR(50), data TIMESTAMP DEFAULT NOW())")
                            # Insere voto
                            cur.execute("INSERT INTO votos_ciocca (nome, voto) VALUES (%s, %s)", (nome, escolha))
                            conn.commit()
                            conn.close()
                            print("✅ Voto salvo no Postgres")
                        except Exception as e:
                            print(f"❌ Erro Postgres: {e}")

                        # Limpa estado e agradece
                        r.delete(state_key)
                        send_whapi_message(chat_id, f"✅ Registrado! Ciocca é *{escolha}*.")
                    
                    else:
                        send_whapi_message(chat_id, "❌ Opção inválida! Digite apenas 1, 2 ou 3.")

    except Exception as e:
        print(f"❌ Erro Geral: {e}")
        return {"status": "error"}
    
    return {"status": "ok"}

@app.get("/health")
def health_check():
    status = {"api": "online", "postgres": "disconnected", "redis": "disconnected"}
    
    # Testa Redis
    try:
        if r and r.ping():
            status["redis"] = "connected"
    except Exception as e:
        status["redis_error"] = str(e)

    # Testa Postgres
    try:
        conn = psycopg2.connect(DB_URL)
        conn.close()
        status["postgres"] = "connected"
    except Exception as e:
        status["postgres_error"] = str(e)

    return status
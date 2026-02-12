import os
import redis
import psycopg2
import requests
import json
from fastapi import FastAPI, Request
from urllib.parse import urlparse, parse_qs

app = FastAPI()

# --- Configurações ---
RAW_DB_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN")

# --- Ajuste Automático para NEON (SSL) ---
# O Neon exige sslmode=require. Se não tiver na URL, adicionamos.
if "sslmode" not in RAW_DB_URL:
    if "?" in RAW_DB_URL:
        DB_URL = f"{RAW_DB_URL}&sslmode=require"
    else:
        DB_URL = f"{RAW_DB_URL}?sslmode=require"
else:
    DB_URL = RAW_DB_URL

# --- Conexão Redis ---
try:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()
    print("✅ [BOOT] Redis OK")
except Exception as e:
    print(f"⚠️ [BOOT] Redis Falhou: {e}")
    r = None

# --- Função de Envio com Retorno ---
def send_whapi(chat_id, text):
    """Retorna True se enviou, False se falhou"""
    url = "https://gate.whapi.cloud/messages/text"
    headers = {
        "Authorization": f"Bearer {WHAPI_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {"to": chat_id, "body": text}
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            print(f"📤 [WHAPI] Msg enviada para {chat_id}")
            return True
        else:
            print(f"❌ [WHAPI] Erro API: {res.text}")
            return False
    except Exception as e:
        print(f"❌ [WHAPI] Erro Conexão: {e}")
        return False

# --- Inicialização do Banco ---
def init_db():
    print("🔄 [DB] Inicializando tabelas...")
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        
        # Cria tabela Projetos
        cur.execute("""
            CREATE TABLE IF NOT EXISTS projetos (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(100) NOT NULL UNIQUE
            );
        """)
        # Garante projetos iniciais
        cur.execute("INSERT INTO projetos (nome) VALUES ('Codefolio'), ('MentorIA') ON CONFLICT (nome) DO NOTHING;")
        
        # Cria tabela Reportes
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reportes_log (
                id SERIAL PRIMARY KEY,
                projeto_nome VARCHAR(100) NOT NULL,
                usuario VARCHAR(255) NOT NULL,
                descricao TEXT NOT NULL,
                prioridade VARCHAR(20) NOT NULL,
                chat_id VARCHAR(100) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        conn.commit()
        print("✅ [DB] Tabelas confirmadas e commitadas.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ [DB] ERRO FATAL NA INICIALIZAÇÃO: {e}")

# Roda ao iniciar
init_db()

@app.get("/")
def home():
    return {"status": "Bot Blindado v3", "db_url_safe": DB_URL.split("@")[-1]}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        messages = data.get("messages", [])

        for msg in messages:
            if msg.get("from_me"): continue
            
            chat_id = msg.get("chat_id")
            user_name = msg.get("from_name", "Dev")
            text = msg.get("text", {}).get("body", "").strip()
            
            if not text: continue
            
            # --- COMANDO DE EMERGÊNCIA ---
            if text.lower() == "reset":
                if r: r.delete(f"flow:{chat_id}", f"data:{chat_id}")
                send_whapi(chat_id, "🔄 Estado resetado! Mande 'oi' para começar.")
                continue

            # Chaves Redis
            state_key = f"flow:{chat_id}"
            data_key = f"data:{chat_id}"
            
            step = r.get(state_key) if r else None

            print(f"📍 {user_name} | Step: {step} | Msg: {text}")

            # 1. INÍCIO
            if not step:
                # Tenta enviar a mensagem ANTES de mudar o estado
                sent = send_whapi(chat_id, 
                    f"Olá, *{user_name}*! 🛠️\n\n"
                    "Qual projeto?\n1️⃣ Codefolio\n2️⃣ MentorIA"
                )
                if sent and r:
                    r.set(state_key, "WAIT_PROJ", ex=600)

            # 2. ESCOLHA PROJETO
            elif step == "WAIT_PROJ":
                if text == "1": proj = "Codefolio"
                elif text == "2": proj = "MentorIA"
                else:
                    send_whapi(chat_id, "❌ Digite apenas 1 ou 2.")
                    continue
                
                sent = send_whapi(chat_id, f"✅ *{proj}*!\n📝 Descreva o problema (min 10 letras):")
                if sent and r:
                    r.hset(data_key, "projeto", proj)
                    r.set(state_key, "WAIT_DESC", ex=600)

            # 3. DESCRIÇÃO (AQUI OCORRIA O ERRO)
            elif step == "WAIT_DESC":
                if len(text) < 10:
                    send_whapi(chat_id, "⚠️ Muito curto. Detalhe mais.")
                    continue
                
                # Tenta enviar a pergunta de prioridade
                sent = send_whapi(chat_id, 
                    "📊 Qual a prioridade?\n"
                    "1️⃣ Alta 🔴\n2️⃣ Média 🟡\n3️⃣ Baixa 🟢"
                )
                
                # SÓ MUDA O ESTADO SE A MENSAGEM FOI ENVIADA
                if sent and r:
                    r.hset(data_key, "descricao", text)
                    r.set(state_key, "WAIT_PRIO", ex=600)
                elif not sent:
                    print("❌ Falha ao enviar pergunta de prioridade. Mantendo estado.")

            # 4. FINALIZAR
            elif step == "WAIT_PRIO":
                prio_map = {"1": "High", "2": "Medium", "3": "Low"}
                if text not in prio_map:
                    send_whapi(chat_id, "❌ Digite 1, 2 ou 3.")
                    continue
                
                prio = prio_map[text]
                raw_data = r.hgetall(data_key)
                proj = raw_data.get("projeto", "Unknown")
                desc = raw_data.get("descricao", "No desc")
                
                # SALVAR NO BANCO
                try:
                    conn = psycopg2.connect(DB_URL)
                    cur = conn.cursor()
                    print(f"💾 [DB] Inserindo: {proj} | {prio}")
                    
                    cur.execute("""
                        INSERT INTO reportes_log (projeto_nome, usuario, descricao, prioridade, chat_id)
                        VALUES (%s, %s, %s, %s, %s) RETURNING id
                    """, (proj, user_name, desc, prio, chat_id))
                    
                    new_id = cur.fetchone()[0]
                    conn.commit() # <--- COMMIT EXPLÍCITO
                    
                    print(f"✅ [DB] COMMITADO! ID: {new_id}")
                    send_whapi(chat_id, f"✅ Reporte *#{new_id}* salvo no banco!")
                    
                    # Limpa
                    cur.close()
                    conn.close()
                    r.delete(state_key, data_key)
                    
                except Exception as e:
                    print(f"❌ [DB] ERRO AO SALVAR: {e}")
                    send_whapi(chat_id, "❌ Erro ao salvar no banco. Tente de novo.")

    except Exception as e:
        print(f"🔥 Erro Crítico: {e}")

    return {"status": "ok"}
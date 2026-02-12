import os
import redis
import psycopg2
import requests
from fastapi import FastAPI, Request

app = FastAPI()

# =======================================================
# CONFIGURAÇÕES (Lê direto do Railway)
# =======================================================
RAW_DB_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN")

# Ajuste automático para SSL do Neon
if RAW_DB_URL and "sslmode" not in RAW_DB_URL:
    if "?" in RAW_DB_URL:
        DB_URL = f"{RAW_DB_URL}&sslmode=require"
    else:
        DB_URL = f"{RAW_DB_URL}?sslmode=require"
else:
    DB_URL = RAW_DB_URL

# =======================================================
# CONEXÕES E FUNÇÕES AUXILIARES
# =======================================================

# 1. Conexão Redis
try:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()
    print("✅ [BOOT] Redis Conectado")
except Exception as e:
    print(f"⚠️ [BOOT] Redis Falhou: {e}")
    r = None

# 2. Envio WhatsApp
def send_whapi(chat_id, text):
    url = "https://gate.whapi.cloud/messages/text"
    headers = {
        "Authorization": f"Bearer {WHAPI_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        requests.post(url, headers=headers, json={"to": chat_id, "body": text}, timeout=5)
    except Exception as e:
        print(f"❌ Erro Whapi: {e}")

# 3. Inicialização do Banco
def init_db():
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
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
        cur.close()
        conn.close()
        print("✅ [DB] Tabelas Prontas.")
    except Exception as e:
        print(f"❌ [DB] Erro Inicialização: {e}")

if DB_URL:
    init_db()

# =======================================================
# FLUXO DO BOT
# =======================================================

@app.get("/")
def home():
    return {"status": "Bot Otimizado vFinal"}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        messages = data.get("messages", [])

        for msg in messages:
            if msg.get("from_me"): continue
            
            chat_id = msg.get("chat_id")
            user_name = msg.get("from_name", "Anônimo")
            text = msg.get("text", {}).get("body", "").strip()
            
            if not text: continue
            
            # --- COMANDO RESET ---
            if text.lower() == "reset":
                if r: r.delete(f"flow:{chat_id}", f"data:{chat_id}")
                send_whapi(chat_id, "🔄 Reiniciado! Digite 'oi'.")
                continue

            # --- MÁQUINA DE ESTADOS ---
            state_key = f"flow:{chat_id}"
            data_key = f"data:{chat_id}"
            step = r.get(state_key) if r else None

            # 1. INÍCIO
            if not step:
                send_whapi(chat_id, f"Olá, *{user_name}*! 👋\n\nQual projeto você quer reportar?\n\n1️⃣ Codefolio\n2️⃣ MentorIA")
                if r: r.set(state_key, "WAIT_PROJ", ex=600)

            # 2. ESCOLHA DO PROJETO
            elif step == "WAIT_PROJ":
                if text == "1": proj = "Codefolio"
                elif text == "2": proj = "MentorIA"
                else:
                    send_whapi(chat_id, "⚠️ Opção inválida. Digite apenas *1* ou *2*.")
                    continue
                
                send_whapi(chat_id, f"✅ *{proj}* selecionado.\n\n📝 Descreva o problema ou tarefa:")
                if r:
                    r.hset(data_key, "projeto", proj)
                    r.set(state_key, "WAIT_DESC", ex=600)

            # 3. DESCRIÇÃO
            elif step == "WAIT_DESC":
                if len(text) < 5: # Reduzi um pouco a exigência pra testes rápidos
                    send_whapi(chat_id, "⚠️ Muito curto. Por favor, detalhe um pouco mais.")
                    continue
                
                send_whapi(chat_id, "📊 Qual a prioridade?\n\n1️⃣ Alta 🔴\n2️⃣ Média 🟡\n3️⃣ Baixa 🟢")
                if r:
                    r.hset(data_key, "descricao", text)
                    r.set(state_key, "WAIT_PRIO", ex=600)

            # 4. PRIORIDADE E CONFIRMAÇÃO
            elif step == "WAIT_PRIO":
                # Mapeamento duplo: O que vai pro Banco vs O que aparece pro Usuário
                map_db = {"1": "High", "2": "Medium", "3": "Low"}
                map_user = {"1": "Alta 🔴", "2": "Média 🟡", "3": "Baixa 🟢"}
                
                if text not in map_db:
                    send_whapi(chat_id, "⚠️ Digite 1, 2 ou 3.")
                    continue
                
                prio_db = map_db[text]       # Ex: High
                prio_display = map_user[text] # Ex: Alta 🔴
                
                # Recupera dados
                raw = r.hgetall(data_key)
                proj = raw.get("projeto")
                desc = raw.get("descricao")
                
                # Salva no Banco
                try:
                    conn = psycopg2.connect(DB_URL)
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO reportes_log (projeto_nome, usuario, descricao, prioridade, chat_id)
                        VALUES (%s, %s, %s, %s, %s) RETURNING id
                    """, (proj, user_name, desc, prio_db, chat_id))
                    
                    new_id = cur.fetchone()[0]
                    conn.commit()
                    cur.close()
                    conn.close()

                    # --- MENSAGEM FINAL FORMATADA ---
                    msg_final = (
                        f"✅ *Reporte Salvo!*\n\n"
                        f"📂 *Projeto:* {proj}\n"
                        f"📝 *Descrição:* {desc}\n"
                        f"🚨 *Prioridade:* {prio_display}\n"
                        f"👤 *Enviado por:* {user_name}\n\n"
                        f"_ID do chamado: #{new_id}_"
                    )
                    send_whapi(chat_id, msg_final)
                    
                    # Limpa o fluxo
                    if r: r.delete(state_key, data_key)
                    
                except Exception as e:
                    print(f"❌ Erro ao salvar: {e}")
                    send_whapi(chat_id, "❌ Erro ao salvar no sistema. Tente novamente.")

    except Exception as e:
        print(f"🔥 Erro Crítico: {e}")

    return {"status": "ok"}
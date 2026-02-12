import os
import redis
import psycopg2
import requests
from fastapi import FastAPI, Request

app = FastAPI()

# =======================================================
# CONFIGURAÇÕES (Lê direto das Variáveis do Railway)
# =======================================================
RAW_DB_URL = os.getenv("DATABASE_URL")  # Pega do Railway
REDIS_URL = os.getenv("REDIS_URL")      # Pega do Railway
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN")  # Pega do Railway

# Ajuste automático para SSL do Neon (obrigatório)
if RAW_DB_URL and "sslmode" not in RAW_DB_URL:
    if "?" in RAW_DB_URL:
        DB_URL = f"{RAW_DB_URL}&sslmode=require"
    else:
        DB_URL = f"{RAW_DB_URL}?sslmode=require"
else:
    DB_URL = RAW_DB_URL

# =======================================================
# CONEXÕES E FUNÇÕES
# =======================================================

# 1. Conexão Redis
try:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()
    print("✅ [BOOT] Redis Conectado")
except Exception as e:
    print(f"⚠️ [BOOT] Redis Falhou: {e}")
    r = None

# 2. Função de Envio WhatsApp
def send_whapi(chat_id, text):
    url = "https://gate.whapi.cloud/messages/text"
    headers = {
        "Authorization": f"Bearer {WHAPI_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        res = requests.post(url, headers=headers, json={"to": chat_id, "body": text}, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print(f"❌ Erro Whapi: {e}")
        return False

# 3. Inicialização do Banco de Dados
def init_db():
    print("🔄 [DB] Verificando tabelas...")
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        
        # Cria tabela de Reportes se não existir
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
        print("✅ [DB] Tabelas Prontas.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ [DB] Erro de Conexão: {e}")

# Executa a verificação ao iniciar
if DB_URL:
    init_db()

# =======================================================
# ROTAS DA API
# =======================================================

@app.get("/")
def home():
    return {"status": "Bot Online", "banco": "Configurado via Railway"}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        messages = data.get("messages", [])

        for msg in messages:
            if msg.get("from_me"): continue
            
            chat_id = msg.get("chat_id")
            user_name = msg.get("from_name", "Usuário")
            text = msg.get("text", {}).get("body", "").strip()
            
            if not text: continue
            
            # --- COMANDO RESET ---
            if text.lower() == "reset":
                if r: r.delete(f"flow:{chat_id}", f"data:{chat_id}")
                send_whapi(chat_id, "🔄 Reiniciado! Mande 'oi'.")
                continue

            # --- FLUXO PRINCIPAL ---
            state_key = f"flow:{chat_id}"
            data_key = f"data:{chat_id}"
            step = r.get(state_key) if r else None

            # 1. INÍCIO
            if not step:
                send_whapi(chat_id, f"Olá, *{user_name}*! 🛠️\n\nQual projeto?\n1️⃣ Codefolio\n2️⃣ MentorIA")
                if r: r.set(state_key, "WAIT_PROJ", ex=600)

            # 2. ESCOLHA DO PROJETO
            elif step == "WAIT_PROJ":
                if text == "1": proj = "Codefolio"
                elif text == "2": proj = "MentorIA"
                else:
                    send_whapi(chat_id, "❌ Digite 1 ou 2.")
                    continue
                
                send_whapi(chat_id, f"✅ *{proj}*!\n\n📝 Descreva o problema (min 10 letras):")
                if r:
                    r.hset(data_key, "projeto", proj)
                    r.set(state_key, "WAIT_DESC", ex=600)

            # 3. DESCRIÇÃO
            elif step == "WAIT_DESC":
                if len(text) < 10:
                    send_whapi(chat_id, "⚠️ Muito curto. Detalhe mais.")
                    continue
                
                send_whapi(chat_id, "📊 Qual a prioridade?\n1️⃣ Alta 🔴\n2️⃣ Média 🟡\n3️⃣ Baixa 🟢")
                if r:
                    r.hset(data_key, "descricao", text)
                    r.set(state_key, "WAIT_PRIO", ex=600)

            # 4. PRIORIDADE E SALVAR
            elif step == "WAIT_PRIO":
                prio_map = {"1": "High", "2": "Medium", "3": "Low"}
                if text not in prio_map:
                    send_whapi(chat_id, "❌ Digite 1, 2 ou 3.")
                    continue
                
                prio = prio_map[text]
                raw = r.hgetall(data_key)
                
                # Inserção no Banco
                try:
                    conn = psycopg2.connect(DB_URL)
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO reportes_log (projeto_nome, usuario, descricao, prioridade, chat_id)
                        VALUES (%s, %s, %s, %s, %s) RETURNING id
                    """, (raw.get("projeto"), user_name, raw.get("descricao"), prio, chat_id))
                    
                    new_id = cur.fetchone()[0]
                    conn.commit() # Confirmação explícita
                    cur.close()
                    conn.close()

                    send_whapi(chat_id, f"✅ Reporte *#{new_id}* salvo com sucesso! 🚀")
                    if r: r.delete(state_key, data_key) # Limpa fluxo
                    
                except Exception as e:
                    print(f"❌ Erro ao salvar: {e}")
                    send_whapi(chat_id, "❌ Erro ao salvar. Tente novamente.")

    except Exception as e:
        print(f"🔥 Erro Crítico: {e}")

    return {"status": "ok"}
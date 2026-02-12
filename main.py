import os
import redis
import psycopg2
import requests
import json
from fastapi import FastAPI, Request

app = FastAPI()

# =======================================================
# CONFIGURAÇÕES
# =======================================================
RAW_DB_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")

# --- ⚠️ COLOQUE SEUS IDS REAIS AQUI ⚠️ ---
# Exemplo: "Codefolio": "a1b2c3d4e5f6..."
NOTION_IDS = {
    "Codefolio": "303c5e35099880779367d853ed84f585", 
    "MentorIA":  "303c5e35099881f99447eea2c312a9c4"
}

# Ajuste SSL do Neon
if RAW_DB_URL and "sslmode" not in RAW_DB_URL:
    DB_URL = f"{RAW_DB_URL}&sslmode=require" if "?" in RAW_DB_URL else f"{RAW_DB_URL}?sslmode=require"
else:
    DB_URL = RAW_DB_URL

# =======================================================
# CONEXÕES
# =======================================================
try:
    r = redis.from_url(REDIS_URL, decode_responses=True)
except:
    r = None

# =======================================================
# FUNÇÕES
# =======================================================

def send_whapi(chat_id, text):
    try:
        requests.post(
            "https://gate.whapi.cloud/messages/text", 
            headers={"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json"}, 
            json={"to": chat_id, "body": text}, 
            timeout=5
        )
    except Exception as e:
        print(f"❌ [WHAPI] Erro: {e}")

def create_notion_card(projeto, descricao, prioridade, report_id):
    # Verifica o ID do Banco
    database_id = NOTION_IDS.get(projeto)
    
    if not database_id or "SUBSTITUA" in database_id:
        print(f"❌ [NOTION] Erro: ID não configurado para '{projeto}' no código!")
        return False

    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    payload = {
        "parent": {"database_id": database_id},
        "properties": {
            "Name": {"title": [{"text": {"content": f"Reporte #{report_id}"}}]},
            "Descrição": {"rich_text": [{"text": {"content": descricao}}]},
            "Prioridade": {"select": {"name": prioridade}},
            "Status": {"status": {"name": "Backlog"}},
            "ID": {"number": report_id}
        }
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            return True
        else:
            print(f"❌ [NOTION] Erro API: {res.text}")
            return False
    except Exception as e:
        print(f"❌ [NOTION] Erro Conexão: {e}")
        return False

# Inicializa Banco
if DB_URL:
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
    except Exception:
        pass

# =======================================================
# FLUXO
# =======================================================

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
            
            if text.lower() == "reset":
                if r: r.delete(f"flow:{chat_id}", f"data:{chat_id}")
                send_whapi(chat_id, "🔄 Reiniciado.")
                continue

            state_key = f"flow:{chat_id}"
            data_key = f"data:{chat_id}"
            step = r.get(state_key) if r else None

            # 1. INÍCIO
            if not step:
                send_whapi(chat_id, f"Olá, *{user_name}*! 👋\n\nQual projeto?\n1️⃣ Codefolio\n2️⃣ MentorIA")
                if r: r.set(state_key, "WAIT_PROJ", ex=600)

            # 2. ESCOLHA PROJETO
            elif step == "WAIT_PROJ":
                if text == "1": proj = "Codefolio"
                elif text == "2": proj = "MentorIA"
                else:
                    send_whapi(chat_id, "⚠️ Digite 1 ou 2.")
                    continue
                
                send_whapi(chat_id, f"✅ *{proj}*!\n\n📝 Descreva o problema:")
                if r:
                    r.hset(data_key, "projeto", proj)
                    r.set(state_key, "WAIT_DESC", ex=600)

            # 3. DESCRIÇÃO
            elif step == "WAIT_DESC":
                if len(text) < 5:
                    send_whapi(chat_id, "⚠️ Muito curto.")
                    continue
                
                send_whapi(chat_id, "📊 Prioridade?\n1️⃣ Alta 🔴\n2️⃣ Média 🟡\n3️⃣ Baixa 🟢")
                if r:
                    r.hset(data_key, "descricao", text)
                    r.set(state_key, "WAIT_PRIO", ex=600)

            # 4. FINALIZAR
            elif step == "WAIT_PRIO":
                map_db = {"1": "High", "2": "Medium", "3": "Low"}
                map_user = {"1": "Alta 🔴", "2": "Média 🟡", "3": "Baixa 🟢"}
                
                if text not in map_db:
                    send_whapi(chat_id, "⚠️ Digite 1, 2 ou 3.")
                    continue
                
                prio_db = map_db[text]
                prio_user = map_user[text]
                raw = r.hgetall(data_key)
                proj = raw.get("projeto")
                desc = raw.get("descricao")
                
                # --- PASSO 1: SALVAR NO BANCO (GARANTIDO) ---
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

                    # --- PASSO 2: MONTAR MENSAGEM DE SUCESSO ---
                    msg_final = (
                        f"✅ *Reporte Salvo!*\n\n"
                        f"📂 *Projeto:* {proj}\n"
                        f"📝 *Descrição:* {desc}\n"
                        f"🚨 *Prioridade:* {prio_user}\n"
                        f"👤 *Autor:* {user_name}\n"
                        f"🔢 *ID:* #{new_id}"
                    )

                    # --- PASSO 3: TENTAR NOTION ---
                    notion_ok = create_notion_card(proj, desc, prio_db, new_id)
                    
                    if notion_ok:
                        msg_final += "\n\n🔗 *Notion:* Sincronizado ✅"
                    else:
                        msg_final += "\n\n⚠️ *Notion:* Não sincronizado (Ver Logs)"

                    # --- PASSO 4: ENVIAR TUDO ---
                    send_whapi(chat_id, msg_final)
                    
                    if r: r.delete(state_key, data_key)

                except Exception as e:
                    print(f"❌ Erro DB: {e}")
                    send_whapi(chat_id, "❌ Erro crítico ao salvar no banco.")

    except Exception:
        pass

    return {"status": "ok"}
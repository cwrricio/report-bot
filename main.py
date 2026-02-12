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

# --- ⚠️ COLOQUE SEUS IDS AQUI ⚠️ ---
NOTION_IDS = {
    "Codefolio": "SUBSTITUA_PELO_ID_DO_CODEFOLIO", 
    "MentorIA":  "SUBSTITUA_PELO_ID_DO_MENTORIA"
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
    print("✅ [BOOT] Redis OK")
except:
    print("⚠️ [BOOT] Redis Falhou")
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
    print(f"\n🔍 [NOTION] Iniciando criação do card para: {projeto}")
    
    database_id = NOTION_IDS.get(projeto)
    
    # 1. Verifica se o ID foi configurado
    if not database_id or "SUBSTITUA" in database_id:
        print(f"❌ [NOTION] Erro: ID do banco não configurado para '{projeto}'!")
        return False

    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    # Mapeamento de prioridade (Certifique-se que o Notion tem essas opções exatas)
    prio_notion = prioridade # Já vem como High/Medium/Low do banco
    
    # 2. Monta o Payload (Dados)
    payload = {
        "parent": {"database_id": database_id},
        "properties": {
            "Name": {
                "title": [{"text": {"content": f"Reporte #{report_id}"}}]
            },
            "Descrição": {
                "rich_text": [{"text": {"content": descricao}}]
            },
            "Prioridade": {
                "select": {"name": prio_notion}
            },
            "Status": {
                "status": {"name": "Backlog"} # Ou "select" se sua coluna for Select
            },
            "ID": {
                "number": report_id
            }
        }
    }

    # 3. Imprime o Payload para Debug (Isso vai aparecer no log do Railway)
    print(f"📦 [NOTION] Enviando Payload:\n{json.dumps(payload, indent=2, ensure_ascii=False)}")

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        
        # 4. Verifica a resposta
        if res.status_code == 200:
            print(f"✅ [NOTION] Sucesso! Card criado.")
            return True
        else:
            print(f"❌ [NOTION] Falha (Status {res.status_code})")
            print(f"📜 [NOTION] Resposta da API: {res.text}") # AQUI ESTÁ O OURO
            return False
            
    except Exception as e:
        print(f"❌ [NOTION] Erro de Conexão: {e}")
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
    except Exception as e:
        print(f"❌ [DB] Erro ao iniciar: {e}")

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
                send_whapi(chat_id, "🔄 Resetado.")
                continue

            state_key = f"flow:{chat_id}"
            data_key = f"data:{chat_id}"
            step = r.get(state_key) if r else None

            # 1. INÍCIO
            if not step:
                send_whapi(chat_id, f"Olá, *{user_name}*! 👋\n\nQual projeto?\n1️⃣ Codefolio\n2️⃣ MentorIA")
                if r: r.set(state_key, "WAIT_PROJ", ex=600)

            # 2. ESCOLHA DO PROJETO
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
                
                if text not in map_db:
                    send_whapi(chat_id, "⚠️ Digite 1, 2 ou 3.")
                    continue
                
                prio = map_db[text]
                raw = r.hgetall(data_key)
                
                # Salva no Neon
                try:
                    conn = psycopg2.connect(DB_URL)
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO reportes_log (projeto_nome, usuario, descricao, prioridade, chat_id)
                        VALUES (%s, %s, %s, %s, %s) RETURNING id
                    """, (raw.get("projeto"), user_name, raw.get("descricao"), prio, chat_id))
                    new_id = cur.fetchone()[0]
                    conn.commit()
                    cur.close()
                    conn.close()

                    # Tenta salvar no Notion
                    notion_result = create_notion_card(raw.get("projeto"), raw.get("descricao"), prio, new_id)
                    
                    # Mensagem Final
                    status_icon = "✅" if notion_result else "⚠️"
                    status_msg = "Notion OK" if notion_result else "Erro no Notion (Cheque Logs)"
                    
                    send_whapi(chat_id, f"✅ Salvo no Banco!\n🆔 #{new_id}\n{status_icon} {status_msg}")
                    
                    if r: r.delete(state_key, data_key)

                except Exception as e:
                    print(f"❌ Erro Crítico DB: {e}")
                    send_whapi(chat_id, "❌ Erro ao salvar.")

    except Exception as e:
        print(f"🔥 Erro Webhook: {e}")

    return {"status": "ok"}
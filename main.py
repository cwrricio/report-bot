import os
import redis
import psycopg2
import requests
import json
from fastapi import FastAPI, Request

app = FastAPI()

# --- Configurações ---
DB_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")

# Redis com proteção
try:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()
    print("✅ Redis conectado")
except Exception as e:
    print(f"⚠️ Redis falhou: {e}")
    r = None

# ====================== FUNÇÕES ======================

def get_project_notion_id(project_name):
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute('SELECT notion_id FROM "projetos" WHERE nome = %s', (project_name,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return result[0] if result else None
    except Exception as e:
        print(f"ERRO NEON (projetos): {e}")
        return None


def log_report_to_neon(proj, user, desc, prio, chat_id):
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        
        cur.execute("""
            INSERT INTO "reportes_log" 
            (projeto_nome, usuario, descricao, prioridade, chat_id, notion_card_created, created_at)
            VALUES (%s, %s, %s, %s, %s, FALSE, NOW())
            RETURNING id
        """, (proj, user, desc, prio, chat_id))
        
        report_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        print(f"✅ Reporte salvo em reportes_log (ID: {report_id})")
        return report_id
    except Exception as e:
        print(f"❌ Erro ao salvar em reportes_log: {e}")
        return None


def update_report_notion_status(report_id, success):
    if not report_id:
        return
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute('UPDATE "reportes_log" SET notion_card_created = %s WHERE id = %s', 
                   (success, report_id))
        conn.commit()
        cur.close()
        conn.close()
        print(f"Atualizado notion_card_created = {success} (ID {report_id})")
    except Exception as e:
        print(f"Erro ao atualizar reportes_log: {e}")


def create_notion_card(db_id, proj, desc, prio, user):
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    payload = {
        "parent": {"database_id": db_id},
        "properties": {
            "Name": {"title": [{"text": {"content": f"Reporte: {proj}"}}]},
            "Descrição": {"rich_text": [{"text": {"content": desc}}]},
            "Prioridade": {"select": {"name": prio}},
            "Usuário": {"rich_text": [{"text": {"content": user}}]},
            "Status": {"status": {"name": "Backlog"}}
        }
    }

    print(f"Tentando criar card para DB: {db_id}")
    print(f"Payload enviado:\n{json.dumps(payload, indent=2, ensure_ascii=False)}")

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"Notion status: {response.status_code}")
        print(f"Notion response: {response.text[:800]}...")
        return response.status_code == 200
    except Exception as e:
        print(f"Exceção Notion: {e}")
        return False


# ====================== WHAPI ======================
def send_whapi_poll(chat_id, question, options, poll_type="proj"):
    url = "https://gate.whapi.cloud/messages/poll"
    payload = {
        "to": chat_id,
        "title": question,
        "options": [opt.strip()[:25] for opt in options],
        "count": 1
    }
    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json"}
    
    response = requests.post(url, headers=headers, json=payload)
    print(f"[POLL] Resposta: {response.status_code}")
    
    if response.status_code in (200, 201) and r:
        try:
            msg_id = response.json()["message"]["id"]
            r.set(f"poll_active:{chat_id}", msg_id, ex=1800)
            r.set(f"poll_options:{msg_id}", json.dumps(payload["options"]), ex=1800)
            r.set(f"poll_type:{msg_id}", poll_type, ex=1800)
            print(f"[POLL] Salvo no Redis -> ID: {msg_id}")
        except:
            pass
    return True


def send_whapi_text(chat_id, text):
    url = "https://gate.whapi.cloud/messages/text"
    payload = {"to": chat_id, "body": text}
    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json"}
    requests.post(url, headers=headers, json=payload)


# ====================== WEBHOOK ======================
@app.post("/webhook")
async def handle_flow(request: Request):
    data = await request.json()
    
    messages = data.get("messages", []) + data.get("messages_updates", [])
    
    for item in messages:
        if item.get("from_me"):
            continue

        chat_id = item.get("chat_id")
        user_name = item.get("from_name", "Anônimo")
        msg_type = item.get("type", "")

        content = None

        if msg_type == "text":
            content = item.get("text", {}).get("body", "").strip()

        elif msg_type == "action":
            action = item.get("action", {})
            if action.get("type") == "vote":
                votes = action.get("votes", [])
                target = action.get("target")
                if votes and target:
                    vote_id = votes[0]
                    resp = requests.get(
                        f"https://gate.whapi.cloud/messages/{target}",
                        headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
                    )
                    if resp.status_code == 200:
                        results = resp.json().get("poll", {}).get("results", [])
                        for res in results:
                            if res.get("id") == vote_id and res.get("count", 0) > 0:
                                content = res.get("name")
                                print(f"[VOTO] Sucesso! Opção selecionada: {content}")
                                break

        if not content:
            continue

        print(f"[CAPTURADO] {content} | Tipo: {msg_type}")

        state_key = f"flow:{chat_id}"
        step = r.get(state_key) if r else None

        if not step:
            if r:
                r.set(state_key, "SET_PROJ", ex=900)
            send_whapi_poll(chat_id, f"Olá, *{user_name}*! 🛠️\n\nQual projeto você deseja reportar?", ["Codefolio", "MentorIA"], "proj")

        elif step == "SET_PROJ":
            if content not in ["Codefolio", "MentorIA"]:
                send_whapi_text(chat_id, "Selecione uma opção na enquete.")
                send_whapi_poll(chat_id, "Escolha o projeto:", ["Codefolio", "MentorIA"], "proj")
                continue
            if r:
                r.set(f"data:{chat_id}:proj", content, ex=900)
                r.set(state_key, "SET_DESC", ex=900)
            send_whapi_text(chat_id, f"✅ *{content}* selecionado!\n\nAgora descreva o problema ou melhoria com detalhes:")

        elif step == "SET_DESC":
            if len(content) < 10:
                send_whapi_text(chat_id, "Descrição muito curta. Tente novamente.")
                continue
            if r:
                r.set(f"data:{chat_id}:desc", content, ex=900)
                r.set(state_key, "SET_PRIO", ex=900)
            send_whapi_poll(chat_id, "Qual a prioridade deste reporte?", ["High", "Medium", "Low"], "prio")

        elif step == "SET_PRIO":
            if content not in ["High", "Medium", "Low"]:
                send_whapi_text(chat_id, "Escolha uma prioridade na enquete.")
                send_whapi_poll(chat_id, "Qual a prioridade?", ["High", "Medium", "Low"], "prio")
                continue

            proj = r.get(f"data:{chat_id}:proj") if r else None
            desc = r.get(f"data:{chat_id}:desc") if r else None
            prio = content

            # 1. Salva primeiro no Neon
            report_id = log_report_to_neon(proj or "desconhecido", user_name, desc or content, prio, chat_id)

            # 2. Tenta criar no Notion
            notion_ok = False
            target_db = get_project_notion_id(proj or "")
            if target_db:
                notion_ok = create_notion_card(target_db, proj or "", desc or "", prio, user_name)

            # Atualiza status no Neon
            update_report_notion_status(report_id, notion_ok)

            if notion_ok:
                send_whapi_text(chat_id, "✅ Reporte enviado com sucesso! Card criado no Notion.")
            else:
                send_whapi_text(chat_id, "⚠️ Reporte salvo no banco (reportes_log), mas falhou ao criar o card no Notion.")

            if r:
                r.delete(state_key, f"data:{chat_id}:proj", f"data:{chat_id}:desc")

    return {"status": "ok"}


@app.get("/")
async def root():
    return {"status": "Bot ativo - v2.9 - reportes_log"}
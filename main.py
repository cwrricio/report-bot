import os
import redis
import psycopg2
import requests
from fastapi import FastAPI, Request
import json

app = FastAPI()

# --- Configurações ---
DB_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")

r = redis.from_url(REDIS_URL, decode_responses=True)

# --- Funções auxiliares ---
def get_project_notion_id(project_name):
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute("SELECT notion_id FROM projetos WHERE nome = %s", (project_name,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return result[0] if result else None
    except Exception as e:
        print(f"Erro ao consultar Neon: {e}")
        return None

def create_notion_card(db_id, proj, desc, prio, user):
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    # Payload alinhado exatamente com o que você confirmou
    payload = {
        "parent": {"database_id": db_id},
        "properties": {
            "Name": {"title": [{"text": {"content": f"Reporte: {proj}"}}]},
            "Descrição": {"rich_text": [{"text": {"content": desc}}]},
            "Prioridade": {"select": {"name": prio}},  # já chega como "High", "Medium", "Low"
            "Usuário": {"rich_text": [{"text": {"content": user}}]},
            "Status": {"status": {"name": "Backlog"}}  # B maiúsculo
        }
    }

    print(f"Tentando criar card para DB: {db_id}")
    print(f"Payload enviado:\n{json.dumps(payload, indent=2, ensure_ascii=False)}")

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"Notion status: {response.status_code}")
        print(f"Notion response: {response.text[:800]}...")  # mostra parte do erro se houver
        if response.status_code == 200:
            print("✅ Card criado no Notion com sucesso!")
        else:
            print("❌ Falha no Notion - verifique o response abaixo")
        return response.status_code == 200
    except Exception as e:
        print(f"Exceção na chamada ao Notion: {e}")
        return False

# --- Enviar Poll (Salva tudo no Redis) ---
def send_whapi_poll(chat_id, question, options, poll_type="proj"):
    url = "https://gate.whapi.cloud/messages/poll"
    formatted_options = [opt.strip()[:25] for opt in options]

    payload = {
        "to": chat_id,
        "title": question,
        "options": formatted_options,
        "count": 1
    }

    headers = {
        "Authorization": f"Bearer {WHAPI_TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.post(url, headers=headers, json=payload)
    print(f"[POLL] Resposta: {response.status_code} - {response.text}")

    if response.status_code in (200, 201):
        try:
            data = response.json()
            poll_id = data.get("message", {}).get("id")
            if poll_id:
                r.set(f"poll:{chat_id}:id", poll_id, ex=1800)
                r.set(f"poll:{poll_id}:options", json.dumps(formatted_options), ex=1800)
                r.set(f"poll:{poll_id}:type", poll_type, ex=1800)
                print(f"[POLL] Salvo no Redis -> ID: {poll_id} | Tipo: {poll_type} | Opções: {formatted_options}")
        except Exception as e:
            print(f"[POLL] Erro ao salvar no Redis: {e}")

    return response.status_code in (200, 201)

# --- Enviar texto ---
def send_whapi_text(chat_id, text):
    url = "https://gate.whapi.cloud/messages/text"
    payload = {"to": chat_id, "body": text}
    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json"}
    response = requests.post(url, headers=headers, json=payload)
    print(f"[TEXT] {response.status_code} - {response.text}")

# --- Webhook ---
@app.post("/webhook")
async def handle_flow(request: Request):
    data = await request.json()

    items = data.get("messages", []) + data.get("messages_updates", [])

    for item in items:
        if item.get("from_me", False):
            continue

        chat_id = item.get("chat_id")
        user_name = item.get("from_name", "Anônimo")
        msg_type = item.get("type", "")

        content = None

        # === CAPTURA DE VOTO NA ENQUETE ===
        if msg_type == "action":
            action = item.get("action", {})
            if action.get("type") == "vote":
                votes = action.get("votes", [])
                poll_id = action.get("target")

                if votes and poll_id:
                    vote_id = votes[0]

                    # Faz GET para pegar o nome exato da opção
                    get_url = f"https://gate.whapi.cloud/messages/{poll_id}"
                    get_headers = {"Authorization": f"Bearer {WHAPI_TOKEN}"}
                    try:
                        get_resp = requests.get(get_url, headers=get_headers, timeout=8)
                        if get_resp.status_code == 200:
                            poll_data = get_resp.json()
                            results = poll_data.get("poll", {}).get("results", [])
                            for res in results:
                                if res.get("id") == vote_id:
                                    content = res.get("name")
                                    print(f"[VOTO] Sucesso! Opção selecionada: {content} (ID: {vote_id})")
                                    break
                    except Exception as e:
                        print(f"[VOTO] Erro no GET do poll: {e}")

        # === Mensagem de texto normal (descrição) ===
        elif msg_type == "text":
            content = item.get("text", {}).get("body", "").strip()

        if not content:
            print(f"[IGNORADO] Tipo não processado: {msg_type}")
            continue

        print(f"[CAPTURADO] {content} | Tipo: {msg_type}")

        state_key = f"flow:{chat_id}"
        step = r.get(state_key)

        # PASSO 0: Início
        if not step:
            r.set(state_key, "SET_PROJ", ex=900)
            msg = f"Olá, *{user_name}*! 🛠️\n\nQual projeto você deseja reportar?"
            send_whapi_poll(chat_id, msg, ["Codefolio", "MentorIA"], poll_type="proj")

        # PASSO 1: Projeto selecionado
        elif step == "SET_PROJ":
            if content not in ["Codefolio", "MentorIA"]:
                send_whapi_text(chat_id, "⚠️ Por favor, selecione uma opção na enquete.")
                send_whapi_poll(chat_id, "Escolha o projeto:", ["Codefolio", "MentorIA"], poll_type="proj")
                continue

            r.set(f"data:{chat_id}:proj", content, ex=900)
            r.set(state_key, "SET_DESC", ex=900)
            send_whapi_text(chat_id, f"✅ *{content}* selecionado!\n\nAgora descreva o problema ou melhoria com detalhes:")

        # PASSO 2: Descrição
        elif step == "SET_DESC":
            if len(content) < 10:
                send_whapi_text(chat_id, "⚠️ Descrição muito curta. Por favor, dê mais detalhes.")
                continue
            r.set(f"data:{chat_id}:desc", content, ex=900)
            r.set(state_key, "SET_PRIO", ex=900)
            send_whapi_poll(chat_id, "Qual a prioridade deste reporte?", ["High", "Medium", "Low"], poll_type="prio")

        # PASSO 3: Prioridade
        elif step == "SET_PRIO":
            if content not in ["High", "Medium", "Low"]:
                send_whapi_text(chat_id, "⚠️ Escolha uma prioridade na enquete.")
                send_whapi_poll(chat_id, "Qual a prioridade?", ["High", "Medium", "Low"], poll_type="prio")
                continue

            proj = r.get(f"data:{chat_id}:proj")
            desc = r.get(f"data:{chat_id}:desc")
            prio = content

            target_db = get_project_notion_id(proj)
            if target_db and create_notion_card(target_db, proj, desc, prio, user_name):
                send_whapi_text(chat_id, 
                    f"✅ *Reporte Enviado com Sucesso!*\n\n"
                    f"📂 Projeto: {proj}\n"
                    f"⚡ Prioridade: {prio}\n"
                    f"📝 Descrição: {desc}\n\n"
                    "Card criado no Notion.")
            else:
                send_whapi_text(chat_id, "❌ Erro ao criar o card no Notion.")

            # Limpeza
            r.delete(state_key, f"data:{chat_id}:proj", f"data:{chat_id}:desc")
            poll_id = r.get(f"poll:{chat_id}:id")
            if poll_id:
                r.delete(f"poll:{poll_id}:options", f"poll:{poll_id}:type", f"poll:{chat_id}:id")

    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "Bot de reportes ativo", "version": "2.4 - Poll Robusto"}
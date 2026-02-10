import os
import redis
import psycopg2
import requests
from fastapi import FastAPI, Request

app = FastAPI()

# --- Configurações ---
DB_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")

r = redis.from_url(REDIS_URL, decode_responses=True)

# --- Funções auxiliares (Neon + Notion) ---
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
    response = requests.post(url, headers=headers, json=payload)
    return response.status_code == 200

# --- Enviar Poll (Enquete) - Formato correto para Whapi ---
def send_whapi_poll(chat_id, question, options):
    url = "https://gate.whapi.cloud/messages/poll"

    formatted_options = [opt.strip()[:25] for opt in options]

    payload = {
        "to": chat_id,
        "title": question,           # texto completo que aparece
        "options": formatted_options,
        "count": 1                   # escolha única
    }

    headers = {
        "Authorization": f"Bearer {WHAPI_TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.post(url, headers=headers, json=payload)
    print(f"Resposta Whapi (poll): {response.status_code} - {response.text}")
    return response.status_code in (200, 201)

# --- Enviar texto simples ---
def send_whapi_text(chat_id, text):
    url = "https://gate.whapi.cloud/messages/text"
    payload = {"to": chat_id, "body": text}
    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json"}
    response = requests.post(url, headers=headers, json=payload)
    print(f"Resposta Whapi (texto): {response.status_code} - {response.text}")

# --- Webhook Principal ---
@app.post("/webhook")
async def handle_flow(request: Request):
    data = await request.json()
    print(f"Webhook recebido: {data}")

    messages = data.get("messages", [])
    if not messages:
        return {"status": "ok"}

    for msg in messages:
        if msg.get("from_me"):
            continue

        chat_id = msg.get("chat_id")
        user_name = msg.get("from_name", "Anônimo")
        msg_type = msg.get("type", "")

        # ==================== CAPTURA MELHORADA DO CONTEÚDO ====================
        content = None

        if msg_type == "text":
            content = msg.get("text", {}).get("body", "").strip()

        elif msg_type == "poll":
            poll = msg.get("poll", {})
            # Formatos mais comuns que a Whapi envia quando o usuário vota
            if poll.get("selected"):
                content = poll.get("selected", "")
            elif poll.get("results"):
                for option in poll.get("results", []):
                    if option.get("count", 0) > 0 or option.get("voters"):
                        content = option.get("name", "")
                        break
            elif poll.get("name"):          # fallback
                content = poll.get("name", "")

        if not content:
            print(f"Conteúdo não capturado (tipo: {msg_type})")
            continue

        print(f"Conteúdo capturado: {content} | Tipo: {msg_type}")

        state_key = f"flow:{chat_id}"
        step = r.get(state_key)

        # PASSO 0: Início do fluxo
        if not step:
            r.set(state_key, "SET_PROJ", ex=900)
            boas_vindas = (
                f"Olá, *{user_name}*! 🛠️\n\n"
                "Bem-vindo ao sistema de reportes.\n"
                "Qual projeto você quer reportar?"
            )
            send_whapi_poll(chat_id, boas_vindas, ["Codefolio", "MentorIA"])

        # PASSO 1: Projeto
        elif step == "SET_PROJ":
            if content not in ["Codefolio", "MentorIA"]:
                send_whapi_text(chat_id, "⚠️ Por favor, selecione uma opção na enquete.")
                send_whapi_poll(chat_id, "Escolha o projeto:", ["Codefolio", "MentorIA"])
                continue

            r.set(f"data:{chat_id}:proj", content, ex=900)
            r.set(state_key, "SET_DESC", ex=900)
            send_whapi_text(chat_id, f"✅ Projeto *{content}* selecionado!\n\nAgora descreva o problema ou melhoria com detalhes (em uma única mensagem):")

        # PASSO 2: Descrição
        elif step == "SET_DESC":
            if len(content) < 10:
                send_whapi_text(chat_id, "⚠️ Descrição muito curta. Por favor, explique melhor.")
                continue

            r.set(f"data:{chat_id}:desc", content, ex=900)
            r.set(state_key, "SET_PRIO", ex=900)
            send_whapi_poll(chat_id, "Qual a prioridade deste reporte?", ["High", "Medium", "Low"])

        # PASSO 3: Prioridade
        elif step == "SET_PRIO":
            if content not in ["High", "Medium", "Low"]:
                send_whapi_text(chat_id, "⚠️ Escolha uma prioridade na enquete.")
                send_whapi_poll(chat_id, "Qual a prioridade?", ["High", "Medium", "Low"])
                continue

            proj = r.get(f"data:{chat_id}:proj")
            desc = r.get(f"data:{chat_id}:desc")
            prio = content

            target_db = get_project_notion_id(proj)
            if target_db and create_notion_card(target_db, proj, desc, prio, user_name):
                send_whapi_text(chat_id, 
                    f"✅ *Reporte enviado com sucesso!*\n\n"
                    f"📂 Projeto: {proj}\n"
                    f"⚡ Prioridade: {prio}\n"
                    f"📝 Descrição: {desc}\n\n"
                    "Card criado no Notion.\n\n"
                    "Para fazer outro reporte, é só enviar qualquer mensagem.")
            else:
                send_whapi_text(chat_id, "❌ Erro ao salvar no Notion. Tente novamente.")

            # Limpa estado
            r.delete(state_key, f"data:{chat_id}:proj", f"data:{chat_id}:desc")

    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "Bot ativo", "version": "2.3 - Poll otimizado"}
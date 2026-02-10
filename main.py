import os
import redis
import psycopg2
import requests
from fastapi import FastAPI, Request

app = FastAPI()

# --- Configurações ---
DB_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN") #
NOTION_TOKEN = os.getenv("NOTION_TOKEN")

r = redis.from_url(REDIS_URL, decode_responses=True)

# --- Função: Busca ID do Projeto no Neon ---
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

# --- Função: Cria Card no Notion ---
def create_notion_card(db_id, proj, desc, prio, user):
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    payload = {
        "parent": { "database_id": db_id },
        "properties": {
            "Name": { "title": [{"text": {"content": f"Reporte: {proj}"}}] },
            "Descrição": { "rich_text": [{"text": {"content": desc}}] },
            "Prioridade": { "select": {"name": prio} },
            "Usuário": { "rich_text": [{"text": {"content": user}}] },
            "Status": { "status": {"name": "Backlog"} } #
        }
    }
    response = requests.post(url, headers=headers, json=payload)
    return response.status_code == 200

# --- Função Única: Enviar Mensagem (Whapi) ---
def send_whapi(chat_id, text, buttons=None):
    """Envia texto ou botões de forma centralizada para facilitar manutenção"""
    if buttons:
        url = "https://gate.whapi.cloud/messages/interactive"
        payload = {
            "to": chat_id,
            "type": "buttons",
            "body": text,
            "action": {"buttons": [{"id": f"btn_{i}", "title": opt} for i, opt in enumerate(buttons)]}
        }
    else:
        url = "https://gate.whapi.cloud/messages/text"
        payload = {"to": chat_id, "body": text}
    
    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json"}
    requests.post(url, headers=headers, json=payload)

# --- Webhook Principal ---
@app.post("/webhook")
async def handle_flow(request: Request):
    data = await request.json()
    messages = data.get("messages", [])
    if not messages: return {"status": "ok"}

    for msg in messages:
        if msg.get("from_me"): continue
        chat_id = msg.get("chat_id")
        user_name = msg.get("from_name", "Anônimo")
        
        # Conteúdo vindo de botão ou texto
        content = msg.get("action", {}).get("title") if msg.get("type") == "action" else msg.get("text", {}).get("body", "").strip()

        state_key = f"flow:{chat_id}"
        step = r.get(state_key)

        # PASSO 1: Boas vindas e Escolha do Projeto
        if not step:
            r.set(state_key, "SET_PROJ", ex=900)
            msg_boas_vindas = (
                f"Olá, *{user_name}*! 🛠️\n\n"
                "Bem-vindo ao sistema de reportes. Para começar, "
                "por favor selecione qual projeto você deseja reportar abaixo:"
            )
            send_whapi(chat_id, msg_boas_vindas, ["Codefolio", "MentorIA"])

        # PASSO 2: Recebe Projeto -> Pede Descrição
        elif step == "SET_PROJ":
            r.set(f"data:{chat_id}:proj", content, ex=900)
            r.set(state_key, "SET_DESC", ex=900)
            msg_desc = (
                f"Projeto *{content}* selecionado! 📝\n\n"
                "Agora, por favor, descreva o problema ou a melhoria de forma detalhada "
                "em *UMA ÚNICA MENSAGEM*."
            )
            send_whapi(chat_id, msg_desc)

        # PASSO 3: Recebe Descrição -> Pede Prioridade (Final)
        elif step == "SET_DESC":
            r.set(f"data:{chat_id}:desc", content, ex=900)
            r.set(state_key, "SET_PRIO", ex=900)
            msg_prio = (
                "Entendido! Para finalizar o reporte, "
                "qual o nível de urgência/prioridade deste item? ⚠️"
            )
            send_whapi(chat_id, msg_prio, ["High", "Medium", "Low"])

        # PASSO 4: Recebe Prioridade -> Envia para o Notion e Confirma
        elif step == "SET_PRIO":
            proj = r.get(f"data:{chat_id}:proj")
            desc = r.get(f"data:{chat_id}:desc")
            prio = content # O valor do botão clicado
            
            target_db = get_project_notion_id(proj)
            
            if target_db and create_notion_card(target_db, proj, desc, prio, user_name):
                # Mensagem de confirmação rica em detalhes
                msg_confirmacao = (
                    "✅ *Reporte Enviado com Sucesso!*\n\n"
                    f"📂 *Projeto:* {proj}\n"
                    f"👤 *Enviado por:* {user_name}\n"
                    f"⚡ *Prioridade:* {prio}\n"
                    f"📝 *Descrição:* {desc}\n\n"
                    "Seu card já foi adicionado ao backlog no Notion."
                )
                send_whapi(chat_id, msg_confirmacao)
            else:
                send_whapi(chat_id, "❌ Erro ao enviar para o Notion. Verifique as conexões da página.")
            
            # Limpa o estado no Redis para o usuário poder reportar novamente
            r.delete(state_key, f"data:{chat_id}:proj", f"data:{chat_id}:desc")

    return {"status": "ok"}
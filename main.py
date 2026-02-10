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
            "Status": { "status": {"name": "Backlog"} }
        }
    }
    response = requests.post(url, headers=headers, json=payload)
    return response.status_code == 200

# --- Função: Enviar Lista Interativa (Menu) ---
def send_whapi_list(chat_id, header_text, body_text, button_title, sections):
    """
    Envia mensagem interativa do tipo LIST (menu dropdown).
    - sections: lista de dicts com 'title' (opcional) e 'rows' (lista de dicts com 'id', 'title', 'description' opcional)
    """
    url = "https://gate.whapi.cloud/messages/interactive"

    payload = {
        "to": chat_id,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {
                "type": "text",
                "text": header_text[:60]  # limite aproximado
            },
            "body": {
                "text": body_text[:1024]
            },
            "action": {
                "button": button_title[:20],  # título do botão que abre a lista
                "sections": sections          # lista de seções com rows
            }
        }
    }

    # Opcional: adicionar footer
    # "footer": {"text": "Selecione uma opção abaixo"}

    headers = {
        "Authorization": f"Bearer {WHAPI_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"Resposta Whapi (list): {response.status_code} - {response.text}")
        if response.status_code not in (200, 201):
            print(f"Payload enviado (debug): {payload}")
        return response.status_code in (200, 201)
    except Exception as e:
        print(f"Erro ao enviar lista: {e}")
        send_whapi_text(chat_id, "Ocorreu um erro ao enviar o menu. Tente novamente.")
        return False

# --- Função: Enviar Mensagem de Texto ---
def send_whapi_text(chat_id, text):
    url = "https://gate.whapi.cloud/messages/text"
    payload = {"to": chat_id, "body": text}
    headers = {
        "Authorization": f"Bearer {WHAPI_TOKEN}",
        "Content-Type": "application/json"
    }
    response = requests.post(url, headers=headers, json=payload)
    print(f"Resposta Whapi (texto): {response.status_code} - {response.text}")
    return response

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
        
        content = None
        
        # Captura resposta de lista interativa
        if msg_type == "interactive":
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "list_reply":
                reply = interactive.get("list_reply", {})
                content = reply.get("title", "").strip()  # ou reply.get("id") se preferir usar ID
                print(f"Seleção de lista: {content} (ID: {reply.get('id')})")
        
        # Mensagem de texto normal
        elif msg_type == "text":
            content = msg.get("text", {}).get("body", "").strip()
        
        if not content:
            print(f"Conteúdo vazio ou tipo não suportado: {msg_type}")
            continue
        
        print(f"Conteúdo capturado: {content}")
        
        state_key = f"flow:{chat_id}"
        step = r.get(state_key)
        
        print(f"Estado atual: {step}")

        # PASSO 0: Inicia fluxo
        if not step:
            r.set(state_key, "SET_PROJ", ex=900)
            header = "Selecione o projeto"
            body = f"Olá, *{user_name}*! 🛠️\nBem-vindo ao sistema de reportes.\nToque no botão abaixo para escolher:"
            button_title = "Ver projetos"
            sections = [
                {
                    "title": "Projetos disponíveis",
                    "rows": [
                        {"id": "proj_1", "title": "Codefolio", "description": "Reporte para Codefolio"},
                        {"id": "proj_2", "title": "MentorIA", "description": "Reporte para MentorIA"}
                    ]
                }
            ]
            send_whapi_list(chat_id, header, body, button_title, sections)

        # PASSO 1: Seleção do projeto
        elif step == "SET_PROJ":
            if content not in ["Codefolio", "MentorIA"]:
                send_whapi_text(chat_id, "⚠️ Por favor, selecione uma opção válida no menu.")
                # Reenvia o menu
                header = "Selecione o projeto"
                body = "Escolha novamente:"
                button_title = "Ver projetos"
                sections = [
                    {
                        "title": "Projetos",
                        "rows": [
                            {"id": "proj_1", "title": "Codefolio", "description": ""},
                            {"id": "proj_2", "title": "MentorIA", "description": ""}
                        ]
                    }
                ]
                send_whapi_list(chat_id, header, body, button_title, sections)
                continue
            
            r.set(f"data:{chat_id}:proj", content, ex=900)
            r.set(state_key, "SET_DESC", ex=900)
            msg_desc = (
                f"Projeto *{content}* selecionado! 📝\n\n"
                "Agora descreva o problema ou melhoria com detalhes "
                "(em uma única mensagem)."
            )
            send_whapi_text(chat_id, msg_desc)

        # PASSO 2: Descrição
        elif step == "SET_DESC":
            if len(content) < 10:
                send_whapi_text(chat_id, "⚠️ Descrição muito curta (mín. 10 caracteres). Tente novamente.")
                continue
            
            r.set(f"data:{chat_id}:desc", content, ex=900)
            r.set(state_key, "SET_PRIO", ex=900)
            header = "Prioridade do reporte"
            body = "Qual o nível de urgência?"
            button_title = "Escolher prioridade"
            sections = [
                {
                    "title": "Níveis de prioridade",
                    "rows": [
                        {"id": "prio_h", "title": "High", "description": "Urgente – precisa de atenção imediata"},
                        {"id": "prio_m", "title": "Medium", "description": "Importante – resolver em breve"},
                        {"id": "prio_l", "title": "Low", "description": "Normal – pode aguardar"}
                    ]
                }
            ]
            send_whapi_list(chat_id, header, body, button_title, sections)

        # PASSO 3: Prioridade
        elif step == "SET_PRIO":
            valid_priorities = ["High", "Medium", "Low"]
            if content not in valid_priorities:
                send_whapi_text(chat_id, "⚠️ Por favor, selecione uma prioridade válida no menu.")
                # Reenvia menu de prioridade
                header = "Prioridade do reporte"
                body = "Escolha novamente:"
                button_title = "Escolher prioridade"
                sections = [
                    {
                        "title": "Níveis",
                        "rows": [
                            {"id": "prio_h", "title": "High", "description": ""},
                            {"id": "prio_m", "title": "Medium", "description": ""},
                            {"id": "prio_l", "title": "Low", "description": ""}
                        ]
                    }
                ]
                send_whapi_list(chat_id, header, body, button_title, sections)
                continue
            
            proj = r.get(f"data:{chat_id}:proj")
            desc = r.get(f"data:{chat_id}:desc")
            prio = content
            
            target_db = get_project_notion_id(proj)
            
            if target_db and create_notion_card(target_db, proj, desc, prio, user_name):
                msg_confirmacao = (
                    "✅ *Reporte enviado com sucesso!*\n\n"
                    f"📂 Projeto: {proj}\n"
                    f"👤 Enviado por: {user_name}\n"
                    f"⚡ Prioridade: {prio}\n"
                    f"📝 Descrição: {desc}\n\n"
                    "Card adicionado ao backlog no Notion.\n"
                    "Para novo reporte, envie qualquer mensagem! 💬"
                )
                send_whapi_text(chat_id, msg_confirmacao)
            else:
                send_whapi_text(chat_id, "❌ Erro ao criar card no Notion. Tente novamente.")
            
            # Limpa estado
            r.delete(state_key, f"data:{chat_id}:proj", f"data:{chat_id}:desc")

    return {"status": "ok"}

# Endpoint de teste
@app.get("/")
async def root():
    return {"status": "Bot de reportes ativo!", "version": "2.2 - com List Messages"}
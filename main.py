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

# --- Função: Enviar Mensagem com Botões (Whapi) ---
ddef send_whapi_buttons(chat_id, text, buttons):
    url = "https://gate.whapi.cloud/messages/interactive"

    formatted_buttons = []
    for i, button_text in enumerate(buttons):
        formatted_buttons.append({
            "type": "reply",
            "reply": {
                "id": f"btn_{i+1}",           # id único, pode ser qualquer string curta
                "title": button_text[:20]     # WhatsApp limita título a ~20 chars
            }
        })

    payload = {
        "to": chat_id,
        "type": "interactive",
        "interactive": {
            "type": "button",                 # ← esse "button" é o tipo correto para quick reply buttons
            "body": {
                "text": text
            },
            "action": {
                "buttons": formatted_buttons  # ← aqui ficam os botões
            }
        }
    }

    # Opcional: adicionar header e/ou footer se quiser
    # "header": {"type": "text", "text": "Escolha uma opção:"},
    # "footer": {"text": "Clique em um botão abaixo"},

    headers = {
        "Authorization": f"Bearer {WHAPI_TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.post(url, headers=headers, json=payload)
    print(f"Resposta Whapi (botões): {response.status_code} - {response.text}")
    return response

# --- Função: Enviar Mensagem de Texto (Whapi) ---
def send_whapi_text(chat_id, text):
    """Envia mensagem de texto simples"""
    url = "https://gate.whapi.cloud/messages/text"
    payload = {
        "to": chat_id,
        "body": text
    }
    
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
        # Ignora mensagens enviadas pelo bot
        if msg.get("from_me"):
            continue
            
        chat_id = msg.get("chat_id")
        user_name = msg.get("from_name", "Anônimo")
        
        # Verifica o tipo de mensagem
        msg_type = msg.get("type", "")
        
        # Captura o conteúdo baseado no tipo
        content = None
        
        # Se for resposta de botão interativo
        if msg_type == "interactive":
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                content = interactive.get("button_reply", {}).get("title", "").strip()
        
        # Se for mensagem de texto normal
        elif msg_type == "text":
            content = msg.get("text", {}).get("body", "").strip()
        
        # Se não conseguiu capturar conteúdo, ignora
        if not content:
            print(f"Conteúdo vazio ou tipo não suportado: {msg_type}")
            continue
        
        print(f"Conteúdo capturado: {content}")
        
        state_key = f"flow:{chat_id}"
        step = r.get(state_key)
        
        print(f"Estado atual: {step}")

        # PASSO 0: Qualquer mensagem sem estado inicia o fluxo
        if not step:
            r.set(state_key, "SET_PROJ", ex=900)
            msg_boas_vindas = (
                f"Olá, *{user_name}*! 🛠️\n\n"
                "Bem-vindo ao sistema de reportes. Para começar, "
                "por favor selecione qual projeto você deseja reportar:"
            )
            send_whapi_buttons(chat_id, msg_boas_vindas, ["Codefolio", "MentorIA"])

        # PASSO 1: Aguardando seleção do projeto
        elif step == "SET_PROJ":
            # Valida se é um projeto válido
            if content not in ["Codefolio", "MentorIA"]:
                send_whapi_text(chat_id, "⚠️ Por favor, selecione uma das opções disponíveis usando os botões.")
                send_whapi_buttons(chat_id, "Escolha o projeto:", ["Codefolio", "MentorIA"])
                continue
            
            r.set(f"data:{chat_id}:proj", content, ex=900)
            r.set(state_key, "SET_DESC", ex=900)
            msg_desc = (
                f"Projeto *{content}* selecionado! 📝\n\n"
                "Agora, por favor, descreva o problema ou a melhoria de forma detalhada "
                "em *UMA ÚNICA MENSAGEM*."
            )
            send_whapi_text(chat_id, msg_desc)

        # PASSO 2: Aguardando descrição do problema
        elif step == "SET_DESC":
            # Valida se a descrição não está vazia
            if len(content) < 10:
                send_whapi_text(chat_id, "⚠️ A descrição está muito curta. Por favor, descreva o problema com mais detalhes (mínimo 10 caracteres).")
                continue
            
            r.set(f"data:{chat_id}:desc", content, ex=900)
            r.set(state_key, "SET_PRIO", ex=900)
            msg_prio = (
                "Entendido! ✅\n\n"
                "Para finalizar o reporte, qual o nível de urgência/prioridade deste item?"
            )
            send_whapi_buttons(chat_id, msg_prio, ["High", "Medium", "Low"])

        # PASSO 3: Aguardando seleção da prioridade
        elif step == "SET_PRIO":
            # Valida se é uma prioridade válida
            if content not in ["High", "Medium", "Low"]:
                send_whapi_text(chat_id, "⚠️ Por favor, selecione uma das prioridades disponíveis usando os botões.")
                send_whapi_buttons(chat_id, "Escolha a prioridade:", ["High", "Medium", "Low"])
                continue
            
            proj = r.get(f"data:{chat_id}:proj")
            desc = r.get(f"data:{chat_id}:desc")
            prio = content
            
            target_db = get_project_notion_id(proj)
            
            if target_db and create_notion_card(target_db, proj, desc, prio, user_name):
                msg_confirmacao = (
                    "✅ *Reporte Enviado com Sucesso!*\n\n"
                    f"📂 *Projeto:* {proj}\n"
                    f"👤 *Enviado por:* {user_name}\n"
                    f"⚡ *Prioridade:* {prio}\n"
                    f"📝 *Descrição:* {desc}\n\n"
                    "Seu card já foi adicionado ao backlog no Notion.\n\n"
                    "Para realizar um novo reporte, basta enviar qualquer mensagem! 💬"
                )
                send_whapi_text(chat_id, msg_confirmacao)
            else:
                send_whapi_text(chat_id, "❌ Erro ao enviar para o Notion. Verifique as conexões e tente novamente.")
            
            # Limpa o estado no Redis
            r.delete(state_key, f"data:{chat_id}:proj", f"data:{chat_id}:desc")

    return {"status": "ok"}

# Endpoint de teste
@app.get("/")
async def root():
    return {"status": "Bot de reportes ativo!", "version": "2.0"}
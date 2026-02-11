import os
import redis
import psycopg2
from psycopg2 import pool
import requests
import json
from fastapi import FastAPI, Request

app = FastAPI()

# --- Configurações ---
DB_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")

# Pool de conexões PostgreSQL
connection_pool = None
try:
    connection_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DB_URL)
    print("✅ Pool de conexões PostgreSQL criado")
except Exception as e:
    print(f"❌ Erro ao criar pool: {e}")

# Redis com proteção
try:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()
    print("✅ Redis conectado")
except Exception as e:
    print(f"⚠️ Redis falhou: {e}")
    r = None

# ====================== FUNÇÕES DE BANCO ======================

def get_db_connection():
    """Obtém conexão do pool"""
    if connection_pool:
        return connection_pool.getconn()
    else:
        return psycopg2.connect(DB_URL)


def release_db_connection(conn):
    """Devolve conexão ao pool"""
    if connection_pool:
        connection_pool.putconn(conn)
    else:
        conn.close()


def get_project_notion_id(project_name):
    """Busca o notion_id de um projeto pelo nome"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT notion_id FROM public.projetos WHERE nome = %s", (project_name,))
        result = cur.fetchone()
        cur.close()
        return result[0] if result else None
    except Exception as e:
        print(f"❌ ERRO ao buscar projeto: {e}")
        return None
    finally:
        if conn:
            release_db_connection(conn)


def log_report_to_neon(proj, user, desc, prio, chat_id):
    """Salva o reporte no banco de dados"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        query = """
            INSERT INTO public.reportes_log 
            (projeto_nome, usuario, descricao, prioridade, chat_id, notion_card_created, created_at)
            VALUES (%s, %s, %s, %s, %s, FALSE, NOW())
            RETURNING id
        """
        
        print(f"💾 Salvando reporte: {proj} | {user} | {prio}")
        
        cur.execute(query, (proj, user, desc, prio, chat_id))
        report_id = cur.fetchone()[0]
        
        # COMMIT é essencial!
        conn.commit()
        cur.close()
        
        print(f"✅ Reporte #{report_id} salvo com sucesso!")
        return report_id
        
    except Exception as e:
        print(f"❌ ERRO ao salvar reporte: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            release_db_connection(conn)


def update_report_notion_status(report_id, success):
    """Atualiza o status de criação no Notion"""
    if not report_id:
        return
    
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE public.reportes_log SET notion_card_created = %s WHERE id = %s", 
                   (success, report_id))
        conn.commit()
        cur.close()
        print(f"✅ Notion status atualizado: {success} (ID {report_id})")
    except Exception as e:
        print(f"❌ Erro ao atualizar Notion status: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            release_db_connection(conn)


def create_notion_card(db_id, proj, desc, prio, user):
    """Cria um card no Notion"""
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

    print(f"📤 Tentando criar card no Notion (DB: {db_id})")

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        
        if response.status_code == 200:
            print(f"✅ Card criado no Notion!")
            return True
        else:
            print(f"❌ Falha Notion: {response.status_code}")
            print(f"   {response.text[:500]}")
            return False
            
    except Exception as e:
        print(f"❌ Exceção Notion: {e}")
        return False


# ====================== WHAPI ======================
def send_whapi_poll(chat_id, question, options, poll_type="proj"):
    """Envia uma enquete pelo WhatsApp"""
    url = "https://gate.whapi.cloud/messages/poll"
    payload = {
        "to": chat_id,
        "title": question,
        "options": [opt.strip()[:25] for opt in options],
        "count": 1
    }
    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json"}
    
    response = requests.post(url, headers=headers, json=payload)
    print(f"[POLL] Status: {response.status_code}")
    
    if response.status_code in (200, 201) and r:
        try:
            msg_id = response.json()["message"]["id"]
            r.set(f"poll_active:{chat_id}", msg_id, ex=1800)
            r.set(f"poll_options:{msg_id}", json.dumps(payload["options"]), ex=1800)
            r.set(f"poll_type:{msg_id}", poll_type, ex=1800)
            print(f"[POLL] Salvo Redis (ID: {msg_id})")
        except Exception as e:
            print(f"⚠️ Erro Redis poll: {e}")
    
    return True


def send_whapi_text(chat_id, text):
    """Envia mensagem de texto pelo WhatsApp"""
    url = "https://gate.whapi.cloud/messages/text"
    payload = {"to": chat_id, "body": text}
    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json"}
    
    response = requests.post(url, headers=headers, json=payload)
    print(f"[TEXT] Status: {response.status_code}")


# ====================== WEBHOOK ======================
@app.post("/webhook")
async def handle_flow(request: Request):
    data = await request.json()
    
    # Captura tanto messages quanto messages_updates
    messages = data.get("messages", []) + data.get("messages_updates", [])
    
    for item in messages:
        # Ignora mensagens próprias
        if item.get("from_me"):
            continue

        chat_id = item.get("chat_id")
        user_name = item.get("from_name", "Anônimo")
        msg_type = item.get("type", "")

        content = None

        # Captura mensagem de texto normal
        if msg_type == "text":
            content = item.get("text", {}).get("body", "").strip()
            print(f"[TEXT CAPTURADO] {content}")

        # Captura voto em poll (LÓGICA ORIGINAL)
        elif msg_type == "action":
            action = item.get("action", {})
            if action.get("type") == "vote":
                votes = action.get("votes", [])
                target = action.get("target")
                
                print(f"[VOTO DETECTADO] votes={votes}, target={target}")
                
                if votes and target:
                    vote_id = votes[0]
                    
                    # Busca os detalhes da poll
                    resp = requests.get(
                        f"https://gate.whapi.cloud/messages/{target}",
                        headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
                    )
                    
                    print(f"[POLL FETCH] Status: {resp.status_code}")
                    
                    if resp.status_code == 200:
                        poll_data = resp.json()
                        results = poll_data.get("poll", {}).get("results", [])
                        
                        print(f"[POLL RESULTS] {results}")
                        
                        for res in results:
                            if res.get("id") == vote_id:
                                content = res.get("name")
                                print(f"[✓ VOTO CAPTURADO] {content}")
                                break
                    else:
                        print(f"[❌ POLL FETCH FALHOU] {resp.text[:200]}")

        if not content:
            print(f"[IGNORADO] Tipo: {msg_type}, Sem content")
            continue

        print(f"\n{'='*60}")
        print(f"PROCESSANDO: {content}")
        print(f"Usuário: {user_name}")
        print(f"Chat: {chat_id}")
        print(f"{'='*60}\n")

        state_key = f"flow:{chat_id}"
        step = r.get(state_key) if r else None

        print(f"[STATE] Etapa atual: {step or 'INICIO'}")

        # ETAPA 1: INICIAR
        if not step:
            print("[AÇÃO] Enviando poll de projetos")
            if r:
                r.set(state_key, "SET_PROJ", ex=900)
            send_whapi_poll(
                chat_id, 
                f"Olá, *{user_name}*! 🛠️\n\nQual projeto você deseja reportar?", 
                ["Codefolio", "MentorIA"], 
                "proj"
            )

        # ETAPA 2: CAPTURAR PROJETO
        elif step == "SET_PROJ":
            print(f"[VERIFICAÇÃO] content='{content}'")
            
            if content not in ["Codefolio", "MentorIA"]:
                print("[ERRO] Projeto inválido")
                send_whapi_text(chat_id, "⚠️ Selecione uma das opções na enquete.")
                send_whapi_poll(chat_id, "Escolha o projeto:", ["Codefolio", "MentorIA"], "proj")
                continue
            
            print(f"[✓] Projeto válido: {content}")
            
            if r:
                r.set(f"data:{chat_id}:proj", content, ex=900)
                r.set(state_key, "SET_DESC", ex=900)
                print(f"[REDIS] Salvo projeto: {content}")
            
            send_whapi_text(
                chat_id, 
                f"✅ *{content}* selecionado!\n\n📝 Agora descreva o problema ou melhoria:"
            )

        # ETAPA 3: CAPTURAR DESCRIÇÃO
        elif step == "SET_DESC":
            if len(content) < 10:
                print("[ERRO] Descrição muito curta")
                send_whapi_text(chat_id, "⚠️ Descrição muito curta. Forneça mais detalhes (mínimo 10 caracteres).")
                continue
            
            print(f"[✓] Descrição válida: {content[:50]}...")
            
            if r:
                r.set(f"data:{chat_id}:desc", content, ex=900)
                r.set(state_key, "SET_PRIO", ex=900)
                print(f"[REDIS] Salva descrição")
            
            send_whapi_poll(
                chat_id, 
                "🎯 Qual a prioridade?", 
                ["High", "Medium", "Low"], 
                "prio"
            )

        # ETAPA 4: FINALIZAR
        elif step == "SET_PRIO":
            print(f"[VERIFICAÇÃO] content='{content}'")
            
            if content not in ["High", "Medium", "Low"]:
                print("[ERRO] Prioridade inválida")
                send_whapi_text(chat_id, "⚠️ Escolha uma prioridade válida.")
                send_whapi_poll(chat_id, "Qual a prioridade?", ["High", "Medium", "Low"], "prio")
                continue

            proj = r.get(f"data:{chat_id}:proj") if r else "desconhecido"
            desc = r.get(f"data:{chat_id}:desc") if r else content
            prio = content

            print(f"\n{'='*60}")
            print(f"SALVANDO REPORTE")
            print(f"Projeto: {proj}")
            print(f"Usuário: {user_name}")
            print(f"Prioridade: {prio}")
            print(f"Descrição: {desc[:100]}...")
            print(f"{'='*60}\n")

            # 1. SALVAR NO BANCO
            report_id = log_report_to_neon(proj, user_name, desc, prio, chat_id)

            if not report_id:
                send_whapi_text(
                    chat_id, 
                    "❌ Erro ao salvar reporte. Tente novamente."
                )
                if r:
                    r.delete(state_key, f"data:{chat_id}:proj", f"data:{chat_id}:desc")
                continue

            # 2. TENTAR NOTION (OPCIONAL)
            notion_ok = False
            target_db = get_project_notion_id(proj)
            
            if target_db:
                notion_ok = create_notion_card(target_db, proj, desc, prio, user_name)
                update_report_notion_status(report_id, notion_ok)
            else:
                print(f"⚠️ Notion ID não configurado para '{proj}'")

            # 3. RESPONDER USUÁRIO
            if notion_ok:
                send_whapi_text(
                    chat_id, 
                    f"✅ *Reporte #{report_id} criado!*\n\n"
                    f"📊 Projeto: {proj}\n"
                    f"🎯 Prioridade: {prio}\n"
                    f"📋 Card criado no Notion!"
                )
            else:
                send_whapi_text(
                    chat_id, 
                    f"✅ *Reporte #{report_id} salvo!*\n\n"
                    f"📊 Projeto: {proj}\n"
                    f"🎯 Prioridade: {prio}"
                )

            # 4. LIMPAR REDIS
            if r:
                r.delete(state_key, f"data:{chat_id}:proj", f"data:{chat_id}:desc")
            
            print(f"[✓ CONCLUÍDO] Reporte #{report_id}\n")

    return {"status": "ok"}


@app.get("/")
async def root():
    return {
        "status": "Bot ativo",
        "version": "3.1",
        "database": "connected" if connection_pool else "disconnected",
        "redis": "connected" if r else "disconnected"
    }


@app.get("/health")
async def health_check():
    """Endpoint de saúde"""
    db_ok = False
    redis_ok = False
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        release_db_connection(conn)
        db_ok = True
    except:
        pass
    
    try:
        if r:
            r.ping()
            redis_ok = True
    except:
        pass
    
    return {
        "database": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
        "overall": "healthy" if (db_ok and redis_ok) else "degraded"
    }


# Cleanup
@app.on_event("shutdown")
def shutdown_event():
    if connection_pool:
        connection_pool.closeall()
        print("✅ Pool fechado")
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

# Pool de conexões PostgreSQL (mais eficiente)
connection_pool = None
try:
    connection_pool = psycopg2.pool.SimpleConnectionPool(
        1, 10,  # min e max conexões
        DB_URL
    )
    print("✅ Pool de conexões PostgreSQL criado")
except Exception as e:
    print(f"❌ Erro ao criar pool PostgreSQL: {e}")

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
        
        # Query explícita com schema public
        query = "SELECT notion_id FROM public.projetos WHERE nome = %s"
        cur.execute(query, (project_name,))
        result = cur.fetchone()
        
        cur.close()
        
        if result:
            print(f"✅ Notion ID encontrado para '{project_name}': {result[0]}")
            return result[0]
        else:
            print(f"⚠️ Projeto '{project_name}' não encontrado na tabela projetos")
            return None
            
    except Exception as e:
        print(f"❌ ERRO ao buscar projeto '{project_name}': {e}")
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
        
        # Insere o reporte com schema explícito
        query = """
            INSERT INTO public.reportes_log 
            (projeto_nome, usuario, descricao, prioridade, chat_id, notion_card_created, created_at)
            VALUES (%s, %s, %s, %s, %s, FALSE, NOW())
            RETURNING id
        """
        
        print(f"📝 Salvando reporte: Projeto={proj}, Usuário={user}, Prioridade={prio}")
        
        cur.execute(query, (proj, user, desc, prio, chat_id))
        report_id = cur.fetchone()[0]
        
        # COMMIT é essencial!
        conn.commit()
        
        cur.close()
        
        print(f"✅ Reporte salvo com sucesso! ID: {report_id}")
        print(f"   Projeto: {proj}")
        print(f"   Usuário: {user}")
        print(f"   Prioridade: {prio}")
        
        return report_id
        
    except psycopg2.IntegrityError as e:
        print(f"❌ ERRO DE INTEGRIDADE: {e}")
        print(f"   Verifique se o projeto '{proj}' existe na tabela 'projetos'")
        if conn:
            conn.rollback()
        return None
        
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
        
        query = "UPDATE public.reportes_log SET notion_card_created = %s WHERE id = %s"
        cur.execute(query, (success, report_id))
        
        conn.commit()
        cur.close()
        
        print(f"✅ Status Notion atualizado: {success} (ID {report_id})")
        
    except Exception as e:
        print(f"❌ Erro ao atualizar status Notion: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            release_db_connection(conn)


def verify_project_exists(project_name):
    """Verifica se um projeto existe no banco"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        query = "SELECT nome FROM public.projetos WHERE nome = %s"
        cur.execute(query, (project_name,))
        result = cur.fetchone()
        
        cur.close()
        
        exists = result is not None
        print(f"{'✅' if exists else '❌'} Projeto '{project_name}' {'existe' if exists else 'NÃO existe'}")
        
        return exists
        
    except Exception as e:
        print(f"❌ Erro ao verificar projeto: {e}")
        return False
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
            print(f"✅ Card criado com sucesso no Notion!")
            return True
        else:
            print(f"❌ Falha ao criar card: Status {response.status_code}")
            print(f"   Resposta: {response.text[:500]}")
            return False
            
    except Exception as e:
        print(f"❌ Exceção ao chamar Notion: {e}")
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
            print(f"[POLL] Salvo no Redis (ID: {msg_id})")
        except Exception as e:
            print(f"⚠️ Erro ao salvar poll no Redis: {e}")
    
    return True


def send_whapi_text(chat_id, text):
    """Envia mensagem de texto pelo WhatsApp"""
    url = "https://gate.whapi.cloud/messages/text"
    payload = {"to": chat_id, "body": text}
    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json"}
    
    response = requests.post(url, headers=headers, json=payload)
    print(f"[TEXT] Mensagem enviada: Status {response.status_code}")


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

        # Captura mensagem de texto
        if msg_type == "text":
            content = item.get("text", {}).get("body", "").strip()

        # Captura voto em enquete
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
                                print(f"[VOTO] Opção selecionada: {content}")
                                break

        if not content:
            continue

        print(f"\n{'='*60}")
        print(f"[MENSAGEM] {content}")
        print(f"[TIPO] {msg_type}")
        print(f"[USUÁRIO] {user_name}")
        print(f"[CHAT] {chat_id}")
        print(f"{'='*60}\n")

        state_key = f"flow:{chat_id}"
        step = r.get(state_key) if r else None

        # PASSO 1: Iniciar fluxo
        if not step:
            print("[FLUXO] Iniciando novo fluxo")
            if r:
                r.set(state_key, "SET_PROJ", ex=900)
            send_whapi_poll(
                chat_id, 
                f"Olá, *{user_name}*! 🛠️\n\nQual projeto você deseja reportar?", 
                ["Codefolio", "MentorIA"], 
                "proj"
            )

        # PASSO 2: Selecionar projeto
        elif step == "SET_PROJ":
            if content not in ["Codefolio", "MentorIA"]:
                print("[FLUXO] Opção de projeto inválida")
                send_whapi_text(chat_id, "⚠️ Selecione uma opção válida na enquete.")
                send_whapi_poll(chat_id, "Escolha o projeto:", ["Codefolio", "MentorIA"], "proj")
                continue
            
            # Verifica se o projeto existe no banco
            if not verify_project_exists(content):
                send_whapi_text(chat_id, f"❌ Erro: Projeto '{content}' não encontrado no sistema. Contate o administrador.")
                if r:
                    r.delete(state_key)
                continue
            
            print(f"[FLUXO] Projeto selecionado: {content}")
            if r:
                r.set(f"data:{chat_id}:proj", content, ex=900)
                r.set(state_key, "SET_DESC", ex=900)
            
            send_whapi_text(
                chat_id, 
                f"✅ *{content}* selecionado!\n\n📝 Agora descreva o problema ou melhoria com detalhes:"
            )

        # PASSO 3: Capturar descrição
        elif step == "SET_DESC":
            if len(content) < 10:
                print("[FLUXO] Descrição muito curta")
                send_whapi_text(chat_id, "⚠️ Descrição muito curta. Forneça mais detalhes (mínimo 10 caracteres).")
                continue
            
            print(f"[FLUXO] Descrição capturada: {content[:50]}...")
            if r:
                r.set(f"data:{chat_id}:desc", content, ex=900)
                r.set(state_key, "SET_PRIO", ex=900)
            
            send_whapi_poll(
                chat_id, 
                "🎯 Qual a prioridade deste reporte?", 
                ["High", "Medium", "Low"], 
                "prio"
            )

        # PASSO 4: Finalizar com prioridade e salvar
        elif step == "SET_PRIO":
            if content not in ["High", "Medium", "Low"]:
                print("[FLUXO] Prioridade inválida")
                send_whapi_text(chat_id, "⚠️ Escolha uma prioridade válida na enquete.")
                send_whapi_poll(chat_id, "Qual a prioridade?", ["High", "Medium", "Low"], "prio")
                continue

            proj = r.get(f"data:{chat_id}:proj") if r else "desconhecido"
            desc = r.get(f"data:{chat_id}:desc") if r else content
            prio = content

            print(f"\n[SALVANDO REPORTE]")
            print(f"  Projeto: {proj}")
            print(f"  Usuário: {user_name}")
            print(f"  Prioridade: {prio}")
            print(f"  Descrição: {desc[:100]}...")

            # 1. Salvar no banco de dados
            report_id = log_report_to_neon(proj, user_name, desc, prio, chat_id)

            if not report_id:
                send_whapi_text(
                    chat_id, 
                    "❌ Erro ao salvar reporte no banco de dados. Tente novamente mais tarde."
                )
                if r:
                    r.delete(state_key, f"data:{chat_id}:proj", f"data:{chat_id}:desc")
                continue

            # 2. Tentar criar no Notion (se houver notion_id configurado)
            notion_ok = False
            target_db = get_project_notion_id(proj)
            
            if target_db:
                notion_ok = create_notion_card(target_db, proj, desc, prio, user_name)
                update_report_notion_status(report_id, notion_ok)
            else:
                print(f"⚠️ Notion ID não configurado para projeto '{proj}'")

            # 3. Responder ao usuário
            if notion_ok:
                send_whapi_text(
                    chat_id, 
                    f"✅ *Reporte #{report_id} criado com sucesso!*\n\n"
                    f"📊 Projeto: {proj}\n"
                    f"🎯 Prioridade: {prio}\n"
                    f"📋 Card criado no Notion!"
                )
            else:
                send_whapi_text(
                    chat_id, 
                    f"✅ *Reporte #{report_id} salvo com sucesso!*\n\n"
                    f"📊 Projeto: {proj}\n"
                    f"🎯 Prioridade: {prio}\n\n"
                    f"⚠️ Card no Notion será criado manualmente."
                )

            # 4. Limpar estado do Redis
            if r:
                r.delete(state_key, f"data:{chat_id}:proj", f"data:{chat_id}:desc")
            
            print(f"[FLUXO] Reporte finalizado! ID: {report_id}\n")

    return {"status": "ok"}


@app.get("/")
async def root():
    return {
        "status": "Bot ativo",
        "version": "3.0",
        "database": "connected" if connection_pool else "disconnected",
        "redis": "connected" if r else "disconnected"
    }


@app.get("/health")
async def health_check():
    """Endpoint de saúde para verificar status"""
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


# Cleanup ao desligar
@app.on_event("shutdown")
def shutdown_event():
    if connection_pool:
        connection_pool.closeall()
        print("✅ Pool de conexões fechado")
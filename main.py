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
        cur.execute("SELECT notion_id FROM public.projetos WHERE nome = %s", (project_name,))
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
            INSERT INTO public.reportes_log 
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
        if conn:
            conn.rollback()
        return None


def update_report_notion_status(report_id, success):
    if not report_id:
        return
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute("UPDATE public.reportes_log SET notion_card_created = %s WHERE id = %s", 
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

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"Notion status: {response.status_code}")
        return response.status_code == 200
    except Exception as e:
        print(f"Exceção ao chamar Notion: {e}")
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
            print(f"[TEXT] {content}")

        elif msg_type == "action":
            action = item.get("action", {})
            
            if action.get("type") == "vote":
                target = action.get("target")
                
                print(f"[VOTO] Detectado, target: {target}")
                
                if target:
                    # Busca a poll completa
                    resp = requests.get(
                        f"https://gate.whapi.cloud/messages/{target}",
                        headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
                    )
                    
                    print(f"[POLL API] Status: {resp.status_code}")
                    
                    if resp.status_code == 200:
                        poll_data = resp.json()
                        results = poll_data.get("poll", {}).get("results", [])
                        
                        print(f"[POLL RESULTS] Total: {len(results)}")
                        
                        # Busca a opção que tem count > 0 (significa que foi votada)
                        for res in results:
                            vote_count = res.get("count", 0)
                            option_name = res.get("name", "")
                            
                            print(f"  Opção: '{option_name}' - Votos: {vote_count}")
                            
                            if vote_count > 0 and not content:
                                content = option_name
                                print(f"[✅ VOTO CAPTURADO] {content}")
                    else:
                        print(f"[❌ ERRO POLL API] {resp.text[:200]}")

        if not content:
            print(f"[IGNORADO] Tipo: {msg_type}")
            continue

        print(f"\n{'='*60}")
        print(f"[PROCESSANDO] {content}")
        print(f"[USUÁRIO] {user_name}")
        print(f"[CHAT] {chat_id}")
        print(f"{'='*60}\n")

        state_key = f"flow:{chat_id}"
        step = r.get(state_key) if r else None
        
        print(f"[STATE] Etapa: {step or 'INICIO'}")

        if not step:
            print("[AÇÃO] Iniciando fluxo")
            if r:
                r.set(state_key, "SET_PROJ", ex=900)
            send_whapi_poll(chat_id, f"Olá, *{user_name}*! 🛠️\n\nQual projeto você deseja reportar?", ["Codefolio", "MentorIA"], "proj")

        elif step == "SET_PROJ":
            if content not in ["Codefolio", "MentorIA"]:
                print(f"[ERRO] Projeto inválido: {content}")
                send_whapi_text(chat_id, "Selecione uma opção na enquete.")
                send_whapi_poll(chat_id, "Escolha o projeto:", ["Codefolio", "MentorIA"], "proj")
                continue
            
            print(f"[OK] Projeto: {content}")
            if r:
                r.set(f"data:{chat_id}:proj", content, ex=900)
                r.set(state_key, "SET_DESC", ex=900)
            send_whapi_text(chat_id, f"✅ *{content}* selecionado!\n\nAgora descreva o problema ou melhoria com detalhes:")

        elif step == "SET_DESC":
            if len(content) < 10:
                print(f"[ERRO] Descrição curta")
                send_whapi_text(chat_id, "Descrição muito curta. Tente novamente.")
                continue
            
            print(f"[OK] Descrição: {content[:50]}...")
            if r:
                r.set(f"data:{chat_id}:desc", content, ex=900)
                r.set(state_key, "SET_PRIO", ex=900)
            send_whapi_poll(chat_id, "Qual a prioridade deste reporte?", ["High", "Medium", "Low"], "prio")

        elif step == "SET_PRIO":
            if content not in ["High", "Medium", "Low"]:
                print(f"[ERRO] Prioridade inválida: {content}")
                send_whapi_text(chat_id, "Escolha uma prioridade na enquete.")
                send_whapi_poll(chat_id, "Qual a prioridade?", ["High", "Medium", "Low"], "prio")
                continue

            proj = r.get(f"data:{chat_id}:proj") if r else None
            desc = r.get(f"data:{chat_id}:desc") if r else None
            prio = content
            
            print(f"\n{'='*60}")
            print(f"[SALVANDO REPORTE]")
            print(f"  Projeto: {proj}")
            print(f"  Usuário: {user_name}")
            print(f"  Prioridade: {prio}")
            print(f"  Descrição: {desc[:80] if desc else 'N/A'}...")
            print(f"{'='*60}\n")

            # 1. Salva no Neon
            report_id = log_report_to_neon(proj or "desconhecido", user_name, desc or content, prio, chat_id)

            if not report_id:
                print(f"[❌] Falha ao salvar no banco")
                send_whapi_text(chat_id, "❌ Erro ao salvar reporte. Tente novamente.")
                if r:
                    r.delete(state_key, f"data:{chat_id}:proj", f"data:{chat_id}:desc")
                continue

            # 2. Tenta criar no Notion
            notion_ok = False
            target_db = get_project_notion_id(proj or "")
            if target_db:
                notion_ok = create_notion_card(target_db, proj or "", desc or "", prio, user_name)

            update_report_notion_status(report_id, notion_ok)

            # 3. Responde usuário
            if notion_ok:
                send_whapi_text(chat_id, f"✅ Reporte #{report_id} criado com sucesso!\n\n📊 Projeto: {proj}\n🎯 Prioridade: {prio}\n📋 Card criado no Notion!")
            else:
                send_whapi_text(chat_id, f"✅ Reporte #{report_id} salvo com sucesso!\n\n📊 Projeto: {proj}\n🎯 Prioridade: {prio}")

            # 4. Limpa Redis
            if r:
                r.delete(state_key, f"data:{chat_id}:proj", f"data:{chat_id}:desc")
            
            print(f"[✅ CONCLUÍDO] Reporte #{report_id}\n")

    return {"status": "ok"}


@app.get("/")
async def root():
    return {"status": "Bot ativo - v3.2 (poll fix)"}
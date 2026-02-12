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
        cur.execute("SELECT notion_id FROM projetos WHERE nome = %s", (project_name,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return result[0] if result else None
    except Exception as e:
        print(f"ERRO NEON (projetos): {e}")
        return None


def log_report_to_neon(proj, user, desc, prio, chat_id):
    conn = None
    try:
        print(f"\n[DB] Salvando reporte no banco...")
        print(f"  Projeto: {proj}")
        print(f"  Usuário: {user}")
        print(f"  Prioridade: {prio}")
        
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        
        cur.execute("""
            INSERT INTO reportes_log 
            (projeto_nome, usuario, descricao, prioridade, chat_id, notion_card_created, created_at)
            VALUES (%s, %s, %s, %s, %s, FALSE, NOW())
            RETURNING id
        """, (proj, user, desc, prio, chat_id))
        
        report_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        
        print(f"✅ Reporte #{report_id} salvo com sucesso!")
        return report_id
        
    except Exception as e:
        print(f"❌ ERRO ao salvar reporte: {e}")
        if conn:
            conn.rollback()
            conn.close()
        return None


def update_report_notion_status(report_id, success):
    if not report_id:
        return
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute("UPDATE reportes_log SET notion_card_created = %s WHERE id = %s", 
                   (success, report_id))
        conn.commit()
        cur.close()
        conn.close()
        print(f"✅ Notion status atualizado: {success}")
    except Exception as e:
        print(f"Erro ao atualizar Notion status: {e}")


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

    print(f"📤 Criando card no Notion...")

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        success = response.status_code == 200
        print(f"{'✅' if success else '❌'} Notion: {response.status_code}")
        return success
    except Exception as e:
        print(f"❌ Erro Notion: {e}")
        return False


# ====================== WHAPI ======================
def send_whapi_text(chat_id, text):
    url = "https://gate.whapi.cloud/messages/text"
    payload = {"to": chat_id, "body": text}
    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json"}
    response = requests.post(url, headers=headers, json=payload)
    print(f"[MSG] Enviada: {response.status_code}")


# ====================== WEBHOOK ======================
@app.post("/webhook")
async def handle_flow(request: Request):
    data = await request.json()
    
    messages = data.get("messages", [])
    
    for item in messages:
        # Ignora mensagens próprias
        if item.get("from_me"):
            continue

        # Só processa mensagens de texto
        if item.get("type") != "text":
            continue

        chat_id = item.get("chat_id")
        user_name = item.get("from_name", "Anônimo")
        content = item.get("text", {}).get("body", "").strip()

        if not content:
            continue

        print(f"\n{'='*70}")
        print(f"📩 MENSAGEM: {content}")
        print(f"👤 USUÁRIO: {user_name}")
        print(f"💬 CHAT: {chat_id}")
        print(f"{'='*70}")

        state_key = f"flow:{chat_id}"
        step = r.get(state_key) if r else None
        
        print(f"📍 ETAPA ATUAL: {step or 'INICIO'}")

        # ========== ETAPA 1: INICIAR ==========
        if not step:
            print("[AÇÃO] Iniciando novo fluxo")
            if r:
                r.set(state_key, "SET_PROJ", ex=900)
            
            send_whapi_text(
                chat_id, 
                f"Olá, *{user_name}*! 🛠️\n\n"
                f"Qual projeto você deseja reportar?\n\n"
                f"*1* - Codefolio 💻\n"
                f"*2* - MentorIA 🤖\n\n"
                f"_Digite apenas o número (1 ou 2)_"
            )

        # ========== ETAPA 2: ESCOLHER PROJETO ==========
        elif step == "SET_PROJ":
            projeto = None
            
            if content == "1":
                projeto = "Codefolio"
            elif content == "2":
                projeto = "MentorIA"
            else:
                print(f"[ERRO] Resposta inválida: {content}")
                send_whapi_text(
                    chat_id,
                    "❌ *Resposta inválida!*\n\n"
                    "Por favor, responda apenas com o *número*:\n\n"
                    "*1* - Codefolio\n"
                    "*2* - MentorIA"
                )
                continue
            
            print(f"✅ Projeto selecionado: {projeto}")
            
            if r:
                r.set(f"data:{chat_id}:proj", projeto, ex=900)
                r.set(state_key, "SET_DESC", ex=900)
            
            send_whapi_text(
                chat_id,
                f"✅ *{projeto}* selecionado!\n\n"
                f"📝 Agora, descreva o problema ou melhoria:\n\n"
                f"_Seja claro e detalhado (mínimo 10 caracteres)_"
            )

        # ========== ETAPA 3: CAPTURAR DESCRIÇÃO ==========
        elif step == "SET_DESC":
            if len(content) < 10:
                print(f"[ERRO] Descrição muito curta: {len(content)} caracteres")
                send_whapi_text(
                    chat_id,
                    "❌ *Descrição muito curta!*\n\n"
                    "Por favor, forneça mais detalhes sobre o problema ou melhoria.\n"
                    "_Mínimo: 10 caracteres_"
                )
                continue
            
            print(f"✅ Descrição capturada ({len(content)} caracteres)")
            
            if r:
                r.set(f"data:{chat_id}:desc", content, ex=900)
                r.set(state_key, "SET_PRIO", ex=900)
            
            send_whapi_text(
                chat_id,
                "📊 Qual a prioridade deste reporte?\n\n"
                "*1* - 🔴 Alta (High)\n"
                "*2* - 🟡 Média (Medium)\n"
                "*3* - 🟢 Baixa (Low)\n\n"
                "_Digite apenas o número (1, 2 ou 3)_"
            )

        # ========== ETAPA 4: FINALIZAR COM PRIORIDADE ==========
        elif step == "SET_PRIO":
            prioridade = None
            
            if content == "1":
                prioridade = "High"
                emoji_prio = "🔴"
            elif content == "2":
                prioridade = "Medium"
                emoji_prio = "🟡"
            elif content == "3":
                prioridade = "Low"
                emoji_prio = "🟢"
            else:
                print(f"[ERRO] Prioridade inválida: {content}")
                send_whapi_text(
                    chat_id,
                    "❌ *Resposta inválida!*\n\n"
                    "Por favor, responda apenas com o *número*:\n\n"
                    "*1* - Alta\n"
                    "*2* - Média\n"
                    "*3* - Baixa"
                )
                continue
            
            # Recupera dados do Redis
            projeto = r.get(f"data:{chat_id}:proj") if r else "Desconhecido"
            descricao = r.get(f"data:{chat_id}:desc") if r else "Sem descrição"
            
            print(f"\n{'='*70}")
            print(f"💾 SALVANDO REPORTE COMPLETO")
            print(f"{'='*70}")
            print(f"Projeto: {projeto}")
            print(f"Usuário: {user_name}")
            print(f"Prioridade: {prioridade}")
            print(f"Descrição: {descricao[:100]}...")
            print(f"{'='*70}\n")

            # 1. SALVAR NO BANCO DE DADOS
            report_id = log_report_to_neon(
                projeto, 
                user_name, 
                descricao, 
                prioridade, 
                chat_id
            )

            if not report_id:
                print("❌ FALHA CRÍTICA ao salvar no banco")
                send_whapi_text(
                    chat_id,
                    "❌ *Erro ao salvar reporte!*\n\n"
                    "Ocorreu um problema técnico. Por favor, tente novamente mais tarde.\n\n"
                    "_Se o problema persistir, entre em contato com o suporte._"
                )
                # Limpar estado
                if r:
                    r.delete(state_key, f"data:{chat_id}:proj", f"data:{chat_id}:desc")
                continue

            # 2. TENTAR CRIAR NO NOTION (OPCIONAL)
            notion_ok = False
            notion_db = get_project_notion_id(projeto)
            
            if notion_db:
                print(f"📋 Tentando criar card no Notion...")
                notion_ok = create_notion_card(notion_db, projeto, descricao, prioridade, user_name)
                update_report_notion_status(report_id, notion_ok)
            else:
                print(f"⚠️ Notion ID não configurado para {projeto}")

            # 3. ENVIAR CONFIRMAÇÃO DETALHADA
            emoji_projeto = "💻" if projeto == "Codefolio" else "🤖"
            
            mensagem_confirmacao = (
                f"✅ *REPORTE REGISTRADO COM SUCESSO!*\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 *ID do Reporte:* #{report_id}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{emoji_projeto} *Projeto:* {projeto}\n"
                f"{emoji_prio} *Prioridade:* {prioridade}\n"
                f"👤 *Reportado por:* {user_name}\n"
                f"📝 *Descrição:*\n_{descricao[:200]}{'...' if len(descricao) > 200 else ''}_\n\n"
            )
            
            if notion_ok:
                mensagem_confirmacao += "✅ Card criado no Notion!\n\n"
            
            mensagem_confirmacao += (
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"_Seu reporte foi registrado e será analisado pela equipe._\n\n"
                f"Obrigado por contribuir! 🙏"
            )
            
            send_whapi_text(chat_id, mensagem_confirmacao)

            # 4. LIMPAR ESTADO DO REDIS
            if r:
                r.delete(state_key, f"data:{chat_id}:proj", f"data:{chat_id}:desc")
            
            print(f"✅ FLUXO CONCLUÍDO - Reporte #{report_id}\n")

    return {"status": "ok"}


@app.get("/")
async def root():
    return {
        "status": "Bot ativo",
        "version": "4.0 - Sistema simplificado com números",
        "redis": "connected" if r else "disconnected"
    }


@app.get("/health")
async def health():
    """Endpoint de saúde"""
    db_ok = False
    redis_ok = False
    
    # Testa banco
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM reportes_log")
        total = cur.fetchone()[0]
        cur.close()
        conn.close()
        db_ok = True
        db_info = f"{total} reportes"
    except Exception as e:
        db_info = str(e)[:100]
    
    # Testa Redis
    try:
        if r:
            r.ping()
            redis_ok = True
    except:
        pass
    
    return {
        "database": "ok" if db_ok else "error",
        "database_info": db_info if db_ok else db_info,
        "redis": "ok" if redis_ok else "error",
        "status": "healthy" if (db_ok and redis_ok) else "degraded"
    }
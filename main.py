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

# --- Conexão com Redis ---
try:
    r = redis.from_url(REDIS_URL, decode_responses=True)
except Exception as e:
    print(f"⚠️ Redis não conectado: {e}")
    r = None

# --- Configurações do Fluxo ---
OPCOES_PROJETOS = {"1": "Codefolio", "2": "MentorIA"}
OPCOES_PRIORIDADE = {"1": "High", "2": "Medium", "3": "Low"}

# --- Função Auxiliar: Whapi ---
def send_whapi_message(chat_id, text):
    url = "https://gate.whapi.cloud/messages/text"
    payload = {"to": chat_id, "body": text}
    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json"}
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print(f"❌ Erro ao enviar mensagem: {e}")

# --- Garantir Tabelas no Banco ---
def init_db():
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        # Criar tabela de projetos
        cur.execute("""
            CREATE TABLE IF NOT EXISTS projetos (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(100) NOT NULL UNIQUE
            );
        """)
        # Inserir projetos iniciais se não existirem
        for p in OPCOES_PROJETOS.values():
            cur.execute("INSERT INTO projetos (nome) VALUES (%s) ON CONFLICT (nome) DO NOTHING", (p,))
        
        # Criar tabela de reportes
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reportes_log (
                id SERIAL PRIMARY KEY,
                projeto_nome VARCHAR(100) NOT NULL,
                usuario VARCHAR(255) NOT NULL,
                descricao TEXT NOT NULL,
                prioridade VARCHAR(20) NOT NULL,
                chat_id VARCHAR(100) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Banco de dados sincronizado!")
    except Exception as e:
        print(f"❌ Erro ao inicializar banco: {e}")

# Inicializa o banco ao subir o app
init_db()

@app.get("/")
def home():
    return {"status": "Bot de Reportes Ativo 🚀"}

@app.post("/webhook")
async def handle_webhook(request: Request):
    try:
        data = await request.json()
        messages = data.get("messages", [])
        if not messages: return {"status": "no messages"}

        for msg in messages:
            if msg.get("from_me"): continue

            chat_id = msg.get("chat_id")
            user_name = msg.get("from_name", "Anônimo")
            text = msg.get("text", {}).get("body", "").strip()
            if not text: continue

            # Chaves do Redis
            state_key = f"report:{chat_id}:step"
            data_key = f"report:{chat_id}:data"

            step = r.get(state_key) if r else None

            # --- FLUXO: INÍCIO ---
            if not step:
                r.set(state_key, "AGUARDANDO_PROJETO", ex=600)
                msg_ini = (
                    f"Olá, *{user_name}*! 🛠️\n\n"
                    "Qual projeto você quer reportar?\n"
                    "1️⃣ - Codefolio\n"
                    "2️⃣ - MentorIA\n\n"
                    "_(Responda apenas o número)_"
                )
                send_whapi_message(chat_id, msg_ini)

            # --- FLUXO: SELECIONAR PROJETO ---
            elif step == "AGUARDANDO_PROJETO":
                if text in OPCOES_PROJETOS:
                    projeto = OPCOES_PROJETOS[text]
                    r.hset(data_key, "projeto", projeto)
                    r.set(state_key, "AGUARDANDO_DESCRICAO", ex=600)
                    send_whapi_message(chat_id, f"✅ *{projeto}* selecionado!\n\nAgora, descreva o problema (mínimo 10 caracteres):")
                else:
                    send_whapi_message(chat_id, "❌ Opção inválida. Escolha 1 ou 2.")

            # --- FLUXO: CAPTURAR DESCRIÇÃO ---
            elif step == "AGUARDANDO_DESCRICAO":
                if len(text) >= 10:
                    r.hset(data_key, "descricao", text)
                    r.set(state_key, "AGUARDANDO_PRIORIDADE", ex=600)
                    msg_prio = (
                        "Qual a prioridade?\n"
                        "1️⃣ - 🔴 Alta\n"
                        "2️⃣ - 🟡 Média\n"
                        "3️⃣ - 🟢 Baixa"
                    )
                    send_whapi_message(chat_id, msg_prio)
                else:
                    send_whapi_message(chat_id, "⚠️ Muito curto! Detalhe um pouco mais o problema.")

            # --- FLUXO: FINALIZAR E SALVAR ---
            elif step == "AGUARDANDO_PRIORIDADE":
                if text in OPCOES_PRIORIDADE:
                    prio = OPCOES_PRIORIDADE[text]
                    report_data = r.hgetall(data_key)
                    
                    # Salvar no Postgres
                    try:
                        conn = psycopg2.connect(DB_URL)
                        cur = conn.cursor()
                        cur.execute("""
                            INSERT INTO reportes_log (projeto_nome, usuario, descricao, prioridade, chat_id)
                            VALUES (%s, %s, %s, %s, %s) RETURNING id
                        """, (report_data['projeto'], user_name, report_data['descricao'], prio, chat_id))
                        report_id = cur.fetchone()[0]
                        conn.commit()
                        cur.close()
                        conn.close()

                        # Feedback final
                        msg_fim = (
                            f"✅ *Reporte #{report_id} salvo!*\n\n"
                            f"Projeto: {report_data['projeto']}\n"
                            f"Prioridade: {prio}\n"
                            "Obrigado pelo feedback! 🚀"
                        )
                        send_whapi_message(chat_id, msg_fim)
                    except Exception as e:
                        print(f"❌ Erro ao salvar: {e}")
                        send_whapi_message(chat_id, "❌ Erro ao salvar no banco. Tente novamente.")
                    
                    # Limpa Redis
                    r.delete(state_key, data_key)
                else:
                    send_whapi_message(chat_id, "❌ Escolha 1, 2 ou 3.")

    except Exception as e:
        print(f"❌ Erro geral: {e}")
    
    return {"status": "ok"}
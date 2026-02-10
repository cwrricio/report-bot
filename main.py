from fastapi import FastAPI, Request
import os
import redis
import psycopg2
from psycopg2 import OperationalError
import requests
import json

app = FastAPI()

# --- Configurações e Variáveis de Ambiente ---
DB_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN") 

# --- Conexão com Redis ---
try:
    # decode_responses=True faz o Redis devolver strings (texto) ao invés de bytes
    r = redis.from_url(REDIS_URL, decode_responses=True)
except Exception as e:
    print(f"Aviso: Redis não conectado: {e}")
    r = None

# --- Opções da Enquete (Atualizado) ---
# O texto à direita é o que será salvo no banco e enviado na resposta
OPCOES = {
    "1": "Bundudo", 
    "2": "Divo", 
    "3": "Divo e Bundudo" 
}

# --- Função Auxiliar: Enviar Mensagem via Whapi ---
def send_whapi_message(chat_id, message):
    """Envia mensagem de texto usando a API da Whapi.cloud"""
    url = "https://gate.whapi.cloud/messages/text"
    
    headers = {
        "Authorization": f"Bearer {WHAPI_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "to": chat_id, 
        "body": message, 
        "typing_time": 0
    }
    
    try:
        # Enviamos o POST para a Whapi
        response = requests.post(url, headers=headers, json=payload)
        
        # Se der erro (400 ou 500), avisamos no log
        if response.status_code >= 400:
            print(f"❌ Erro Whapi ({response.status_code}): {response.text}")
    except Exception as e:
        print(f"❌ Erro de conexão ao enviar: {e}")

# --- Rota Inicial (Health Check básico) ---
@app.get("/")
def home():
    return {"status": "Bot Whapi do Ciocca Online 🚀"}

@app.get("/health")
def health_check():
    # Retorno simples para o Railway saber que o app não travou
    return {"status": "ok"}

# --- ROTA DO WEBHOOK (Cérebro do Bot) ---
@app.post("/webhook")
async def receive_webhook(request: Request):
    try:
        # Lê o JSON que a Whapi mandou
        body = await request.json()
        
        # A Whapi envia uma lista de mensagens dentro de 'messages'
        messages = body.get("messages", [])
        
        # Se a lista estiver vazia (pode ser atualização de status), ignoramos
        if not messages:
            return {"status": "ignored"}

        for message_data in messages:
            # 1. Ignora mensagens enviadas por mim mesmo (para não entrar em loop)
            if message_data.get("from_me"):
                continue

            # 2. Extrai dados importantes
            chat_id = message_data.get("chat_id") # ID único do usuário (ex: 5511999...@s.whatsapp.net)
            text_body = message_data.get("text", {}).get("body", "") # O texto da mensagem
            nome = message_data.get("from_name", "Anônimo") # Nome do contato
            
            # Se não tiver texto (ex: mandou só foto sem legenda), ignora
            if not text_body:
                continue

            texto = text_body.strip().lower() # Limpa espaços e deixa minúsculo

            print(f"📩 Recebido de {nome}: {texto}")

            # --- MÁQUINA DE ESTADOS (REDIS) ---
            if r:
                # Cria uma chave única para esse usuário
                state_key = f"voto:{chat_id}:status"
                
                # Pergunta ao Redis: "Em que passo esse cara está?"
                estado_atual = r.get(state_key)

                # CENÁRIO A: Usuário Novo (Não tem estado no Redis)
                # Qualquer coisa que ele mandar, o bot inicia a enquete.
                if not estado_atual:
                    # Salva no Redis que ele está "pensando" (expira em 300 segundos/5 min)
                    r.set(state_key, "AGUARDANDO_VOTO", ex=300)
                    
                    msg = (
                        "🧐 *Enquete Oficial*\n"
                        "O que o Ciocca é?\n\n"
                        "1️⃣ - Bundudo\n"
                        "2️⃣ - Divo\n"
                        "3️⃣ - Bundudo e Divo\n\n"
                        "_(Por Favor responda APENAS com o número)_"
                    )
                    send_whapi_message(chat_id, msg)
                
                # CENÁRIO B: Usuário já recebeu a pergunta (Estado AGUARDANDO_VOTO)
                elif estado_atual == "AGUARDANDO_VOTO":
                    
                    # Verifica se ele respondeu 1, 2 ou 3
                    if texto in OPCOES:
                        escolha = OPCOES[texto] # Pega o texto bonito ("Divo", "Bundudo", etc)
                        
                        # --- SALVA NO BANCO (POSTGRES) ---
                        try:
                            conn = psycopg2.connect(DB_URL)
                            cur = conn.cursor()
                            # Cria a tabela se não existir (segurança)
                            cur.execute("""
                                CREATE TABLE IF NOT EXISTS votos_ciocca (
                                    id SERIAL PRIMARY KEY, 
                                    nome VARCHAR(100), 
                                    voto VARCHAR(100), 
                                    data TIMESTAMP DEFAULT NOW()
                                )
                            """)
                            # Insere o voto
                            cur.execute("INSERT INTO votos_ciocca (nome, voto) VALUES (%s, %s)", (nome, escolha))
                            conn.commit()
                            cur.close()
                            conn.close()
                            print(f"✅ Voto salvo: {escolha}")
                        except Exception as e:
                            print(f"❌ Erro Postgres: {e}")

                        # --- ENCERRAMENTO ---
                        # Deleta a chave do Redis. O bot "esquece" que estava falando com ele.
                        # Assim, a sessão encerra e não ocupa memória à toa.
                        r.delete(state_key)
                        
                        # Envia confirmação final
                        send_whapi_message(chat_id, f"✅ Registrado! O Ciocca é *{escolha}*.")
                    
                    else:
                        # Se ele estava aguardando voto mas mandou "batata" ou "4"
                        send_whapi_message(chat_id, "❌ Opção inválida! Digite apenas 1, 2 ou 3.")

    except Exception as e:
        print(f"❌ Erro Geral no Webhook: {e}")
        return {"status": "error"}
    
    return {"status": "ok"}
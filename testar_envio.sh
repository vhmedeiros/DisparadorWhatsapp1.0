#!/usr/bin/env bash
#
# Teste de ENVIO para um grupo.
# Uso:  ./testar_envio.sh [sessionId] [nomeDoGrupo] [mensagem]
#   padrões: teste / Canelinhas / mensagem de teste
#
# Reconecta usando as credenciais salvas (auth_info_<sessao>) - NÃO pede QR.
#
set -uo pipefail

SESSION_ID="${1:-teste}"
GROUP_NAME="${2:-Canelinhas}"
MESSAGE="${3:-[TESTE] Mensagem automatica do DisparadorWhatsapp - pode ignorar.}"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

export NVM_DIR="$HOME/.nvm"
# shellcheck disable=SC1091
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm use >/dev/null 2>&1
echo "Node: $(node -v)"

pkill -f "server_baileys_pro.mjs" 2>/dev/null && { echo "Backend anterior encerrado."; sleep 1; }

echo "Subindo backend... (saída bruta em logs/server-stdout.log)"
mkdir -p logs
node server_baileys_pro.mjs >> logs/server-stdout.log 2>&1 &
SERVER_PID=$!
trap 'echo; echo "Encerrando backend (PID $SERVER_PID)..."; kill "$SERVER_PID" 2>/dev/null' EXIT
sleep 2

jget() { python3 -c "import sys,json;print(json.load(sys.stdin).get('$1') or '')" 2>/dev/null; }

echo "Iniciando sessão '$SESSION_ID' (reconexão pelas credenciais salvas)..."
curl -s -X POST http://127.0.0.1:3000/session/start \
  -H 'Content-Type: application/json' -d "{\"sessionId\":\"$SESSION_ID\"}" >/dev/null

echo "Aguardando a sessão ficar 'open'..."
ST=""
for i in $(seq 1 24); do
  RESP=$(curl -s -X POST http://127.0.0.1:3000/session/state \
    -H 'Content-Type: application/json' -d "{\"sessionId\":\"$SESSION_ID\"}")
  ST=$(printf '%s' "$RESP" | jget state)
  echo "  [$i] estado: $ST"
  [ "$ST" = "open" ] && break
  if [ "$ST" = "loggedOut" ]; then
    echo "Sessão deslogada - precisaria escanear novo QR. Abortando."
    exit 1
  fi
  sleep 5
done
[ "$ST" = "open" ] || { echo "Timeout aguardando sessão abrir. Abortando."; exit 1; }

KEY=$(printf '%s' "$GROUP_NAME" | tr '[:upper:]' '[:lower:]')
echo "Aguardando o grupo '$GROUP_NAME' entrar no cache (sync de grupos)..."
JID=""
for i in $(seq 1 12); do
  JIDS=$(curl -s http://127.0.0.1:3000/group_jids)
  JID=$(printf '%s' "$JIDS" | python3 -c "import sys,json;print(json.load(sys.stdin).get('$KEY',''))" 2>/dev/null)
  [ -n "$JID" ] && break
  CNT=$(printf '%s' "$JIDS" | python3 -c "import sys,json;print(len(json.load(sys.stdin)))" 2>/dev/null)
  echo "  [$i] ainda não encontrado... ($CNT grupos em cache)"
  sleep 5
done

if [ -z "$JID" ]; then
  echo "ERRO: grupo '$GROUP_NAME' não encontrado no cache."
  echo "Grupos disponíveis (até 40):"
  curl -s http://127.0.0.1:3000/group_jids | python3 -c "import sys,json;[print('  -',k) for k in sorted(json.load(sys.stdin))[:40]]"
  exit 1
fi
echo "Grupo encontrado: '$GROUP_NAME' -> $JID"
echo "Enviando: \"$MESSAGE\""

curl -s -X POST http://127.0.0.1:3000/send_message \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c "import json,sys;print(json.dumps({'sessionId':sys.argv[1],'jid':sys.argv[2],'message':sys.argv[3]}))" "$SESSION_ID" "$GROUP_NAME" "$MESSAGE")"
echo

echo "Aguardando confirmação no log (aguarde 'Sucesso no envio')..."
sleep 8
echo "--- últimas linhas relevantes do log ---"
grep -E "Enviando para|Sucesso no envio|Erro no envio|ENVIO IGNORADO|Usando JID" logs/app.*.log | tail -n 15 || tail -n 20 logs/app.*.log

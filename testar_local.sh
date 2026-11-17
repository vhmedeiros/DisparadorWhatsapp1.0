#!/usr/bin/env bash
#
# Teste local do backend Baileys (WSL/Linux).
# Uso:  ./testar_local.sh [sessionId]
#   1. Sobe o servidor Node (server_baileys_pro.mjs) em Node 24.
#   2. Inicia a sessão informada (padrão: "teste").
#   3. Quando o QR é gerado, abre o link do QR no navegador do Windows.
#   4. Fica monitorando o estado a cada 5s até você parar com Ctrl+C.
#
set -uo pipefail

SESSION_ID="${1:-teste}"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# Carrega Node 24 (lê o .nvmrc)
export NVM_DIR="$HOME/.nvm"
# shellcheck disable=SC1091
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm use >/dev/null 2>&1
echo "Node: $(node -v)"

# Encerra um backend antigo, se houver
pkill -f "server_baileys_pro.mjs" 2>/dev/null && { echo "Backend anterior encerrado."; sleep 1; }

echo "Subindo backend... (saída bruta em logs/server-stdout.log)"
mkdir -p logs
node server_baileys_pro.mjs >> logs/server-stdout.log 2>&1 &
SERVER_PID=$!
trap 'echo; echo "Encerrando backend (PID $SERVER_PID)..."; kill "$SERVER_PID" 2>/dev/null' EXIT

sleep 2

parse() {
  # $1 = campo JSON desejado; lê o JSON do stdin
  python3 -c "import sys,json;print(json.load(sys.stdin).get('$1') or '')" 2>/dev/null
}

OPENED=0
while true; do
  RESP=$(curl -s -X POST http://127.0.0.1:3000/session/start \
    -H 'Content-Type: application/json' -d "{\"sessionId\":\"$SESSION_ID\"}")

  STATE=$(printf '%s' "$RESP" | parse state)
  QR=$(printf '%s' "$RESP" | parse qrCode)

  case "$STATE" in
    open)       MSG="CONECTADO! Deixe rodando e observe logs/app.*.log" ;;
    qr_required) MSG="Aguardando leitura do QR Code" ;;
    connecting) MSG="Conectando..." ;;
    closed)     MSG="Fechado - aguardando reconexão (delay crescente)" ;;
    loggedOut)  MSG="Desconectado permanentemente - precisa de novo QR" ;;
    *)          MSG="Estado: '$STATE' (resposta: $RESP)" ;;
  esac
  echo "[$(date +%H:%M:%S)] Sessão '$SESSION_ID' -> $MSG"

  if [ "$STATE" = "qr_required" ] && [ "$OPENED" = "0" ] && [ -n "$QR" ]; then
    URL=$(python3 -c "import urllib.parse,sys;print('https://api.qrserver.com/v1/create-qr-code/?size=300x300&data='+urllib.parse.quote(sys.argv[1]))" "$QR")
    echo "    >>> Abrindo QR no navegador. Se não abrir, acesse:"
    echo "    $URL"
    /mnt/c/Windows/explorer.exe "$URL" >/dev/null 2>&1 || true
    OPENED=1
  fi

  sleep 5
done

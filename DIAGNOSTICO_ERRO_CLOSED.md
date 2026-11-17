# Diagnóstico — Erro `closed` no DisparadorWhatsapp

**Data do diagnóstico:** 2026-11-17  
**Feito por:** Análise automatizada dos logs e código-fonte  
**Arquivo principal afetado:** `server_baileys_pro.mjs`  
**Pacote afetado:** `@whiskeysockets/baileys` (atualmente `6.7.21`)

---

## Contexto

O sistema roda 24h/dia no Windows Server 2019, disparando mensagens para grupos de WhatsApp usando múltiplas contas simultaneamente via Baileys. As sessões passaram a ficar presas no estado `closed`, impossibilitando envios.

Uma tentativa anterior de correção alterou:
```js
// DE:
const version = [6, 7, 1];
// PARA:
const version = [2, 3000, 1043857760];
```
Essa mudança estava **na direção certa** mas **não foi suficiente** (ver Causa #1 abaixo).

---

## Evidências nos Logs

| Arquivo | Tamanho | Linhas |
|---|---|---|
| `logs-backend.txt` | ~grande | 1.811.431 linhas |
| `logs-backend-error.txt` | 248 MB | 2.354.561 linhas |

**Padrão identificado no log:**
```
[WhatsClipper] Conexão fechada. Motivo: 428. Reconectar? true   ← funcionava
[WhatsClipper] Iniciando sessão com versão fixa: 6.7.1
[WhatsClipper] Conectado com sucesso!                           ← conectou normalmente
...
[WhatsClipper] Conexão fechada. Motivo: 408. Reconectar? true   ← entrou em loop
[WhatsClipper] Iniciando sessão com versão fixa: 6.7.1
[WhatsClipper] Conexão fechada. Motivo: 408. Reconectar? true   ← nunca conecta mais
... (repete indefinidamente)
```

- **84.518 ocorrências** do erro `408` nos logs de erro  
- **4.569 reconexões com código 428** (normal — essas funcionavam)  
- **6.694 conexões bem-sucedidas** (antes do problema começar)

**Código de erro 408** = `DisconnectReason.connectionLost` (timeout de keepalive)

---

## Causas Identificadas

---

### 🔴 Causa #1 — Versão do pacote Baileys desatualizada (causa raiz principal)

**Versão instalada:** `6.7.21`  
**Versão mais recente estável (legacy):** `6.7.24`  
**Versão do protocolo no pacote instalado:** `[2, 3000, 1023223821]` (em `baileys-version.json`)  
**Versão do protocolo atual (GitHub/6.7.24):** `[2, 3000, 1043857760]`

A mudança de versão de protocolo feita no código (`[2, 3000, 1043857760]`) estava correta — essa é a versão que o WhatsApp Web exige atualmente. Porém, o pacote `6.7.21` foi lançado **antes** dessa versão de protocolo e contém bugs de handshake associados. A versão `6.7.24` já inclui o protocolo correto **e** as correções de handshake correspondentes.

**Analogia:** A chave (versão do protocolo) está certa, mas a fechadura (código do Baileys) está enferrujada.

**Verificar:**
```
npm show @whiskeysockets/baileys dist-tags
# Resultado: { latest: '7.0.0-rc14', legacy: '6.7.24' }
```

A versão `7.x` é uma release candidate (RC) — **não usar em produção ainda**.

---

### 🔴 Causa #2 — Loop de reconexão sem backoff exponencial (reconnect storm)

**Código atual problemático:**
```js
// server_baileys_pro.mjs, linha 153:
setTimeout(() => startSession(sessionId), 5000); // fixo em 5 segundos
```

Quando todas as sessões falham simultaneamente (o que acontece quando o servidor WA rejeita a versão do protocolo), **todas** tentam reconectar ao mesmo tempo, a cada 5 segundos. O servidor do WhatsApp interpreta isso como flood e responde com `408 Connection Lost`, perpetuando o loop.

**Sessões observadas nos logs:**
- `WhatsClipper`
- `WhatsSMR`
- `WhatsLook`
- `WhatsMK`
- `WhatsDinamica`
- `WhatsValues`
- `testewppvh`

Todas em loop simultâneo de 5s = muitas conexões por minuto = bloqueio do WA.

**Solução:** Backoff exponencial com jitter:
```js
function getReconnectDelay(attempt) {
    // 5s → 10s → 20s → 40s → ... até 5 minutos máximo
    const base = Math.min(5000 * Math.pow(2, attempt), 300000);
    // Jitter ±20% para evitar que todas as sessões tentem juntas
    const jitter = base * 0.2 * (Math.random() * 2 - 1);
    return Math.round(base + jitter);
}
```

---

### 🟡 Causa #3 — Vazamento de event listeners ao reconectar

Toda vez que `startSession()` é chamada, um novo `conn` é criado e novos listeners são registrados via `conn.ev.on(...)`. O `conn` anterior pode ainda ter listeners ativos pendentes. Ao longo de milhares de reconexões (84k+), isso acumula memória e pode causar comportamento inesperado.

**Solução:** Limpar listeners do conn anterior antes de criar o novo:
```js
async function startSession(sessionId) {
    // Limpar conexão anterior
    const existingSession = sessions[sessionId];
    if (existingSession?.conn) {
        try {
            existingSession.conn.ev.removeAllListeners();
        } catch (e) { /* ignora */ }
    }
    // ... resto do código ...
}
```

---

### 🟡 Causa #4 — Constantes de alerta não declaradas

```js
// server_baileys_pro.mjs, linha 55:
if (!ADMIN_SESSION_ID || !ALERT_TARGET_JID) { // variáveis nunca declaradas!
```

Em JavaScript, variáveis não declaradas são `undefined` — não causa crash, mas o sistema de alertas de desconexão **nunca funciona**. A função `sendConnectionAlert()` sempre cai no `if` de "não configurado".

**Solução:**
```js
const ADMIN_SESSION_ID = process.env.ADMIN_SESSION_ID || null;
const ALERT_TARGET_JID = process.env.ALERT_TARGET_JID || null;
```

---

## Correções Necessárias

### 1. Atualizar o pacote (na máquina de produção)

```bat
npm install @whiskeysockets/baileys@6.7.24
```

Depois atualizar `package.json`:
```diff
- "@whiskeysockets/baileys": "^6.7.20",
+ "@whiskeysockets/baileys": "6.7.24",
```

### 2. Restaurar `fetchLatestBaileysVersion` no `server_baileys_pro.mjs`

```diff
 import makeWASocket, { 
     useMultiFileAuthState, 
     DisconnectReason,
-    // fetchLatestBaileysVersion <-- REMOVIDO
+    fetchLatestBaileysVersion,
 } from '@whiskeysockets/baileys'; 
```

E dentro de `startSession()`:
```diff
-    // const version = [6, 7, 1];
-    const version = [2, 3000, 1043857760];
-    console.log(`[${sessionId}] Iniciando sessão com versão fixa: ${version.join('.')}`);
+    const { version, isLatest } = await fetchLatestBaileysVersion();
+    console.log(`[${sessionId}] Versão WA: ${version.join('.')} (atualizada do GitHub: ${isLatest})`);
```

> `fetchLatestBaileysVersion()` tenta buscar a versão atual do GitHub. Se falhar (sem internet/timeout), **usa automaticamente o `baileys-version.json` do pacote** como fallback — sem crash.

### 3. Adicionar backoff exponencial + contador de tentativas

```diff
+// --- Backoff Exponencial com Jitter ---
+function getReconnectDelay(attempt) {
+    const base = Math.min(5000 * Math.pow(2, attempt), 300000);
+    const jitter = base * 0.2 * (Math.random() * 2 - 1);
+    return Math.round(base + jitter);
+}

 // Dentro de startSession(), no objeto da sessão:
     sessions[sessionId] = { 
         conn, 
         state: 'connecting', 
-        qrCode: null 
+        qrCode: null,
+        reconnectAttempts: 0,
     };

 // No handler connection === 'close':
-                console.log(`[${sessionId}] Tentando reconexão...`);
-                setTimeout(() => startSession(sessionId), 5000);
+                const attempts = (sessions[sessionId]?.reconnectAttempts || 0) + 1;
+                if (sessions[sessionId]) sessions[sessionId].reconnectAttempts = attempts;
+                const delay = getReconnectDelay(attempts);
+                console.log(`[${sessionId}] Reconexão tentativa #${attempts} em ${(delay/1000).toFixed(1)}s...`);
+                setTimeout(() => startSession(sessionId), delay);

 // No handler connection === 'open':
+            if (sessions[sessionId]) sessions[sessionId].reconnectAttempts = 0; // reset
```

### 4. Declarar constantes de alerta

Usuário **não usa variáveis de ambiente** — usar valores hardcoded com fallback para env:

```diff
+// Adicionar após os imports, antes das variáveis globais:
+const ADMIN_SESSION_ID = process.env.ADMIN_SESSION_ID || 'WhatsAdmin';
+const ALERT_TARGET_JID = process.env.ALERT_TARGET_JID || '5561998883036@s.whatsapp.net';
```

### 5. Limpar listeners antes de reconectar

```diff
 async function startSession(sessionId) {
     const authDir = path.join(__dirname, `auth_info_${sessionId}`);
+    
+    const existingSession = sessions[sessionId];
+    if (existingSession?.conn) {
+        try { existingSession.conn.ev.removeAllListeners(); } catch (e) {}
+    }
```

---

## Respostas do Usuário (todas as dúvidas resolvidas)

Todas as perguntas foram respondidas pelo usuário. O implementador **não precisa perguntar nada** — pode executar as correções diretamente.

| # | Pergunta | Resposta |
|---|---|---|
| 1 | ADMIN_SESSION_ID | `WhatsAdmin` |
| 1 | ALERT_TARGET_JID | `5561998883036@s.whatsapp.net` |
| 2 | Internet no Windows Server | **Sim** — `raw.githubusercontent.com` acessível |
| 3 | Rotação de logs | **Sim** — implementar junto com a correção principal |
| 4 | Versão do Node.js | **v24.11.0** ✅ (compatível com Baileys 6.7.24 e 7.x) |
| 5 | Usuário sabe usar "Limpar Conexão" | **Não sabe** — incluir instruções no guia de pós-correção |

### Implicações das respostas

- **Alerta configurado:** `ADMIN_SESSION_ID = 'WhatsAdmin'` e `ALERT_TARGET_JID = '5561998883036@s.whatsapp.net'` devem ser declarados como constantes no `server_baileys_pro.mjs` (não como `process.env`, já que o usuário não usa variáveis de ambiente — ou pode-se deixar com env e valor padrão hardcoded como fallback).
- **Rotação de logs:** Usar `pino-roll` (compatível com o `pino` já instalado). Rotacionar diariamente, mantendo os últimos 7 dias. Instalar como dependência.
- **Node.js v24** é mais que suficiente para qualquer versão do Baileys (6.x ou 7.x).
- **QR Code:** O implementador deve incluir, no arquivo de instruções ou README, um passo a passo de como usar a função "Limpar Conexão" do app Python para forçar novo QR.

---

## Rotação de Logs (implementar junto)

Usar `pino-roll` — compatível com o `pino` já instalado.

### Instalar:
```bat
npm install pino-roll
```

### Atualizar `package.json`:
```diff
     "pino": "^10.0.0",
+    "pino-roll": "^1.0.0",
```

### Alterar o logger no `server_baileys_pro.mjs`:

A ideia é redirecionar a saída do processo para um arquivo rotacionado diariamente, em vez de usar `console.log` para o stdout sem controle.

**Opção mais simples (sem mudar o código do logger interno):** Criar um script de start que piped a saída para o `pino-roll`.

No `package.json`, alterar o script de start:
```diff
-    "start": "node server_baileys_pro.mjs",
+    "start": "node server_baileys_pro.mjs | pino-roll logs/app-%Y-%m-%d.log --frequency daily --limit 7d",
```

Criar a pasta `logs/` na raiz do projeto.

Ou, se preferir manter o `console.log` como está e apenas controlar os arquivos de log via `start_gui_loop.bat`, redirecionar no próprio `.bat`:
```bat
node server_baileys_pro.mjs >> logs\app-%DATE:~6,4%-%DATE:~3,2%-%DATE:~0,2%.log 2>&1
```

> **Nota ao implementador:** Os arquivos `logs-backend.txt` e `logs-backend-error.txt` atuais podem ser apagados ou arquivados — eles já têm 248 MB de erros que não são mais relevantes.

---

## Instruções de QR Code — Para o Usuário (que não sabe usar "Limpar Conexão")

**Quando usar:** Se após a correção e reinício do servidor, uma sessão ficar presa em estado `connecting` ou `closed` por mais de 2 minutos, as credenciais daquela sessão provavelmente se corromperam e precisam ser resetadas.

**Passo a passo no app Python:**

1. Abra o aplicativo Python (`app_sender_pro.py`)
2. No menu principal, procure o botão ou opção **"Gerenciar Sessões"** ou **"Limpar Conexão"**
3. Selecione a sessão problemática (ex: `WhatsClipper`)
4. Confirme a exclusão das credenciais quando perguntado (mensagem: *"Tem certeza que deseja DELETAR a CONEXÃO...?"* → clique **Sim**)
5. Depois, clique em **"1. Adicionar/Verificar Sessão"** e selecione a mesma sessão
6. Um QR Code será aberto no navegador automaticamente
7. No celular que tem a conta WhatsApp daquela sessão: abra WhatsApp → Menu (3 pontinhos) → Dispositivos conectados → Conectar dispositivo → Escaneie o QR Code

> **Importante:** Essa operação limpa APENAS as credenciais de conexão. Todos os agendamentos daquela sessão são preservados.

---

## Verificação Pós-Correção

**Logs esperados após a correção:**
```
[WhatsClipper] Versão WA: 2.3000.1043857760 (atualizada do GitHub: false)
[WhatsClipper] Conectado com sucesso!
[WhatsMK] Versão WA: 2.3000.1043857760 (atualizada do GitHub: false)
[WhatsMK] Conectado com sucesso!
```

**Se ainda houver reconexões, devem ter delay crescente:**
```
[WhatsClipper] Reconexão tentativa #1 em 5.3s...
[WhatsClipper] Reconexão tentativa #2 em 10.1s...
[WhatsClipper] Reconexão tentativa #3 em 19.8s...
```

Em vez do antigo:
```
[WhatsClipper] Tentando reconexão...   ← spam a cada 5s
[WhatsClipper] Tentando reconexão...
[WhatsClipper] Tentando reconexão...
```

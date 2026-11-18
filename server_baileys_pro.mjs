import makeWASocket, { 
    useMultiFileAuthState, 
    DisconnectReason,
    fetchLatestBaileysVersion,
} from '@whiskeysockets/baileys'; 
import express from 'express';
import bodyParser from 'body-parser';
import async from 'async'; 
import path from 'path';
import fs from 'fs';
import util from 'util';
import dns from 'dns';
import net from 'net';
import pino from 'pino';

// --- Ajuste de rede (servidor sem rota IPv6 + path MTU reduzida) ---
// O "Happy Eyeballs" (autoSelectFamily) do Node tenta IPv6, não há rota (ENETUNREACH),
// e o handshake TLS do WebSocket do WhatsApp trava -> erro 408. Forçamos IPv4 primeiro
// e desligamos o autoSelectFamily. Se algum dia houver IPv6 funcional, é só remover isto.
dns.setDefaultResultOrder('ipv4first');
if (typeof net.setDefaultAutoSelectFamily === 'function') {
    net.setDefaultAutoSelectFamily(false);
}

// --- Variáveis de Caminho para Módulos ES (.mjs) ---
import { fileURLToPath } from 'url';
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
// --------------------------------------------------------

// --- Configurações de Alerta ---
// O usuário não usa variáveis de ambiente; os valores padrão são usados como fallback.
const ADMIN_SESSION_ID = process.env.ADMIN_SESSION_ID || 'WhatsAdmin';
const ALERT_TARGET_JID = process.env.ALERT_TARGET_JID || '5561998883036@s.whatsapp.net';

// --- Rotação de Logs (pino-roll: rotação diária, mantendo os últimos 7 arquivos) ---
const logStream = pino.transport({
    target: 'pino-roll',
    options: {
        file: path.join(__dirname, 'logs', 'app'),
        frequency: 'daily',
        mkdir: true,
        limit: { count: 7 },
        extension: '.log',
    },
});

// Espelha tudo que seria impresso no console para o arquivo de log rotacionado.
const originalLog = console.log.bind(console);
const originalInfo = console.info.bind(console);
const originalWarn = console.warn.bind(console);
const originalError = console.error.bind(console);

function formatLogArgs(args) {
    return args
        .map((a) => (typeof a === 'string' ? a : util.inspect(a, { depth: 4, breakLength: Infinity })))
        .join(' ');
}

// --- Filtro de ruído do libsignal ("Bad MAC") ---
// Essas mensagens vêm direto do console.error do libsignal (recebimento de mensagens
// que não puderam ser decriptadas). Não afetam o ENVIO, mas inundam o log.
// Estratégia: registra a 1ª ocorrência e, depois, no máximo 1 resumo por minuto.
const badMacState = { lastLog: 0, suppressed: 0 };

function classifyBadMac(text) {
    if (text.includes('Failed to decrypt message with any known session')) return 'start';
    if (text.includes('Bad MAC')) return 'cont';
    return null;
}

function writeLogLine(level, text, args, original) {
    const line = `[${new Date().toISOString()}] [${level}] ${text}`;
    if (original) original(...args);
    try {
        logStream.write(line + '\n');
    } catch (e) {
        originalLog(`[LOGGER] Falha ao escrever no arquivo de log: ${e.message}`);
    }
}

function teeToLogFile(level, original, args) {
    const text = formatLogArgs(args);
    const kind = classifyBadMac(text);

    if (kind) {
        if (kind === 'cont') return; // continuação (stack) do Bad MAC: sempre ignorada
        const now = Date.now();
        if (now - badMacState.lastLog >= 60000) {
            const suffix = badMacState.suppressed > 0
                ? ` [${badMacState.suppressed} ocorrências de "Bad MAC" suprimidas no último minuto]`
                : '';
            badMacState.lastLog = now;
            badMacState.suppressed = 0;
            writeLogLine(level, text + suffix, args, original);
        } else {
            badMacState.suppressed++;
        }
        return;
    }

    writeLogLine(level, text, args, original);
}

console.log = (...args) => teeToLogFile('LOG', originalLog, args);
console.info = (...args) => teeToLogFile('INFO', originalInfo, args);
console.warn = (...args) => teeToLogFile('WARN', originalWarn, args);
console.error = (...args) => teeToLogFile('ERROR', originalError, args);

// Logger interno do Baileys (silencioso para não poluir o log da aplicação).
const logger = pino({ level: 'silent' });
const app = express();
const port = 3000;

app.use(bodyParser.json());

// -----------------------------

const sessions = {};
const groupJidsCache = {}; 

// --- Backoff Exponencial com Jitter (evita "reconnect storm") ---
function getReconnectDelay(attempt) {
    // 5s → 10s → 20s → 40s → ... até o teto de 5 minutos
    const base = Math.min(5000 * Math.pow(2, attempt - 1), 300000);
    // Jitter de ±20% para que as sessões não reconectem todas no mesmo instante
    const jitter = base * 0.2 * (Math.random() * 2 - 1);
    return Math.round(base + jitter);
}

// --- Fila de mensagens (inalterado) ---
const sendQueue = async.queue(async (task) => {
    const { sessionId, jid, message } = task;
    const session = sessions[sessionId];

    if (!session || session.state !== 'open') {
        console.error(`[${sessionId}] Tentativa de envio falhou: Sessão não está aberta.`);
        throw new Error(`Sessão ${sessionId} não está aberta.`); 
    }

    try {
        console.log(`[${sessionId}] Enviando para ${jid}...`);
        const sendResult = await session.conn.sendMessage(jid, { text: message }); 
        console.log(`[${sessionId}] Sucesso no envio para ${jid}.`);
        return sendResult; 
    } catch (e) {
        console.error(`[${sessionId}] Erro no envio para ${jid}:`, e);
        throw e; 
    }
}, 1);

// --- Função de Alerta (inalterado) ---
async function sendConnectionAlert(disconnectedSessionId) {
    console.log(`[ALERTA] Tentando enviar aviso sobre a desconexão de: ${disconnectedSessionId}`);
    
    if (!ADMIN_SESSION_ID || !ALERT_TARGET_JID) {
        console.warn('[ALERTA] ADMIN_SESSION_ID ou ALERT_TARGET_JID não configurados. Alerta não enviado.');
        return;
    }

    const adminSession = sessions[ADMIN_SESSION_ID];

    if (!adminSession || adminSession.state !== 'open') {
        console.error(`[ALERTA] FALHA AO ENVIAR ALERTA! A sessão admin "${ADMIN_SESSION_ID}" não está conectada.`);
        if (disconnectedSessionId === ADMIN_SESSION_ID) {
             console.error(`[ALERTA] A sessão admin ("${ADMIN_SESSION_ID}") foi a que se desconectou.`);
        }
        return;
    }

    const alertMessage = `🚨 ALERTA DE DESCONEXÃO 🚨

A sessão do WhatsApp com ID: *${disconnectedSessionId}*
foi desconectada permanentemente (Status: Logged Out).

É necessário escanear um novo QR Code para esta sessão através do app Python.`;

    try {
        await adminSession.conn.sendMessage(ALERT_TARGET_JID, { text: alertMessage });
        console.log(`[ALERTA] Alerta sobre "${disconnectedSessionId}" enviado com sucesso para ${ALERT_TARGET_JID}.`);
    } catch (e) {
        console.error(`[ALERTA] Erro ao enviar a mensagem de alerta via sessão admin: ${e.message}`);
    }
}

// --- Função Sincronizar Grupos ---
// Normaliza o nome do grupo (remove espaços nas pontas) para o cache ficar consistente.
function normalizeGroupName(name) {
    return String(name || '').trim().toLowerCase();
}

async function syncGroups(sessionId, conn) {
    console.log(`[${sessionId}] Iniciando sincronização de grupos...`);
    try {
        const groups = await conn.groupFetchAllParticipating();
        let count = 0;
        for (const [jid, metadata] of Object.entries(groups)) {
            const groupName = metadata.subject;
            if (groupName) {
                const key = normalizeGroupName(groupName);
                if (key) {
                    groupJidsCache[key] = jid;
                    count++;
                }
            }
        }
        console.log(`[${sessionId}] Sincronização de grupos concluída: ${count} grupos encontrados e em cache.`);
    } catch (e) {
        console.error(`[${sessionId}] Erro ao sincronizar grupos: ${e.message}`);
        setTimeout(() => syncGroups(sessionId, conn), 5000); 
    }
}

// --- FUNÇÃO PARA INICIAR UMA SESSÃO ---
async function startSession(sessionId) {
    const authDir = path.join(__dirname, `auth_info_${sessionId}`);

    // --- Limpa a conexão anterior (evita vazamento de listeners ao reconectar) ---
    const existingSession = sessions[sessionId];
    const previousAttempts = existingSession?.reconnectAttempts || 0;
    if (existingSession?.conn?.ev) {
        try {
            existingSession.conn.ev.removeAllListeners();
        } catch (e) { /* ignora */ }
    }

    const { state, saveCreds } = await useMultiFileAuthState(authDir);

    // --- Versão do protocolo obtida dinamicamente do GitHub (fallback automático se offline) ---
    const { version, isLatest, error: versionError } = await fetchLatestBaileysVersion({ timeout: 10000 });
    console.log(`[${sessionId}] Versão WA: ${version.join('.')} (atualizada do GitHub: ${isLatest})${versionError ? ` [FALHA ao buscar no GitHub: ${versionError.message}]` : ''}`);

    const conn = makeWASocket({
        version,
        auth: state,
        printQRInTerminal: false,
        logger: logger,
        getMessage: async () => ({}), 
        shouldIgnoreJid: (jid) => false, 
    });

    sessions[sessionId] = { 
        conn, 
        state: 'connecting', 
        qrCode: null,
        reconnectAttempts: previousAttempts,
    };
    
    // 4. Lógica de eventos
    conn.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            sessions[sessionId].qrCode = qr;
            sessions[sessionId].state = 'qr_required';
            console.log(`[${sessionId}] QR Code necessário.`);
        }
        
        if (connection === 'close') {
            const statusCode = lastDisconnect?.error?.output?.statusCode;
            const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
            
            sessions[sessionId].state = shouldReconnect ? 'closed' : 'loggedOut';
            const detalhe = lastDisconnect?.error?.message || 'sem mensagem';
            console.log(`[${sessionId}] Conexão fechada. Motivo: ${statusCode}. Reconectar? ${shouldReconnect}. Detalhe: ${detalhe}`);
            if (lastDisconnect?.error) {
                const errOut = lastDisconnect.error.output;
                if (errOut) console.log(`[${sessionId}] Disconnect output: ${JSON.stringify(errOut).slice(0, 500)}`);
                if (lastDisconnect.error.stack) console.log(`[${sessionId}] Disconnect stack: ${String(lastDisconnect.error.stack).split('\n').slice(0, 6).join(' | ')}`);
            }
            
            if (shouldReconnect) {
                const attempts = (sessions[sessionId].reconnectAttempts || 0) + 1;
                sessions[sessionId].reconnectAttempts = attempts;
                const delay = getReconnectDelay(attempts);
                console.log(`[${sessionId}] Reconexão tentativa #${attempts} em ${(delay / 1000).toFixed(1)}s...`);
                setTimeout(() => startSession(sessionId), delay);
            } else {
                console.log(`[${sessionId}] Desconexão permanente detectada. Disparando alerta...`);
                sendConnectionAlert(sessionId); 
            }
            
        } else if (connection === 'open') {
            sessions[sessionId].state = 'open';
            sessions[sessionId].reconnectAttempts = 0;
            console.log(`[${sessionId}] Conectado com sucesso!`);
            syncGroups(sessionId, conn);
        }
    });

    // LISTENER DE JID
    conn.ev.on('messages.upsert', async ({ messages }) => {
        if (!messages.length) return;
        const message = messages[0];
        const jid = message.key.remoteJid;
        if (message.key.fromMe || !jid.endsWith('@g.us')) return;

        try {
            const metadata = await conn.groupMetadata(jid);
            const groupName = metadata.subject;
            const key = normalizeGroupName(groupName);
            if (key && !groupJidsCache[key]) {
                groupJidsCache[key] = jid;
                console.log(`[${sessionId}] JID de grupo capturado (Mensagem): "${groupName}" -> ${jid}`);
            }
        } catch (e) {
            console.error(`[${sessionId}] FALHA ao obter metadados do grupo ${jid}: ${e.message}`);
        }
    });

    // 5. Salva credenciais (inalterado)
    conn.ev.on('creds.update', saveCreds);

    sessions[sessionId].conn = conn;
    return sessions[sessionId];
}

// --- ENDPOINTS (Todos inalterados) ---

// --- ENDPOINT 1: INICIAR NOVA SESSÃO / VERIFICAR STATUS ---
app.post('/session/start', async (req, res) => {
    const { sessionId } = req.body;

    if (!sessionId) {
        return res.status(400).json({ status: 'error', message: 'sessionId é obrigatório.' });
    }

    let session = sessions[sessionId];

    if (!session || session.state === 'loggedOut' || session.state === 'failed_auth') {
        try {
            session = await startSession(sessionId);
        } catch (e) {
            console.error(`Erro fatal ao iniciar sessão ${sessionId}:`, e);
            return res.status(500).json({ status: 'error', message: 'Erro interno ao iniciar sessão.' });
        }
    }
    
    res.json({
        sessionId: sessionId,
        state: session.state,
        qrCode: session.qrCode
    });
});

// --- ENDPOINT 2: ENVIAR MENSAGEM VIA FILA ---
app.post('/send_message', async (req, res) => {
    const { sessionId, jid: rawJid, message } = req.body;

    if (!sessionId || !rawJid || !message) {
        return res.status(400).json({ status: 'error', message: 'sessionId, JID e mensagem são obrigatórios.' });
    }

    const ignoredPhrase = "sem notícias até o momento";
    
    if (message.toLowerCase().includes(ignoredPhrase.toLowerCase())) {
        console.log(`[${sessionId}] ENVIO IGNORADO: Mensagem contém a frase de bloqueio: "${ignoredPhrase}".`);
        return res.status(200).json({ 
            status: 'ignored', 
            message: 'Envio ignorado: Conteúdo sem notícias no momento.' 
        });
    }

    const session = sessions[sessionId];
    if (!session || session.state !== 'open') {
        return res.status(503).json({ 
            status: 'error', 
            message: `Sessão ${sessionId} não está aberta. Status: ${session ? session.state : 'UNKNOWN'}` 
        });
    }

    let jidToSend = rawJid;
    const isJidFormat = rawJid.includes('@');

    if (!isJidFormat) {
        const cachedJid = groupJidsCache[normalizeGroupName(rawJid)];
        if (cachedJid) {
            jidToSend = cachedJid;
            console.log(`[${sessionId}] Usando JID numérico em cache: ${jidToSend}`);
        } else {
            jidToSend = `${normalizeGroupName(rawJid).replace(/ /g, '_')}@g.us`;
            console.warn(`[${sessionId}] JID "${rawJid}" não encontrado no cache. Tentando JID formatado: ${jidToSend}`);
        }
    }

    sendQueue.push({ sessionId, jid: jidToSend, message });
    
    res.json({ status: 'queued', message: 'Mensagem adicionada à fila de envio.' });
});

// --- ENDPOINT 3: BUSCAR CACHE DE JIDS DE GRUPO ---
app.get('/group_jids', (req, res) => {
    res.json(groupJidsCache);
});

// --- ENDPOINT 4: VERIFICAR STATUS DA SESSÃO ---
app.get('/session/state', (req, res) => {
    const { sessionId } = req.query; 
    
    if (!sessionId) {
        return res.status(400).json({ status: 'error', message: 'sessionId é obrigatório na query.' });
    }

    const session = sessions[sessionId];

    if (!session) {
        return res.status(200).json({ sessionId, state: 'NOT_INIT', message: 'Sessão nunca foi inicializada neste servidor.' });
    }
    
    res.json({ sessionId, state: session.state, qrCode: session.qrCode, message: 'Status atual da sessão.' });
});

app.post('/session/state', (req, res) => {
    const { sessionId } = req.body; 

    if (!sessionId) {
        return res.status(400).json({ status: 'error', message: 'sessionId é obrigatório no corpo (body) da requisição POST.' });
    }

    const session = sessions[sessionId];

    if (!session) {
        return res.status(200).json({ sessionId, state: 'NOT_INIT', message: 'Sessão nunca foi inicializada neste servidor.' });
    }
    
    res.json({ sessionId, state: session.state, qrCode: session.qrCode, message: 'Status atual da sessão.' });
});


// --- AUTO-START NO BOOT ---
// Ao iniciar o backend, procura por sessões já pareadas (pastas auth_info_* com creds.json)
// e reconecta cada uma automaticamente, sem precisar de QR nem de ação manual.
// Isso torna o reinício do PC/serviço transparente para o agendador.
function discoverSavedSessions() {
    try {
        const dirs = fs
            .readdirSync(__dirname, { withFileTypes: true })
            .filter((entry) => entry.isDirectory())
            .map((entry) => entry.name);
        const candidates = dirs.filter((name) => name.startsWith('auth_info_'));
        console.log(`[BOOT] Diretório do projeto: ${__dirname}`);
        console.log(`[BOOT] Pastas auth_info_* encontradas: ${candidates.join(', ') || 'nenhuma'}`);
        return candidates
            .map((name) => name.slice('auth_info_'.length))
            .filter((sessionId) => sessionId && fs.existsSync(path.join(__dirname, `auth_info_${sessionId}`, 'creds.json')));
    } catch (e) {
        console.error(`[BOOT] Falha ao procurar sessões salvas: ${e.message}`);
        return [];
    }
}

function autoStartSavedSessions() {
    const sessionIds = discoverSavedSessions();
    if (!sessionIds.length) {
        console.log('[BOOT] Nenhuma sessão salva (auth_info_* com creds.json) encontrada.');
        return;
    }
    console.log(`[BOOT] Sessões salvas encontradas: ${sessionIds.join(', ')}. Iniciando reconexão automática...`);
    sessionIds.forEach((sessionId, index) => {
        if (sessions[sessionId]) return;
        // Escalona as conexões (3s entre cada) para não disparar todos os handshakes ao mesmo tempo.
        setTimeout(() => {
            console.log(`[BOOT] Iniciando sessão salva "${sessionId}"...`);
            startSession(sessionId).catch((e) => {
                console.error(`[${sessionId}] Falha ao iniciar sessão no boot: ${e.message}`);
            });
        }, index * 3000);
    });
}

app.listen(port, () => {
    console.log(`Servidor Node.js PRO (Baileys) rodando em http://localhost:${port}`);
    autoStartSavedSessions();
});
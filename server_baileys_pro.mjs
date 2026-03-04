import makeWASocket, { 
    useMultiFileAuthState, 
    DisconnectReason,
    // fetchLatestBaileysVersion <-- REMOVIDO
} from '@whiskeysockets/baileys'; 
import express from 'express';
import bodyParser from 'body-parser';
import async from 'async'; 
import path from 'path';
import fs from 'fs';
import pino from 'pino';

// --- Variáveis de Caminho para Módulos ES (.mjs) ---
import { fileURLToPath } from 'url';
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
// --------------------------------------------------------

const logger = pino({ level: 'silent' });
const app = express();
const port = 3000;

app.use(bodyParser.json());

// -----------------------------

const sessions = {};
const groupJidsCache = {}; 

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

// --- Função Sincronizar Grupos (inalterado) ---
async function syncGroups(sessionId, conn) {
    console.log(`[${sessionId}] Iniciando sincronização de grupos...`);
    try {
        const groups = await conn.groupFetchAllParticipating();
        let count = 0;
        for (const [jid, metadata] of Object.entries(groups)) {
            const groupName = metadata.subject;
            if (groupName) {
                groupJidsCache[groupName.toLowerCase()] = jid;
                count++;
            }
        }
        console.log(`[${sessionId}] Sincronização de grupos concluída: ${count} grupos encontrados e em cache.`);
    } catch (e) {
        console.error(`[${sessionId}] Erro ao sincronizar grupos: ${e.message}`);
        setTimeout(() => syncGroups(sessionId, conn), 5000); 
    }
}

// --- FUNÇÃO PARA INICIAR UMA SESSÃO (MODIFICADA) ---
async function startSession(sessionId) {
    const authDir = path.join(__dirname, `auth_info_${sessionId}`);
    
    const { state, saveCreds } = await useMultiFileAuthState(authDir);
    
    // --- INÍCIO DA MODIFICAÇÃO ---
    // Removido: const { version } = await fetchLatestBaileysVersion()
    // Adicionada uma versão fixa para evitar consulta à internet:
    const version = [6, 7, 1]; // Usando uma versão estável e recente
    console.log(`[${sessionId}] Iniciando sessão com versão fixa: ${version.join('.')}`);
    // --- FIM DA MODIFICAÇÃO ---

    const conn = makeWASocket({
        version, // <--- Usa a versão fixa
        auth: state,
        printQRInTerminal: false,
        logger: logger,
        getMessage: async () => ({}), 
        shouldIgnoreJid: (jid) => false, 
    });

    sessions[sessionId] = { 
        conn, 
        state: 'connecting', 
        qrCode: null 
    };
    
    // 4. Lógica de eventos (inalterado)
    conn.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            sessions[sessionId].qrCode = qr;
            sessions[sessionId].state = 'qr_required';
            console.log(`[${sessionId}] QR Code necessário.`);
        }
        
        if (connection === 'close') {
            const shouldReconnect = lastDisconnect.error?.output?.statusCode !== DisconnectReason.loggedOut;
            const reason = lastDisconnect.error?.output?.statusCode;
            
            sessions[sessionId].state = shouldReconnect ? 'closed' : 'loggedOut';
            console.log(`[${sessionId}] Conexão fechada. Motivo: ${reason}. Reconectar? ${shouldReconnect}`);
            
            if (shouldReconnect) {
                console.log(`[${sessionId}] Tentando reconexão...`);
                setTimeout(() => startSession(sessionId), 5000); 
            } else {
                console.log(`[${sessionId}] Desconexão permanente detectada. Disparando alerta...`);
                sendConnectionAlert(sessionId); 
            }
            
        } else if (connection === 'open') {
            sessions[sessionId].state = 'open';
            console.log(`[${sessionId}] Conectado com sucesso!`);
            syncGroups(sessionId, conn);
        }
    });

    // LISTENER DE JID (inalterado)
    conn.ev.on('messages.upsert', async ({ messages }) => {
        if (!messages.length) return;
        const message = messages[0];
        const jid = message.key.remoteJid;
        if (message.key.fromMe || !jid.endsWith('@g.us')) return;

        try {
            const metadata = await conn.groupMetadata(jid);
            const groupName = metadata.subject;
            if (groupName && !groupJidsCache[groupName.toLowerCase()]) {
                groupJidsCache[groupName.toLowerCase()] = jid;
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
        const cachedJid = groupJidsCache[rawJid.toLowerCase()];
        if (cachedJid) {
            jidToSend = cachedJid;
            console.log(`[${sessionId}] Usando JID numérico em cache: ${jidToSend}`);
        } else {
            jidToSend = `${rawJid.toLowerCase().replace(/ /g, '_')}@g.us`;
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


app.listen(port, () => {
    console.log(`Servidor Node.js PRO (Baileys) rodando em http://localhost:${port}`);
});
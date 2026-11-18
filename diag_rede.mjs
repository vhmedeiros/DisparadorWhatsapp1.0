import https from 'https';
import tls from 'tls';
import dns from 'dns';

function httpsGet(url, label) {
    return new Promise((resolve) => {
        const req = https.get(url, { timeout: 15000 }, (res) => {
            console.log(`[HTTPS] ${label} -> ${res.statusCode}`);
            res.resume();
            resolve();
        });
        req.on('timeout', () => { console.log(`[HTTPS] ${label} -> TIMEOUT`); req.destroy(); resolve(); });
        req.on('error', (e) => {
            console.log(`[HTTPS] ${label} -> ERRO name=${e.name} code=${e.code} msg=${JSON.stringify(e.message)}`);
            if (e.errors) console.log(`         sub: ${e.errors.map((x) => `${x.code}:${x.message}`).join(' | ')}`);
            if (e.cause) console.log(`         cause: ${e.cause.code} ${e.cause.message}`);
            resolve();
        });
    });
}

function tlsTest(host, port = 443) {
    return new Promise((resolve) => {
        const socket = tls.connect({ host, port, servername: host, timeout: 12000, rejectUnauthorized: false }, () => {
            const cert = socket.getPeerCertificate();
            console.log(`[TLS ] ${host}:${port} -> OK proto=${socket.getProtocol()} authorized=${socket.authorized} issuerCN=${cert && cert.issuer && cert.issuer.CN}`);
            socket.end();
            resolve();
        });
        socket.on('timeout', () => { console.log(`[TLS ] ${host}:${port} -> TIMEOUT`); socket.destroy(); resolve(); });
        socket.on('error', (e) => { console.log(`[TLS ] ${host}:${port} -> ERRO name=${e.name} code=${e.code} msg=${JSON.stringify(e.message)}`); resolve(); });
    });
}

function resolveDns(host) {
    return new Promise((resolve) => {
        dns.lookup(host, { all: true }, (e, a) => {
            console.log(`[DNS ] ${host} -> ${e ? 'ERRO ' + e.message : JSON.stringify(a)}`);
            resolve();
        });
    });
}

console.log('Node', process.version, '| cwd', process.cwd());

const hosts = ['raw.githubusercontent.com', 'registry.npmjs.org', 'registry.npmmirror.com', 'www.google.com', 'web.whatsapp.com', 'g.whatsapp.net'];
for (const h of hosts) await resolveDns(h);

await httpsGet('https://raw.githubusercontent.com/WhiskeySockets/Baileys/master/src/Defaults/baileys-version.json', 'github-raw');
await httpsGet('https://registry.npmjs.org/pino-roll', 'npmjs');
await httpsGet('https://registry.npmmirror.com/pino-roll', 'npmmirror');
await httpsGet('https://www.google.com', 'google');
await httpsGet('https://web.whatsapp.com', 'web.whatsapp.com');
await httpsGet('https://api.qrserver.com/v1/create-qr-code/?size=50x50&data=test', 'qrserver');

for (const h of ['web.whatsapp.com', 'g.whatsapp.net', 'raw.githubusercontent.com']) await tlsTest(h);

console.log('FIM');

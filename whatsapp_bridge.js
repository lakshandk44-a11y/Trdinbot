/**
 * WhatsApp Notification Bridge for the trading bot.
 *
 * Uses Baileys (@whiskeysockets/baileys) - a lightweight WhatsApp Web
 * protocol library. Unlike whatsapp-web.js, this does NOT need
 * Puppeteer/Chromium, which matters on a small VPS already running the
 * Python bot + Flask dashboard.
 *
 * First run: a QR code prints in the terminal. Scan it with
 * WhatsApp > Linked Devices > Link a Device on your phone - exactly the
 * same as linking WhatsApp Web/Desktop. After that, the session is saved
 * to ./wa_auth/ so you do NOT need to re-scan on every restart.
 *
 * Exposes ONE local endpoint:
 *   POST http://localhost:3001/send   body: {"message": "..."}
 * This is only reachable from the same machine (not exposed to the
 * internet) - the Python bot calls it locally, nothing else needs to.
 */

// FIX (ReferenceError: crypto is not defined): Baileys expects the Web
// Crypto API as a GLOBAL `crypto` object, which Node.js only exposes
// globally by default from v20+. On older Node versions (common on a
// stock EC2 install), this polyfill makes it available without needing
// to upgrade Node itself. Must run before Baileys is required below.
const { webcrypto } = require("crypto");
if (!globalThis.crypto) {
    globalThis.crypto = webcrypto;
}

const express = require("express");
const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason,
} = require("@whiskeysockets/baileys");
const qrcode = require("qrcode-terminal");

const PORT = process.env.WHATSAPP_BRIDGE_PORT || 3001;

// Who to send notifications to. Format: countrycode+number, no +, no
// spaces (e.g. Sri Lanka number 07XXXXXXXX -> "947XXXXXXXX").
// Leave unset (or set WHATSAPP_TO=self) to message your OWN linked
// WhatsApp account (shows up as "Message Yourself" in the app) - this is
// the simplest option since it needs no extra number setup at all.
const WHATSAPP_TO = process.env.WHATSAPP_TO || "self";

let sock = null;
let selfJid = null;

async function startWhatsApp() {
    const { state, saveCreds } = await useMultiFileAuthState("./wa_auth");

    sock = makeWASocket({
        auth: state,
        printQRInTerminal: false, // we print it ourselves via qrcode-terminal below
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            console.log("\n[WhatsApp Bridge] Scan this QR code with your phone:");
            console.log("WhatsApp app -> Settings -> Linked Devices -> Link a Device\n");
            qrcode.generate(qr, { small: true });
        }

        if (connection === "close") {
            const shouldReconnect =
                lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut;
            console.log("[WhatsApp Bridge] Connection closed. Reconnecting:", shouldReconnect);
            if (shouldReconnect) {
                startWhatsApp();
            } else {
                console.log("[WhatsApp Bridge] Logged out - delete ./wa_auth and restart to re-link.");
            }
        } else if (connection === "open") {
            selfJid = sock.user?.id;
            console.log("[WhatsApp Bridge] ✅ Connected as", selfJid);
        }
    });
}

function targetJid() {
    if (WHATSAPP_TO === "self") {
        return selfJid; // message yourself
    }
    return `${WHATSAPP_TO}@s.whatsapp.net`;
}

const app = express();
app.use(express.json());

app.post("/send", async (req, res) => {
    const { message } = req.body || {};
    if (!message) {
        return res.status(400).json({ error: "message field required" });
    }
    if (!sock || !targetJid()) {
        return res.status(503).json({ error: "WhatsApp not connected yet" });
    }
    try {
        await sock.sendMessage(targetJid(), { text: message });
        res.json({ ok: true });
    } catch (e) {
        console.error("[WhatsApp Bridge] Send failed:", e);
        res.status(500).json({ error: String(e) });
    }
});

app.get("/health", (req, res) => {
    res.json({ connected: !!sock, target: WHATSAPP_TO });
});

app.listen(PORT, "127.0.0.1", () => {
    console.log(`[WhatsApp Bridge] Local HTTP endpoint on http://127.0.0.1:${PORT}/send`);
});

startWhatsApp();

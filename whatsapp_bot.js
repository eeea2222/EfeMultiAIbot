'use strict';
const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const axios = require('axios');
const path = require('path');
const fs = require('fs');

// ── Yapılandırma (ortam değişkenlerinden) ────────────────────
const PANEL = process.env.PANEL_URL || 'http://127.0.0.1:5050';
const TARGET_GROUPS = (process.env.WHATSAPP_GROUPS || 'MyGroup1,MyGroup2')
  .split(',').map(s => s.trim()).filter(Boolean);
const SEARCH_COOLDOWN = parseInt(process.env.SEARCH_COOLDOWN || '15000', 10);

// Google arama (opsiyonel)
let google = null;
try { google = require('googlethis'); }
catch { console.warn('[BOT] googlethis yüklü değil, arama devre dışı.'); }

// identity.txt sistem promptu
let efePrompt = "Sen uzman bir yapay zeka asistanısın.";
try { efePrompt = fs.readFileSync(path.join(__dirname, 'identity.txt'), 'utf8'); }
catch (e) { console.warn('[BOT] identity.txt bulunamadı, varsayılan kullanılıyor.'); }

const agenticUsers = new Set();
const processingChats = new Set();
let llmQueue = Promise.resolve();
let lastSearch = 0;

// ── API Yardımcıları ─────────────────────────────────────────
async function getHistory(chatId, limit = 10, budget = 4096) {
  try {
    const r = await axios.get(`${PANEL}/api/messages/${chatId}?limit=${limit}&budget=${budget}`, { timeout: 5000 });
    return r.data.messages || [];
  } catch (e) {
    console.warn('[BOT] Geçmiş yüklenemedi:', e.message);
    return [];
  }
}

async function saveMsg(chatId, role, content) {
  try {
    await axios.post(`${PANEL}/api/messages/save`, { chat_id: chatId, role, content }, { timeout: 5000 });
  } catch (e) {
    console.warn(`[BOT] Mesaj kaydedilemedi (${role}):`, e.message);
  }
}

async function isEnabled(personId) {
  try {
    const r = await axios.get(`${PANEL}/api/ai_enabled/${personId}`, { timeout: 2000 });
    return r.data.enabled;
  } catch { return false; }
}

async function getLLMStatus() {
  for (let i = 0; i < 2; i++) {
    try {
      const r = await axios.get(`${PANEL}/api/server/status`, { timeout: 3000 });
      return r.data;
    } catch (e) {
      if (i === 1) throw e;
      await new Promise(r => setTimeout(r, 1000));
    }
  }
}

// ── WhatsApp İstemcisi ───────────────────────────────────────
const client = new Client({
  authStrategy: new LocalAuth({
    clientId: 'ram-bot',
    dataPath: path.join(__dirname, '.wwebjs_auth')
  }),
  puppeteer: {
    args: [
      '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
      '--disable-accelerated-2d-canvas', '--no-first-run', '--no-zygote',
      '--disable-gpu'
    ]
  }
});

// ── Graceful Shutdown ────────────────────────────────────────
let shuttingDown = false;

async function gracefulShutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`\n[BOT] ${signal} alındı, kapatılıyor…`);
  try {
    await client.destroy();
    console.log('[BOT] WhatsApp bağlantısı kapatıldı.');
  } catch (e) {
    console.error('[BOT] Kapatma hatası:', e.message);
  }
  process.exit(0);
}

process.on('SIGTERM', () => gracefulShutdown('SIGTERM'));
process.on('SIGINT', () => gracefulShutdown('SIGINT'));

// ── Olaylar ──────────────────────────────────────────────────
client.on('qr', qr => {
  qrcode.generate(qr, { small: true });
  console.log('[BOT] QR hazır — Telefonunuzla taratın');
});

client.on('authenticated', () => {
  console.log('[BOT] Kimlik doğrulandı ✓');
});

client.on('auth_failure', msg => {
  console.error('[BOT] Kimlik doğrulama başarısız:', msg);
});

client.on('disconnected', reason => {
  console.warn('[BOT] Bağlantı koptu:', reason);
  if (!shuttingDown) {
    const delays = [5000, 15000, 30000, 60000, 120000];
    let attempt = 0;
    const tryReconnect = () => {
      const delay = delays[Math.min(attempt, delays.length - 1)];
      console.log(`[BOT] ${delay / 1000}s sonra yeniden bağlanılıyor… (deneme ${attempt + 1})`);
      setTimeout(async () => {
        try {
          await client.initialize();
          attempt = 0;
        } catch (e) {
          console.error('[BOT] Yeniden bağlanma başarısız:', e.message);
          attempt++;
          tryReconnect();
        }
      }, delay);
    };
    tryReconnect();
  }
});

client.on('ready', async () => {
  console.log('[BOT] Hazır! ✓');
  try {
    const contacts = await client.getContacts();
    let n = 0;
    for (const c of contacts) {
      if (c.isMyContact && !c.isGroup) {
        await axios.post(`${PANEL}/api/contacts/upsert`, {
          id: c.id._serialized, name: c.name || '', pushname: c.pushname || ''
        }).catch(() => { });
        n++;
      }
    }
    console.log(`[BOT] Rehber senkronize: ${n} kişi`);
  } catch (e) { console.error('[BOT] Rehber hatası:', e.message); }
});

// ── Mesaj İşleme ─────────────────────────────────────────────
client.on('message', async msg => {
  if (shuttingDown) return;

  let chat;
  try { chat = await msg.getChat(); }
  catch (e) { console.log('[BOT] Desteklenmeyen mesaj, atlanıyor.'); return; }

  const isTarget = chat.isGroup && TARGET_GROUPS.includes(chat.name);
  const personId = msg.author || msg.from;

  let should = false;
  if (chat.isGroup) {
    if (isTarget) {
      const mentions = await msg.getMentions();
      should = mentions.some(c => c.id._serialized === client.info.wid._serialized);
    }
  } else {
    should = await isEnabled(personId);
  }
  if (!should) return;
  if (processingChats.has(chat.id._serialized)) {
    console.log('[BOT] Zaten işleniyor, atlanıyor.');
    return;
  }

  let prompt = msg.body.replace(/@\d+/g, '').trim();
  let imgB64 = null, imgMime = '';

  if (msg.hasMedia) {
    try {
      const media = await msg.downloadMedia();
      if (media?.mimetype?.startsWith('image/')) {
        imgB64 = media.data; imgMime = media.mimetype;
        await msg.reply('📸 Görsel alındı…');
      }
    } catch (e) {
      console.error('[BOT] Medya indirme hatası:', e.message);
    }
  }
  if (!prompt && !imgB64) return;
  if (!prompt && imgB64) prompt = 'Bu görselde ne var? Detaylıca açıkla.';

  processingChats.add(chat.id._serialized);

  llmQueue = llmQueue.then(async () => {
    const contact = await msg.getContact();
    const sender = contact.pushname || contact.name || 'Kullanıcı';
    const enhanced = `[${sender}]: ${prompt}`;
    const lower = prompt.toLowerCase();

    try {
      // Komutlar
      if (lower === 'agentic geç') {
        agenticUsers.add(personId);
        await msg.reply('🤖 Agentic mod aktif.'); return;
      }
      if (lower === 'normal geç' || lower === 'normale dön') {
        agenticUsers.delete(personId);
        await msg.reply('👤 Normal mod aktif.'); return;
      }

      await msg.reply('⏳ Düşünüyorum…');

      const status = await getLLMStatus();
      if (!status.running) {
        await msg.reply('Model kapalı. Panel: http://localhost:5050'); return;
      }
      const llamaUrl = `http://127.0.0.1:${status.port}/v1/chat/completions`;

      // ── İnternet Arama (opsiyonel) ──
      let needsSearch = false;
      if (!imgB64 && google) {
        try {
          const chk = await axios.post(llamaUrl, {
            model: 'local', stream: false,
            messages: [
              { role: 'system', content: "Gerçek zamanlı bilgi gerekiyorsa 'EVET', yoksa 'HAYIR' yaz." },
              { role: 'user', content: prompt }
            ]
          }, { timeout: 8000 });
          needsSearch = chk.data.choices[0].message.content.trim().toUpperCase().includes('EVET');
        } catch (e) { console.warn('[BOT] Arama karar hatası:', e.message); }
      }

      let searchCtx = '';
      if (needsSearch && google) {
        const now = Date.now();
        if (now - lastSearch >= SEARCH_COOLDOWN) {
          lastSearch = now;
          await msg.reply('🔍 Araştırıyorum…');
          try {
            const sr = await google.search(prompt, {
              page: 0, safe: false, parse_ads: false,
              additional_params: { hl: 'tr' }
            });
            if (sr.results?.length) {
              searchCtx = 'Güncel arama sonuçları:\n';
              for (let i = 0; i < Math.min(3, sr.results.length); i++)
                searchCtx += `- ${sr.results[i].title}: ${sr.results[i].description}\n`;
            }
          } catch (e) { console.log('[BOT] Arama hatası:', e.message); }
        }
      }

      // ── Prompt Hazırla ──
      const fmtRule = ' ÖNEMLİ: LaTeX kullanma. Sadece *kalın* _italik_ WhatsApp formatları.';
      const dateInfo = `\n[Tarih: ${new Date().toLocaleString('tr-TR', { timeZone: 'Europe/Istanbul' })}]`;
      const sysPrompt = agenticUsers.has(personId)
        ? efePrompt + fmtRule + dateInfo
        : 'Yardımsever bir yapay zeka asistanısın.' + fmtRule + dateInfo;

      let finalMsgs = [{ role: 'system', content: sysPrompt }];
      if (searchCtx) finalMsgs.push({ role: 'system', content: searchCtx });

      // Geçmiş (token bütçeli)
      try {
        const hist = await getHistory(personId, imgB64 ? 2 : 12, imgB64 ? 1024 : 4096);
        finalMsgs = finalMsgs.concat(hist);
      } catch (e) { console.error('[BOT] Geçmiş hatası:', e.message); }

      if (imgB64) {
        finalMsgs.push({
          role: 'user', content: [
            { type: 'text', text: enhanced },
            { type: 'image_url', image_url: { url: `data:${imgMime};base64,${imgB64}` } }
          ]
        });
        // Görseli kaydet (dedup)
        try {
          await axios.post(`${PANEL}/api/messages/save`,
            { chat_id: personId, role: 'user', content: `[Görsel] ${prompt}` });
        } catch { }
      } else {
        finalMsgs.push({ role: 'user', content: enhanced });
      }

      // Kullanıcı mesajını LLM çağrısından ÖNCE kaydet
      await saveMsg(personId, 'user', enhanced);

      // ── LLM İsteği ──
      const res = await axios.post(llamaUrl, {
        model: 'local', messages: finalMsgs, temperature: 0.7, stream: false
      }, { timeout: 120000 });

      let reply = res.data.choices[0].message.content.trim();

      // LaTeX temizliği
      reply = reply
        .replace(/\$\$(.*?)\$\$/gs, '$1').replace(/\$(.*?)\$/g, '$1')
        .replace(/\\text\{([^}]+)\}/g, '$1').replace(/\\boxed\{([^}]+)\}/g, '*$1*')
        .replace(/\\frac\{([^}]+)\}\{([^}]+)\}/g, '$1/$2')
        .replace(/\\sqrt\{([^}]+)\}/g, '√$1').replace(/\\cdot/g, '·')
        .replace(/\\times/g, '×').replace(/\\div/g, '÷')
        .replace(/\\implies/g, '=>').replace(/\\[a-zA-Z]+/g, '');

      // Asistan yanıtını kaydet
      await saveMsg(personId, 'assistant', reply);

      // Uzun cevabı böl (WhatsApp limit ~4096)
      const maxLen = 3900;
      const prefix = searchCtx ? '🌐 *(İnternet Destekli)*\n' : '';
      if (reply.length <= maxLen) {
        await msg.reply(prefix + reply);
      } else {
        const chunks = [];
        for (let i = 0; i < reply.length; i += maxLen) chunks.push(reply.slice(i, i + maxLen));
        for (let i = 0; i < chunks.length; i++)
          await msg.reply((i === 0 ? prefix : '') + `[${i + 1}/${chunks.length}]\n` + chunks[i]);
      }
      console.log(`[BOT] Yanıt verildi (${reply.length} karakter)`);

    } catch (e) {
      console.error('[BOT] Hata:', e.message);
      try { await msg.reply('Hata: ' + e.message.substring(0, 200)); } catch { }
    } finally {
      processingChats.delete(chat.id._serialized);
    }
  });
});

// ── Başlat ───────────────────────────────────────────────────
client.initialize();
console.log(`[BOT] Başlatılıyor… (Panel: ${PANEL})`);

// ── Hata yönetimi ────────────────────────────────────────────
process.on('unhandledRejection', (reason, promise) => {
  console.error('[BOT] Unhandled Rejection:', reason);
});

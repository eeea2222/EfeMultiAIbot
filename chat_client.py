#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chat_client.py — Bağımsız Web Chat Arayüzü

Kullanım:
    python chat_client.py                 # Port 5051, ana sunucu localhost:5050
    MAIN_SERVER=http://192.168.1.5:5050 python chat_client.py
    CHAT_PORT=8080 python chat_client.py

Gereksinimler:
    pip install flask requests
    (Ana sunucu app.py çalışıyor olmalı)
"""

import json
import logging
import os
import pathlib
import time
import uuid

from flask import (
    Flask, Response, jsonify, make_response, render_template_string,
    request, stream_with_context
)
import requests as http_req
import werkzeug.utils

# ─── Yapılandırma ────────────────────────────────────────────
MAIN_SERVER  = os.environ.get("MAIN_SERVER",  "http://127.0.0.1:5050")
CHAT_PORT    = int(os.environ.get("CHAT_PORT",  "5051"))
CHAT_HOST    = os.environ.get("CHAT_HOST",  "0.0.0.0")
APP_TITLE    = os.environ.get("APP_TITLE",  "EfeMultiAIbot")
COOKIE_NAME  = "wcuid"
COOKIE_TTL   = 60 * 60 * 24 * 365   # 1 yıl

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("chat_client")

app = Flask(__name__)

# ─── HTML Arayüzü ────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0d0b09">
<title>{{ title }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300;0,9..144,600;0,9..144,700;1,9..144,400&family=DM+Mono:wght@300;400;500&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.0.6/purify.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>
/* ═══ Reset & Tokens ═══ */
:root{
  --ink:#0d0b09;--ink2:#1a1714;--ink3:#252220;
  --paper:#f5f0e8;--paper2:#ede7da;--paper3:#e4ddd1;
  --amber:#c8780a;--amber-glow:rgba(200,120,10,.12);
  --lime:#4a7c59;--lime-glow:rgba(74,124,89,.10);
  --muted:#786b5e;--faint:#a89d91;--ghost:#d4ccc2;
  --user-bg:#1a1714;--user-border:rgba(200,120,10,.2);
  --asst-bg:#f0ead9;--asst-border:rgba(74,124,89,.15);
  --radius:14px;--mono:'DM Mono',monospace;--sans:'DM Sans',sans-serif;
  --serif:'Fraunces',Georgia,serif;
}
*{margin:0;padding:0;box-sizing:border-box}
html{height:100%;font-size:15px}
body{height:100%;background:var(--ink);color:var(--paper);
  font-family:var(--sans);display:flex;flex-direction:column;overflow:hidden;
  /* subtle grain */
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.035'/%3E%3C/svg%3E");
}
::-webkit-scrollbar{width:3px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--ink3);border-radius:2px}

/* ═══ Header ═══ */
.topbar{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 24px;height:58px;
  background:rgba(13,11,9,.92);
  border-bottom:1px solid rgba(255,255,255,.06);
  backdrop-filter:blur(12px);flex-shrink:0;gap:16px;
  position:relative;z-index:10;
}
.logo{
  font-family:var(--serif);font-size:19px;font-weight:600;
  letter-spacing:-.02em;color:var(--paper);
  display:flex;align-items:center;gap:10px;
}
.logo-dot{
  width:9px;height:9px;border-radius:50%;background:var(--amber);
  box-shadow:0 0 10px var(--amber);flex-shrink:0;
  animation:pulse 3s ease-in-out infinite;
}
@keyframes pulse{0%,100%{box-shadow:0 0 6px var(--amber)}50%{box-shadow:0 0 16px var(--amber)}}
.header-right{display:flex;align-items:center;gap:12px}
.quota-pill{
  display:flex;align-items:center;gap:6px;
  background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);
  border-radius:20px;padding:4px 12px;font-size:11.5px;
  font-family:var(--mono);color:var(--faint);letter-spacing:.01em;
  transition:all .3s;
}
.quota-pill.warn{border-color:rgba(200,120,10,.4);color:var(--amber)}
.quota-pill.full{border-color:rgba(220,60,60,.4);color:#dc6060}
.quota-num{color:var(--paper);font-weight:500}
.user-chip{
  background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);
  border-radius:20px;padding:5px 14px;font-size:12px;color:var(--faint);
  cursor:pointer;transition:all .2s;font-family:var(--sans);
  display:flex;align-items:center;gap:5px;white-space:nowrap;
}
.user-chip:hover{border-color:rgba(200,120,10,.4);color:var(--amber)}
.chip-label{pointer-events:none}

/* ═══ Messages ═══ */
#chat{flex:1;overflow-y:auto;padding:28px 0 12px;display:flex;flex-direction:column;gap:0}
.msg-wrap{
  display:flex;flex-direction:column;padding:8px 24px;
  animation:slideIn .22s cubic-bezier(.22,1,.36,1);max-width:100%;
}
@keyframes slideIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.msg-wrap.user{align-items:flex-end}
.msg-wrap.assistant{align-items:flex-start}

.msg-meta{
  display:flex;align-items:center;gap:8px;margin-bottom:5px;
  font-size:11px;color:var(--faint);font-family:var(--mono);
}
.msg-wrap.user .msg-meta{flex-direction:row-reverse}
.meta-name{font-weight:500}
.meta-time{opacity:.7}

.bubble{
  max-width:min(680px,88vw);padding:12px 16px;
  font-size:14.5px;line-height:1.72;word-break:break-word;
  position:relative;
}
.bubble.user-b{
  background:var(--user-bg);color:var(--paper);
  border:1px solid var(--user-border);border-radius:var(--radius) var(--radius) 4px var(--radius);
  font-family:var(--sans);
}
.bubble.asst-b{
  background:var(--asst-bg);color:#2a2318;
  border:1px solid var(--asst-border);border-radius:var(--radius) var(--radius) var(--radius) 4px;
  font-family:var(--sans);
}
.bubble pre{
  background:rgba(0,0,0,.08);border:1px solid rgba(0,0,0,.1);border-radius:8px;
  padding:12px 14px;margin:10px 0;overflow-x:auto;
  font-family:var(--mono);font-size:12.5px;line-height:1.6;
}
.bubble.user-b pre{background:rgba(255,255,255,.05);border-color:rgba(255,255,255,.08)}
.bubble code{
  font-family:var(--mono);font-size:12.5px;background:rgba(0,0,0,.08);
  padding:1px 5px;border-radius:4px;
}
.bubble.user-b code{background:rgba(255,255,255,.1)}
.bubble pre code{background:none;padding:0;font-size:inherit;display:block}
.bubble b,.bubble strong{font-weight:600}
.bubble i,.bubble em{font-style:italic}
.bubble p{margin-bottom:8px}
.bubble p:last-child{margin-bottom:0}
.bubble ul, .bubble ol {margin: 8px 0 8px 18px}
.bubble table {border-collapse: collapse; margin: 10px 0; width: 100%; font-size: 13.5px}
.bubble th, .bubble td {border: 1px solid rgba(0,0,0,.1); padding: 6px 10px}
.bubble.user-b th, .bubble.user-b td {border-color: rgba(255,255,255,.1)}

/* Cursor blink */
.cursor{
  display:inline-block;width:7px;height:14px;
  background:var(--lime);border-radius:1px;
  vertical-align:text-bottom;margin-left:1px;
  animation:cblink .65s step-end infinite;
}
@keyframes cblink{0%,100%{opacity:1}50%{opacity:0}}

/* Typing indicator */
.typing-wrap{padding:8px 24px;display:flex;align-items:flex-start;animation:slideIn .2s ease-out}
.typing-bubble{
  background:var(--asst-bg);border:1px solid var(--asst-border);
  border-radius:var(--radius) var(--radius) var(--radius) 4px;
  padding:12px 18px;display:flex;align-items:center;gap:5px;
}
.typing-dot{
  width:6px;height:6px;border-radius:50%;background:var(--lime);
  animation:tdot 1.1s infinite ease-in-out;
}
.typing-dot:nth-child(2){animation-delay:.18s}
.typing-dot:nth-child(3){animation-delay:.36s}
@keyframes tdot{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-7px)}}

/* ═══ Welcome Screen ═══ */
.welcome{
  flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:6px;padding:40px 24px;
  pointer-events:none;
}
.welcome-icon{font-size:48px;margin-bottom:8px}
.welcome-title{
  font-family:var(--serif);font-size:28px;font-weight:300;
  color:var(--paper);letter-spacing:-.02em;text-align:center;
}
.welcome-sub{font-size:13px;color:var(--muted);text-align:center;line-height:1.6;max-width:400px}
.welcome-chips{
  display:flex;flex-wrap:wrap;gap:8px;margin-top:20px;
  justify-content:center;pointer-events:all;max-width:560px;
}
.chip{
  background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);
  border-radius:20px;padding:7px 16px;font-size:12.5px;cursor:pointer;
  color:var(--faint);transition:all .2s;
}
.chip:hover{background:var(--amber-glow);border-color:rgba(200,120,10,.35);color:var(--amber)}

/* ═══ Rate Limit Banner ═══ */
.rate-banner{
  margin:0 24px 16px;background:rgba(220,60,60,.08);
  border:1px solid rgba(220,60,60,.25);border-radius:10px;
  padding:10px 16px;font-size:12.5px;color:#dc6060;
  display:flex;align-items:center;gap:8px;
}
.rate-banner .rate-icon{font-size:16px}

/* ═══ Input ═══ */
.input-zone{
  padding:14px 24px 20px;background:rgba(13,11,9,.95);
  border-top:1px solid rgba(255,255,255,.06);flex-shrink:0;
  backdrop-filter:blur(12px);
}
.input-box{
  display:flex;align-items:flex-end;gap:10px;
  background:rgba(255,255,255,.05);
  border:1.5px solid rgba(255,255,255,.1);border-radius:var(--radius);
  padding:10px 14px;transition:border-color .2s;
}
.input-box:focus-within{border-color:rgba(200,120,10,.45);box-shadow:0 0 0 3px rgba(200,120,10,.06)}
.input-box.disabled{opacity:.45;pointer-events:none}
#msg-input{
  flex:1;background:none;border:none;outline:none;resize:none;
  color:var(--paper);font-family:var(--sans);font-size:14px;
  line-height:1.6;max-height:120px;overflow-y:auto;
}
#msg-input::placeholder{color:var(--muted)}
.send-btn{
  width:36px;height:36px;flex-shrink:0;border:none;border-radius:9px;
  background:var(--amber);cursor:pointer;display:flex;align-items:center;
  justify-content:center;transition:all .2s;
  box-shadow:0 2px 12px rgba(200,120,10,.35);
}
.send-btn:hover:not(:disabled){background:#d98a1c;transform:translateY(-1px)}
.send-btn:disabled{opacity:.35;cursor:not-allowed;box-shadow:none;transform:none}
.send-btn.stop-mode{background:#c0392b;box-shadow:0 2px 12px rgba(192,57,43,.35)}
.send-btn svg{width:15px;height:15px;fill:none;stroke:#fff;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.input-hint{text-align:center;margin-top:7px;font-size:11px;color:var(--muted);letter-spacing:.01em}
.mobile-hint{display:none}
@media(max-width:640px){.desktop-hint{display:none}.mobile-hint{display:inline}}

/* ═══ Username Modal ═══ */
.modal-backdrop{
  position:fixed;inset:0;background:rgba(0,0,0,.75);backdrop-filter:blur(8px);
  z-index:100;display:flex;align-items:center;justify-content:center;
  animation:fadeIn .2s ease;
}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.modal{
  background:var(--ink2);border:1px solid rgba(255,255,255,.12);
  border-radius:16px;padding:28px;width:340px;max-width:90vw;
  animation:scaleIn .2s cubic-bezier(.22,1,.36,1);
}
@keyframes scaleIn{from{transform:scale(.94);opacity:0}to{transform:scale(1);opacity:1}}
.modal-title{
  font-family:var(--serif);font-size:18px;font-weight:600;
  color:var(--paper);margin-bottom:6px;
}
.modal-sub{font-size:12.5px;color:var(--muted);margin-bottom:20px;line-height:1.6}
.modal input{
  width:100%;background:rgba(255,255,255,.06);border:1.5px solid rgba(255,255,255,.1);
  border-radius:9px;padding:10px 13px;color:var(--paper);
  font-family:var(--sans);font-size:14px;outline:none;transition:border-color .2s;
  margin-bottom:14px;
}
.modal input:focus{border-color:rgba(200,120,10,.5)}
.modal-btn{
  width:100%;padding:10px;border:none;border-radius:9px;
  background:var(--amber);color:#fff;font-family:var(--sans);
  font-size:14px;font-weight:600;cursor:pointer;transition:all .2s;
  letter-spacing:.01em;
}
.modal-btn:hover{background:#d98a1c}

/* ═══ Experts & Sandbox Modals ═══ */
.settings-beta-warn{
  background:rgba(220,160,20,.1);border:1px solid rgba(220,160,20,.3);
  border-radius:8px;padding:10px;font-size:12px;color:#dca814;
  margin-bottom:14px;line-height:1.5;
}
.setting-row{
  display:flex;align-items:center;justify-content:space-between;
  padding:12px 14px;background:rgba(255,255,255,.03);
  border:1px solid rgba(255,255,255,.06);border-radius:10px;margin-bottom:10px;
}
.setting-info{display:flex;flex-direction:column;gap:4px}
.setting-title{font-size:14px;color:var(--paper);font-weight:500}
.setting-desc{font-size:11.5px;color:var(--muted)}
.toggle-switch{
  position:relative;width:40px;height:22px;background:rgba(255,255,255,.1);
  border-radius:20px;cursor:pointer;transition:all .2s;
}
.toggle-switch.on{background:var(--amber)}
.toggle-switch::after{
  content:'';position:absolute;top:2px;left:2px;width:18px;height:18px;
  background:#fff;border-radius:50%;transition:all .2s;
}
.toggle-switch.on::after{transform:translateX(18px)}

.sandbox-list{
  max-height:300px;overflow-y:auto;background:rgba(0,0,0,.15);
  border-radius:10px;border:1px solid rgba(255,255,255,.05);
  margin-bottom:14px;
}
.sb-file{
  display:flex;align-items:center;justify-content:space-between;
  padding:10px 14px;border-bottom:1px solid rgba(255,255,255,.05);
  font-size:13px;
}
.sb-file:last-child{border-bottom:none}
.sb-file-name{color:var(--paper);font-family:var(--mono)}
.sb-actions{display:flex;gap:8px}
.sb-btn{
  background:rgba(255,255,255,.08);border:none;border-radius:6px;
  color:var(--paper);font-size:11px;padding:4px 8px;cursor:pointer;
}
.sb-btn:hover{background:rgba(255,255,255,.15)}
.sb-btn.del:hover{background:#dc6060;color:#fff}

.code-header{
  display:flex;justify-content:space-between;align-items:center;
  background:rgba(0,0,0,.3);padding:6px 14px;border-radius:8px 8px 0 0;
  font-size:11px;font-family:var(--sans);color:var(--faint);
  margin:10px 0 -10px 0;border:1px solid rgba(255,255,255,.05);
  border-bottom:none;
}
.bubble.user-b .code-header{background:rgba(255,255,255,.08)}

/* ═══ LLM Offline Banner ═══ */
.offline-banner{
  position:fixed;bottom:90px;left:50%;transform:translateX(-50%);
  background:rgba(220,60,60,.12);border:1px solid rgba(220,60,60,.3);
  border-radius:10px;padding:10px 20px;font-size:12.5px;color:#e07070;
  z-index:50;display:none;white-space:nowrap;
}

/* ═══ Thinking Bubble (Double Prompt) ═══ */
.thinking-wrap{padding:8px 24px;display:flex;align-items:flex-start;animation:slideIn .22s ease-out}
.thinking-bubble{
  background:linear-gradient(135deg, var(--asst-bg) 0%, rgba(200,120,10,.08) 50%, var(--asst-bg) 100%);
  background-size:300% 300%;
  animation:shimmer 2s ease infinite;
  border:1px solid rgba(200,120,10,.25);border-radius:var(--radius) var(--radius) var(--radius) 4px;
  padding:16px 20px;display:flex;flex-direction:column;align-items:center;gap:10px;
  min-width:220px;
}
@keyframes shimmer{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
.thinking-icon{font-size:28px;animation:brainPulse 1.5s ease-in-out infinite}
@keyframes brainPulse{0%,100%{transform:scale(1)}50%{transform:scale(1.15)}}
.thinking-label{font-size:13px;color:#2a2318;font-family:var(--sans);font-weight:500;text-align:center}
.thinking-countdown{
  font-family:var(--mono);font-size:32px;font-weight:700;
  color:var(--amber);line-height:1;
}
.thinking-sub{font-size:11px;color:var(--muted);font-family:var(--mono)}
.thinking-progress{
  width:100%;height:4px;background:rgba(0,0,0,.08);border-radius:2px;
  overflow:hidden;margin-top:4px;
}
.thinking-progress-bar{
  height:100%;background:var(--amber);border-radius:2px;
  width:100%;transition:width linear;
  animation:progressShrink var(--countdown-duration, 8s) linear forwards;
}
@keyframes progressShrink{0%{width:100%}100%{width:0%}}
.thinking-result{
  background:rgba(74,124,89,.1);border:1px solid rgba(74,124,89,.2);
  border-radius:8px;padding:8px 14px;font-family:var(--mono);font-size:13px;
  color:#2a2318;width:100%;text-align:center;animation:fadeIn .2s ease;
}
.thinking-result .expr{color:var(--muted);font-size:11px}
.thinking-result .result{font-size:18px;font-weight:600;color:var(--lime)}

/* ═══ File Attachment ═══ */
.attach-btn{
  background:none;border:none;cursor:pointer;
  color:var(--muted);font-size:20px;padding:6px 8px;
  transition:color .2s;flex-shrink:0;line-height:1;
}
.attach-btn:hover{color:var(--amber)}
.preview-strip{
  display:flex;gap:8px;padding:6px 12px;overflow-x:auto;
  background:rgba(255,255,255,.03);border-top:1px solid rgba(255,255,255,.06);
  flex-shrink:0;min-height:0;
}
.preview-strip:empty{display:none;padding:0;border:0;min-height:0}
.preview-item{
  position:relative;border-radius:8px;overflow:hidden;
  border:1px solid rgba(255,255,255,.1);flex-shrink:0;
  animation:fadeIn .2s ease;
}
.preview-item img{
  height:56px;width:auto;max-width:80px;object-fit:cover;display:block;
}
.preview-item .file-chip{
  padding:8px 12px;font-size:11px;color:var(--paper);
  font-family:var(--mono);background:rgba(255,255,255,.06);
  display:flex;align-items:center;gap:6px;max-width:140px;
}
.preview-item .file-chip .fname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.preview-item .file-chip .ficon{font-size:16px;flex-shrink:0}
.preview-remove{
  position:absolute;top:2px;right:2px;width:18px;height:18px;
  background:rgba(0,0,0,.7);color:#fff;border:none;border-radius:50%;
  font-size:11px;cursor:pointer;display:flex;align-items:center;
  justify-content:center;line-height:1;
}
.preview-remove:hover{background:#dc6060}

/* Chat bubble images */
.bubble-img{
  max-width:280px;max-height:240px;border-radius:8px;
  margin:8px 0 4px;cursor:pointer;transition:opacity .2s;
}
.bubble-img:hover{opacity:.85}
.bubble-file-card{
  display:inline-flex;align-items:center;gap:8px;
  background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);
  border-radius:8px;padding:8px 14px;margin:6px 0;
  font-size:12px;color:var(--paper);text-decoration:none;
  transition:background .2s;font-family:var(--mono);
}
.bubble-file-card:hover{background:rgba(255,255,255,.12)}
.bubble.user-b .bubble-file-card{background:rgba(255,255,255,.08);border-color:rgba(200,120,10,.15)}
.bubble.user-b .bubble-img{border:1px solid rgba(200,120,10,.15)}

/* Files Modal */
.files-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));
  gap:10px;max-height:400px;overflow-y:auto;padding:4px;
}
.file-card{
  border:1px solid rgba(255,255,255,.08);border-radius:10px;
  overflow:hidden;background:rgba(255,255,255,.03);
  transition:border-color .2s;cursor:pointer;position:relative;
}
.file-card:hover{border-color:rgba(200,120,10,.3)}
.file-card-thumb{
  width:100%;height:80px;object-fit:cover;display:block;
  background:rgba(0,0,0,.2);
}
.file-card-icon{
  width:100%;height:80px;display:flex;align-items:center;
  justify-content:center;font-size:28px;background:rgba(0,0,0,.15);
}
.file-card-info{padding:8px;font-size:11px}
.file-card-name{
  color:var(--paper);font-family:var(--mono);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;
}
.file-card-size{color:var(--muted);margin-top:2px}
.file-card-del{
  position:absolute;top:4px;right:4px;width:22px;height:22px;
  background:rgba(0,0,0,.6);color:#fff;border:none;border-radius:50%;
  font-size:12px;cursor:pointer;display:none;align-items:center;
  justify-content:center;z-index:2;
}
.file-card:hover .file-card-del{display:flex}
.file-card-del:hover{background:#dc6060}
</style>
<style>
/* ═══ Mobile & Tablet Responsive ═══ */

/* Safe area for notch phones (iPhone X+) */
@supports(padding: env(safe-area-inset-top)){
  .topbar{padding-top:env(safe-area-inset-top);height:calc(58px + env(safe-area-inset-top))}
  .input-zone{padding-bottom:calc(20px + env(safe-area-inset-bottom))}
  .offline-banner{bottom:calc(90px + env(safe-area-inset-bottom))}
}

/* ─── Tablet (640px - 1024px) ─── */
@media(max-width:1024px){
  .topbar{padding:0 16px;gap:10px}
  .logo{font-size:17px;gap:8px}
  .header-right{gap:8px}
  .quota-pill{padding:3px 8px;font-size:10.5px}
  .user-chip{padding:5px 10px;font-size:11px}
  .msg-wrap{padding:8px 16px}
  .typing-wrap{padding:8px 16px}
  .bubble{max-width:min(680px,92vw)}
  .input-zone{padding:12px 16px 16px}
  .welcome{padding:30px 16px}
}

/* ─── Mobile (< 640px) ─── */
@media(max-width:640px){
  html{font-size:14px}
  .topbar{padding:0 12px;height:50px;gap:6px}
  .logo{font-size:15px;gap:6px}
  .logo-dot{width:7px;height:7px}
  #app-title{max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

  /* Header: hide text labels, show only emoji icons */
  .header-right{gap:4px}
  .quota-pill{display:none}
  .chip-label{display:none !important}
  .user-chip{padding:6px 8px;font-size:16px;border-radius:10px;
    min-width:36px;min-height:36px;justify-content:center}
  #user-chip{font-size:13px;max-width:60px;overflow:hidden;text-overflow:ellipsis}

  /* Messages */
  #chat{padding:12px 0 8px}
  .msg-wrap{padding:5px 10px}
  .msg-meta{font-size:10px;gap:5px;margin-bottom:3px}
  .bubble{max-width:92vw;padding:10px 13px;font-size:13.5px;line-height:1.65}
  .bubble pre{padding:10px 12px;font-size:11.5px;-webkit-overflow-scrolling:touch}
  .bubble code{font-size:11.5px;padding:1px 4px}
  .bubble table{font-size:12px}
  .bubble th,.bubble td{padding:4px 6px}

  /* Typing */
  .typing-wrap{padding:5px 10px}

  /* Welcome */
  .welcome{padding:24px 12px;gap:4px}
  .welcome-icon{font-size:36px;margin-bottom:4px}
  .welcome-title{font-size:22px}
  .welcome-sub{font-size:12px;max-width:300px}
  .welcome-chips{gap:6px;margin-top:14px;max-width:100%}
  .chip{padding:6px 12px;font-size:11.5px}

  /* Input */
  .input-zone{padding:10px 10px 14px}
  .input-box{padding:8px 10px;gap:8px;border-radius:12px}
  #msg-input{font-size:15px;max-height:100px;-webkit-appearance:none}
  .send-btn{width:40px;height:40px;border-radius:10px;min-width:40px}
  .send-btn svg{width:16px;height:16px}
  .input-hint{font-size:10px;margin-top:5px}

  /* Rate banner */
  .rate-banner{margin:0 10px 10px;padding:8px 12px;font-size:11.5px}

  /* Modals: full-width on mobile */
  .modal{width:100%;max-width:100vw;border-radius:16px 16px 0 0;
    position:fixed;bottom:0;left:0;right:0;max-height:85vh;overflow-y:auto;
    animation:slideUp .25s cubic-bezier(.22,1,.36,1)}
  .modal-backdrop{align-items:flex-end}
  @keyframes slideUp{from{transform:translateY(100%)}to{transform:translateY(0)}}
  .modal-title{font-size:16px}
  .modal-sub{font-size:12px}
  .modal input{font-size:16px;padding:12px 13px}
  .modal-btn{padding:12px;font-size:15px;min-height:44px}

  /* Settings/Sandbox modal */
  .modal[style*="width:480px"]{width:100% !important}
  .setting-row{padding:10px 12px}
  .setting-title{font-size:13px}
  .setting-desc{font-size:10.5px}
  .toggle-switch{min-width:40px}
  .sb-file{padding:10px 12px;font-size:12px}
  .sb-btn{padding:6px 10px;font-size:11px;min-height:32px}

  /* Code header */
  .code-header{padding:5px 10px;font-size:10px}

  /* Offline banner */
  .offline-banner{bottom:75px;padding:8px 14px;font-size:11.5px;border-radius:8px;
    left:10px;right:10px;transform:none;white-space:normal;text-align:center}
}

/* ─── Very small phones (< 380px) ─── */
@media(max-width:380px){
  .topbar{height:46px;padding:0 8px}
  .logo{font-size:14px}
  .msg-wrap{padding:4px 8px}
  .bubble{max-width:95vw;padding:8px 10px;font-size:13px}
  .input-zone{padding:8px 8px 10px}
  .welcome-title{font-size:20px}
  .chip{padding:5px 10px;font-size:11px}
}

/* Touch optimizations */
@media(hover:none) and (pointer:coarse){
  .chip:hover,.user-chip:hover,.send-btn:hover:not(:disabled),.sb-btn:hover,.modal-btn:hover{transform:none}
  .chip:active{background:var(--amber-glow);border-color:rgba(200,120,10,.35);color:var(--amber)}
  .user-chip:active{border-color:rgba(200,120,10,.4);color:var(--amber)}
  .send-btn:active:not(:disabled){background:#d98a1c;transform:scale(.95)}
  #msg-input{font-size:16px} /* prevents zoom on iOS */
}

/* Landscape phone */
@media(max-height:500px) and (orientation:landscape){
  .topbar{height:40px;padding:0 12px}
  .logo{font-size:14px}
  #chat{padding:6px 0}
  .msg-wrap{padding:3px 10px}
  .input-zone{padding:6px 10px 8px}
  .welcome{padding:10px 12px}
  .welcome-icon{font-size:24px;margin-bottom:2px}
  .welcome-title{font-size:18px}
  .welcome-chips{margin-top:8px}
}
</style>
</head>
<body>

<!-- TOPBAR -->
<div class="topbar">
  <div class="logo">
    <div class="logo-dot"></div>
    <span id="app-title">{{ title }}</span>
  </div>
  <div class="header-right">
    <div class="quota-pill" id="quota-pill" title="Bu saatteki / günlük mesaj kullanımı">
      <span>⏱</span>
      <span id="quota-h" class="quota-num">–</span>/<span id="quota-h-lim">–</span>
      &nbsp;·&nbsp;
      <span>📅</span>
      <span id="quota-d" class="quota-num">–</span>/<span id="quota-d-lim">–</span>
    </div>
    <div class="user-chip" id="newchat-chip" onclick="startNewChat()" title="Yeni Sohbet Başlat">
      🗑️<span class="chip-label"> Yeni</span>
    </div>
    <div class="user-chip" id="settings-chip" onclick="openSettingsModal()" title="AI Uzmanları / Ayarlar">
      ⚙️<span class="chip-label"> Uzmanlar</span>
    </div>
    <div class="user-chip" id="sandbox-chip" onclick="openSandboxModal()" title="Kişisel Sandbox Dosyaları">
      📁<span class="chip-label"> Sandbox</span>
    </div>
    <div class="user-chip" id="files-chip" onclick="openFilesModal()" title="Yüklenen Dosyalar">
      📂<span class="chip-label"> Dosyalar</span>
    </div>
    <div class="user-chip" id="user-chip" onclick="openUsernameModal()">
      👤 <span id="username-display">...</span>
    </div>
  </div>
</div>

<!-- MESSAGES -->
<div id="chat">
  <div class="welcome" id="welcome">
    <div class="welcome-icon">✦</div>
    <div class="welcome-title">{{ title }}</div>
    <div class="welcome-sub">Yapay zeka asistanınla konuşmaya başla.<br>Sana her konuda yardımcı olmaya hazır.</div>
    <div class="welcome-chips">
      <div class="chip" onclick="quickSend('Merhaba! Kendini kısaca tanıt.')">Kendini tanıt</div>
      <div class="chip" onclick="quickSend('Python\'da bir web scraper yaz.')">Python Scraper</div>
      <div class="chip" onclick="quickSend('Kuantum bilgisayarları nedir? Basitçe anlat.')">Kuantum BG</div>
      <div class="chip" onclick="quickSend('Bana güzel bir şiir yaz.')">Şiir Yaz</div>
      <div class="chip" onclick="quickSend('İstanbul\'da gezilecek 5 yer öner.')">İstanbul Turu</div>
      <div class="chip" onclick="quickSend('SQL JOIN türlerini karşılaştır.')">SQL JOIN</div>
    </div>
  </div>
</div>

<div class="offline-banner" id="offline-banner">⚠ AI sunucusu kapalı — yöneticinizle iletişime geçin</div>

<!-- INPUT -->
<div class="input-zone">
  <div class="preview-strip" id="preview-strip"></div>
  <div class="input-box" id="input-box">
    <button class="attach-btn" id="attach-btn" onclick="triggerFilePicker()" title="Dosya ekle (resim, belge, kod)">📎</button>
    <textarea id="msg-input" rows="1" placeholder="Mesajını yaz… (Enter gönderin, Shift+Enter yeni satır)"></textarea>
    <button class="send-btn" id="send-btn" onclick="handleSend()" title="Gönder">
      <svg id="send-icon" viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
    </button>
  </div>
  <input type="file" id="file-picker" style="display:none" multiple
    accept="image/*,.pdf,.txt,.md,.csv,.py,.js,.ts,.html,.css,.json,.xml,.yaml,.yml,.c,.cpp,.h,.java,.rs,.go,.sh,.sql,.doc,.docx,.rtf">
  <div class="input-hint"><span class="desktop-hint">Enter → Gönder &nbsp;·&nbsp; Shift+Enter → Satır &nbsp;·&nbsp; 📎 Dosya ekle &nbsp;·&nbsp; Esc → Durdur</span><span class="mobile-hint">Mesajını yaz ve gönder butonuna bas</span></div>
</div>

<!-- USERNAME MODAL -->
<div class="modal-backdrop" id="username-modal" style="display:none" onclick="if(event.target===this&&state.uid)closeUsernameModal()">
  <div class="modal">
    <div class="modal-title">Nasıl görünmek istersin?</div>
    <div class="modal-sub">Bir kullanıcı adı seç. Dilediğinde değiştirebilirsin.</div>
    <input type="text" id="username-input" placeholder="Kullanıcı adın…" maxlength="32"
      onkeydown="if(event.key==='Enter')saveUsername()">
    <button class="modal-btn" onclick="saveUsername()">Devam Et →</button>
  </div>
</div>

<!-- SETTINGS MODAL -->
<div class="modal-backdrop" id="settings-modal" style="display:none" onclick="if(event.target===this)closeSettingsModal()">
  <div class="modal">
    <div class="modal-title">AI Uzmanları</div>
    <div class="modal-sub">Yapay zekaya yeni yetenekler ekle. Değişiklikler anında kaydedilir.</div>
    <div class="settings-beta-warn">
      <b style="color:#f39c12">⚠ DİKKAT (BETA)</b><br>
      Uzmanlar yapay zekanın gerçek dünyada işlem yapmasını sağlar (örn. Sandbox içinde Python kodu çalıştırmak). Test aşamasındadır, lütfen dikkatli kullanın!
    </div>
    
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-title">Hesap Makinesi 🧮</div>
        <div class="setting-desc">Kesin matematiksel hesaplamalar yapar.</div>
      </div>
      <div class="toggle-switch" id="toggle-calculator" onclick="toggleExpert('calculator')"></div>
    </div>
    
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-title">Python Yürütücü 💻</div>
        <div class="setting-desc">Gerçek Python kodunu Sandbox'ta saklar ve çalıştırır.</div>
      </div>
      <div class="toggle-switch" id="toggle-sandbox" onclick="toggleExpert('sandbox')"></div>
    </div>
    
    <button class="modal-btn" onclick="closeSettingsModal()">Kapat</button>
  </div>
</div>

<!-- SANDBOX MODAL -->
<div class="modal-backdrop" id="sandbox-modal" style="display:none" onclick="if(event.target===this)closeSandboxModal()">
  <div class="modal" style="width:480px">
    <div class="modal-title">📁 Senin Sandbox'ın</div>
    <div class="modal-sub">AI'ın oluşturduğu veya senin kaydettiğin dosyalar.</div>
    
    <div class="sandbox-list" id="sandbox-list">
      <div style="padding:15px;text-align:center;color:var(--muted);font-size:13px">Yükleniyor...</div>
    </div>
    
    <button class="modal-btn" onclick="closeSandboxModal()">Kapat</button>
  </div>
</div>

<!-- FILES MODAL -->
<div class="modal-backdrop" id="files-modal" style="display:none" onclick="if(event.target===this)closeFilesModal()">
  <div class="modal" style="width:520px">
    <div class="modal-title">📂 Dosyalarım</div>
    <div class="modal-sub">Yüklediğin resimler, belgeler ve kodlar. Tıkla → indir.
      <button class="sb-btn" style="margin-left:8px" onclick="triggerFilePicker()">+ Yükle</button>
    </div>
    <div class="files-grid" id="files-grid">
      <div style="padding:20px;text-align:center;color:var(--muted);font-size:13px;grid-column:1/-1">Yükleniyor...</div>
    </div>
    <button class="modal-btn" onclick="closeFilesModal()">Kapat</button>
  </div>
</div>

<script>
// ─── State ───────────────────────────────────────────────────
const state = {
  uid: null, username: 'Anonim',
  limits: {hourly_used:0, hourly_limit:20, daily_used:0, daily_limit:100, max_tokens:2048},
  generating: false, abortCtrl: null,
  history: [],  // local mirror for display
  pendingFiles: [],  // [{name, size, type, dataUrl, b64}]
};

// ─── Helpers ─────────────────────────────────────────────────
function $(id){ return document.getElementById(id); }
function escHtml(t){
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function fmtTime(ts){ return new Date(ts*1000).toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit'}); }
function nowSec(){ return Math.floor(Date.now()/1000); }

// Cookie helpers
function setCookie(name, val, maxAge){
  document.cookie = `${name}=${val}; max-age=${maxAge}; path=/; SameSite=Lax`;
}
function getCookie(name){
  return (document.cookie.split(';').map(c=>c.trim())
    .find(c=>c.startsWith(name+'=')) || '').split('=').slice(1).join('=') || null;
}
function genUUID(){
  return ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g,c=>
    (c^crypto.getRandomValues(new Uint8Array(1))[0]&15>>c/4).toString(16));
}

// ─── Session init ─────────────────────────────────────────────
async function initSession(){
  let uid = getCookie('wcuid');
  if(!uid){ uid = genUUID(); setCookie('wcuid', uid, 31536000); }
  state.uid = uid;

  const storedName = localStorage.getItem('wc_username');
  const username   = storedName || 'Anonim';
  state.username   = username;
  $('username-display').textContent = username;

  // Register with main server
  try {
    const r = await fetch('/api/session', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ uid, username })
    });
    const d = await r.json();
    if(d.ok){
      state.limits = d.limits || state.limits;
      if(d.user?.username && d.user.username !== 'Anonim'){
        state.username = d.user.username;
        localStorage.setItem('wc_username', d.user.username);
        $('username-display').textContent = d.user.username;
      }
      updateQuotaPill();
    }
  } catch(e){
    console.warn('Session register failed:', e.message);
  }

  if(!storedName){ openUsernameModal(); }

  loadHistory();
}

// ─── Quota pill ───────────────────────────────────────────────
function updateQuotaPill(){
  const {hourly_used, hourly_limit, daily_used, daily_limit} = state.limits;
  $('quota-h').textContent = hourly_used ?? '–';
  $('quota-h-lim').textContent = hourly_limit ?? '–';
  $('quota-d').textContent = daily_used ?? '–';
  $('quota-d-lim').textContent = daily_limit ?? '–';

  const pill = $('quota-pill');
  const hourRatio = hourly_limit ? hourly_used/hourly_limit : 0;
  const dayRatio  = daily_limit  ? daily_used/daily_limit   : 0;
  const ratio = Math.max(hourRatio, dayRatio);
  pill.className = 'quota-pill' + (ratio>=1 ? ' full' : ratio>=0.8 ? ' warn' : '');
}

// ─── Username modal ───────────────────────────────────────────
function openUsernameModal(){
  $('username-input').value = state.username === 'Anonim' ? '' : state.username;
  $('username-modal').style.display = 'flex';
  setTimeout(()=>$('username-input').focus(), 50);
}
function closeUsernameModal(){ $('username-modal').style.display = 'none'; }
async function saveUsername(){
  const name = $('username-input').value.trim() || 'Anonim';
  const oldName = state.username;

  // Hesap değiştiyse yeni UID oluştur — eski geçmiş kalır, yeni temiz başlar
  if(name !== oldName && oldName !== 'Anonim'){
    const newUid = genUUID();
    state.uid = newUid;
    setCookie('wcuid', newUid, 31536000);
    clearChatDisplay();
  }

  state.username = name;
  localStorage.setItem('wc_username', name);
  $('username-display').textContent = name;
  closeUsernameModal();

  // Yeni hesabı sunucuya kaydet
  try {
    await fetch('/api/session', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ uid: state.uid, username: name })
    });
  } catch(e){}

  // Geçmişi yükle (yeni hesapsa boş gelecek)
  loadHistory();
}

// ─── Clear chat display ─────────────────────────────────────────
function clearChatDisplay(){
  const chat = $('chat');
  chat.innerHTML = '';
  // Welcome ekranını geri koy
  const w = document.createElement('div');
  w.className = 'welcome'; w.id = 'welcome';
  w.innerHTML = `
    <div class="welcome-icon">✦</div>
    <div class="welcome-title">${$('app-title').textContent}</div>
    <div class="welcome-sub">Yapay zeka asistanınla konuşmaya başla.<br>Sana her konuda yardımcı olmaya hazır.</div>
    <div class="welcome-chips">
      <div class="chip" onclick="quickSend('Merhaba! Kendini kısaca tanıt.')">Kendini tanıt</div>
      <div class="chip" onclick="quickSend('Bana güzel bir şiir yaz.')">Şiir Yaz</div>
      <div class="chip" onclick="quickSend('İstanbul\\'da gezilecek 5 yer öner.')">Tur Öner</div>
    </div>
  `;
  chat.appendChild(w);
  state.history = [];
  hideRateBanner();
}

// ─── Yeni Sohbet başlat ─────────────────────────────────────────
async function startNewChat(){
  if(state.generating) return;
  if(!confirm('Mevcut sohbeti silip yeni başlatmak istiyor musun?')) return;

  // Sunucudaki geçmişi temizle
  try {
    await fetch(`/api/history/clear`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ uid: state.uid })
    });
  } catch(e){}

  clearChatDisplay();
}

// ─── Load history ─────────────────────────────────────────────
async function loadHistory(){
  try{
    const r = await fetch(`/api/history?uid=${state.uid}&limit=30`);
    const d = await r.json();
    if(!d.ok || !Array.isArray(d.messages) || !d.messages.length) return;
    hideWelcome();
    d.messages.forEach(m => appendBubble(m.role, m.content, null, false));
    scrollToBottom();
  }catch(e){ console.warn('History load failed:', e.message); }
}

// ─── Chat rendering ───────────────────────────────────────────
function hideWelcome(){ const w=$('welcome'); if(w) w.remove(); }

function appendBubble(role, text, ts, animate=true){
  const wrap = document.createElement('div');
  wrap.className = `msg-wrap ${role}`;
  if(!animate) wrap.style.animation='none';

  const meta = document.createElement('div');
  meta.className = 'msg-meta';
  const nameEl = document.createElement('span');
  nameEl.className = 'meta-name';
  nameEl.textContent = role==='user' ? state.username : 'Asistan';
  const timeEl = document.createElement('span');
  timeEl.className = 'meta-time';
  timeEl.textContent = ts ? fmtTime(ts) : new Date().toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit'});
  meta.appendChild(nameEl);
  meta.appendChild(timeEl);

  const bubble = document.createElement('div');
  bubble.className = `bubble ${role==='user' ? 'user-b' : 'asst-b'}`;
  renderBubbleContent(bubble, text);

  wrap.appendChild(meta);
  wrap.appendChild(bubble);
  $('chat').appendChild(wrap);
  return bubble;
}

function renderBubbleContent(el, text){
  if (typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
    if (!marked.defaults.highlight) {
      marked.setOptions({
        breaks: true,
        highlight: function(code, lang) {
          if (lang && hljs.getLanguage(lang)) {
            return hljs.highlight(code, { language: lang }).value;
          }
          return hljs.highlightAuto(code).value;
        }
      });
    }
    el.innerHTML = DOMPurify.sanitize(marked.parse(text));
    
    // Add "Save to Sandbox" button for python code blocks
    const preBlocks = el.querySelectorAll('pre');
    preBlocks.forEach(pre => {
      const code = pre.querySelector('code');
      if(code && code.className.includes('language-python')){
        const header = document.createElement('div');
        header.className = 'code-header';
        
        const langSpan = document.createElement('span');
        langSpan.textContent = 'python';
        
        const saveBtn = document.createElement('button');
        saveBtn.className = 'sb-btn';
        saveBtn.innerHTML = '💾 Sandbox\'a Kaydet';
        saveBtn.onclick = () => saveToSandbox(code.textContent);
        
        header.appendChild(langSpan);
        header.appendChild(saveBtn);
        pre.parentNode.insertBefore(header, pre);
      }
    });
  } else {
    el.innerHTML = '';
    const parts = text.split(/(```[\s\S]*?```)/g);
    for(const part of parts){
      if(part.startsWith('```')){
        const lines = part.split('\n');
        const pre = document.createElement('pre');
        const code = document.createElement('code');
        code.textContent = lines.slice(1,-1).join('\n');
        pre.appendChild(code); el.appendChild(pre);
      } else {
        const span = document.createElement('span');
        span.innerHTML = escHtml(part)
          .replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>')
          .replace(/\*(.*?)\*/g,'<em>$1</em>')
          .replace(/`([^`]+)`/g,'<code>$1</code>')
          .replace(/\n/g,'<br>');
        el.appendChild(span);
      }
    }
  }
}

function showTyping(){
  const w = document.createElement('div');
  w.className='typing-wrap'; w.id='typing-indicator';
  w.innerHTML='<div class="typing-bubble"><div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div></div>';
  $('chat').appendChild(w);
  scrollToBottom();
}
function hideTyping(){ const t=$('typing-indicator'); if(t) t.remove(); }

function scrollToBottom(){ const c=$('chat'); c.scrollTop=c.scrollHeight; }

// ─── Rate limit banner ────────────────────────────────────────
function showRateBanner(msg){
  let b = $('rate-banner');
  if(!b){
    b = document.createElement('div');
    b.id = 'rate-banner';
    b.className = 'rate-banner';
    $('chat').appendChild(b);
  }
  b.innerHTML = `<span class="rate-icon">⛔</span> ${escHtml(msg)}`;
  scrollToBottom();
}
function hideRateBanner(){ const b=$('rate-banner'); if(b) b.remove(); }

// ─── File Attachment ──────────────────────────────────────────
const FILE_ICONS = {
  pdf:'📄', txt:'📝', md:'📝', csv:'📊', doc:'📄', docx:'📄', rtf:'📄',
  py:'🐍', js:'⚡', ts:'⚡', html:'🌐', css:'🎨', json:'📋', xml:'📋',
  yaml:'📋', yml:'📋', c:'⚙️', cpp:'⚙️', h:'⚙️', java:'☕', rs:'🦀',
  go:'🔹', sh:'💻', sql:'🗃️',
};
const IMG_EXTS = new Set(['png','jpg','jpeg','gif','webp','bmp','svg']);

function triggerFilePicker(){
  $('file-picker').click();
}

$('file-picker').addEventListener('change', function(e){
  const files = Array.from(e.target.files);
  for(const f of files){
    if(f.size > 10*1024*1024){ alert(`${f.name} çok büyük (max 10MB)`); continue; }
    const reader = new FileReader();
    reader.onload = function(){
      const b64 = reader.result.split(',')[1];
      const ext = f.name.split('.').pop().toLowerCase();
      const isImg = IMG_EXTS.has(ext);
      state.pendingFiles.push({
        name: f.name, size: f.size, type: f.type, ext,
        dataUrl: isImg ? reader.result : null,
        b64, isImg
      });
      renderPreviews();
    };
    reader.readAsDataURL(f);
  }
  e.target.value = ''; // reset so same file can be re-selected
});

// Drag & drop support
const inputBox = $('input-box');
inputBox.addEventListener('dragover', e => { e.preventDefault(); inputBox.style.borderColor = 'var(--amber)'; });
inputBox.addEventListener('dragleave', () => { inputBox.style.borderColor = ''; });
inputBox.addEventListener('drop', e => {
  e.preventDefault();
  inputBox.style.borderColor = '';
  const dt = e.dataTransfer;
  if(dt.files.length){
    $('file-picker').files = dt.files;
    $('file-picker').dispatchEvent(new Event('change'));
  }
});

function renderPreviews(){
  const strip = $('preview-strip');
  strip.innerHTML = '';
  state.pendingFiles.forEach((f, idx) => {
    const item = document.createElement('div');
    item.className = 'preview-item';
    if(f.isImg && f.dataUrl){
      item.innerHTML = `<img src="${f.dataUrl}" alt="${escHtml(f.name)}">`;
    } else {
      const icon = FILE_ICONS[f.ext] || '📄';
      item.innerHTML = `<div class="file-chip"><span class="ficon">${icon}</span><span class="fname">${escHtml(f.name)}</span></div>`;
    }
    const rm = document.createElement('button');
    rm.className = 'preview-remove';
    rm.innerHTML = '✕';
    rm.onclick = () => { state.pendingFiles.splice(idx, 1); renderPreviews(); };
    item.appendChild(rm);
    strip.appendChild(item);
  });
}

function fmtSize(bytes){
  if(bytes < 1024) return bytes + ' B';
  if(bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
  return (bytes/1024/1024).toFixed(1) + ' MB';
}

async function uploadPendingFiles(){
  const uploaded = [];
  for(const f of state.pendingFiles){
    try{
      const r = await fetch('/api/files/upload', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ uid: state.uid, filename: f.name, data: f.b64 })
      });
      const d = await r.json();
      if(d.ok) uploaded.push({...d, originalName: f.name, isImg: f.isImg, dataUrl: f.dataUrl});
      else console.warn('Upload failed:', d.error);
    }catch(e){
      console.warn('Upload error:', e.message);
    }
  }
  state.pendingFiles = [];
  renderPreviews();
  return uploaded;
}

// ─── Files Modal ──────────────────────────────────────────────
function openFilesModal(){
  $('files-modal').style.display = 'flex';
  loadFiles();
}
function closeFilesModal(){ $('files-modal').style.display = 'none'; }

async function loadFiles(){
  const grid = $('files-grid');
  grid.innerHTML = '<div style="padding:20px;text-align:center;color:var(--muted);font-size:13px;grid-column:1/-1">Yükleniyor...</div>';
  try{
    const r = await fetch(`/api/files/list?uid=${state.uid}`);
    const d = await r.json();
    if(!d.ok || !Array.isArray(d.files) || !d.files.length){
      grid.innerHTML = '<div style="padding:30px;text-align:center;color:var(--muted);font-size:13px;grid-column:1/-1">Henüz dosya yok</div>';
      return;
    }
    grid.innerHTML = '';
    for(const f of d.files){
      const card = document.createElement('div');
      card.className = 'file-card';
      const icon = FILE_ICONS[f.ext] || '📄';
      card.innerHTML = `
        ${f.is_image
          ? `<img class="file-card-thumb" src="${f.url}" alt="${escHtml(f.name)}" loading="lazy">`
          : `<div class="file-card-icon">${icon}</div>`}
        <div class="file-card-info">
          <div class="file-card-name" title="${escHtml(f.name)}">${escHtml(f.name)}</div>
          <div class="file-card-size">${fmtSize(f.size)}</div>
        </div>
        <button class="file-card-del" title="Sil" onclick="event.stopPropagation();deleteFile(${JSON.stringify(f.name)})">✕</button>
      `;
      card.onclick = () => {
        if(f.is_image) window.open(f.url, '_blank');
        else {
          const a = document.createElement('a');
          a.href = f.url; a.download = f.name; a.click();
        }
      };
      grid.appendChild(card);
    }
  }catch(e){
    grid.innerHTML = `<div style="padding:20px;text-align:center;color:#e07070;font-size:13px;grid-column:1/-1">Hata: ${escHtml(e.message)}</div>`;
  }
}

async function deleteFile(filename){
  if(!confirm(`"${filename}" dosyasını silmek istediğinize emin misiniz?`)) return;
  try{
    await fetch('/api/files/delete', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ uid: state.uid, filename })
    });
    loadFiles();
  }catch(e){ alert('Silinemedi: ' + e.message); }
}

// ─── Send ─────────────────────────────────────────────────────
function handleSend(){
  if(state.generating){ stopGeneration(); return; }
  const ta = $('msg-input');
  const text = ta.value.trim();
  if(!text && !state.pendingFiles.length) return;
  ta.value = ''; ta.style.height='auto';
  sendMessage(text);
}

function quickSend(text){ $('msg-input').value=text; handleSend(); }

async function sendMessage(text){
  hideWelcome();
  hideRateBanner();

  // Upload pending files first
  let uploadedFiles = [];
  if(state.pendingFiles.length){
    uploadedFiles = await uploadPendingFiles();
  }

  // Build display text with file attachments
  let displayText = text;
  let sendText = text;
  let imageDataList = [];  // base64 images for VL model
  if(uploadedFiles.length){
    const fileParts = uploadedFiles.map(f => {
      if(f.isImg) return `[📷 ${f.originalName}]`;
      return `[📎 ${f.originalName}]`;
    });
    const fileLabel = fileParts.join(' ');
    displayText = (text ? text + '\n' : '') + fileLabel;
    sendText = (text ? text + '\n' : '') + 'Ekteki dosyalar: ' + uploadedFiles.map(f => f.filename).join(', ');
    // Collect base64 data for images
    for(const f of uploadedFiles){
      if(f.isImg && f.dataUrl){
        imageDataList.push({
          data: f.dataUrl.split(',')[1],  // raw base64
          mime: f.dataUrl.split(';')[0].split(':')[1] || 'image/png',
          name: f.originalName
        });
      }
    }
  }

  if(!displayText) return;

  // User bubble — show file previews
  const bubble = appendBubble('user', displayText);
  if(uploadedFiles.length){
    const fileArea = document.createElement('div');
    fileArea.style.cssText = 'margin-top:8px;display:flex;flex-wrap:wrap;gap:6px';
    for(const f of uploadedFiles){
      if(f.isImg && f.dataUrl){
        const img = document.createElement('img');
        img.className = 'bubble-img';
        img.src = f.dataUrl;
        img.alt = f.originalName;
        img.onclick = () => window.open(f.url, '_blank');
        fileArea.appendChild(img);
      } else {
        const a = document.createElement('a');
        a.className = 'bubble-file-card';
        a.href = f.url;
        a.download = f.filename;
        const ext = f.filename.split('.').pop().toLowerCase();
        const icon = FILE_ICONS[ext] || '📄';
        a.innerHTML = `${icon} ${escHtml(f.filename)} <span style="color:var(--muted);font-size:10px">${fmtSize(f.size)}</span>`;
        fileArea.appendChild(a);
      }
    }
    bubble.appendChild(fileArea);
  }
  scrollToBottom();

  // UI: generating mode
  state.generating = true;
  state.abortCtrl  = new AbortController();
  const btn = $('send-btn');
  btn.className = 'send-btn stop-mode';
  btn.innerHTML = '<svg viewBox="0 0 24 24" style="fill:#fff;stroke:none"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';

  showTyping();

  let fullReply = '';
  let asstBubble = null;
  let cursor = null;

  try{
    const sendBody = { uid: state.uid, content: sendText };
    if(imageDataList.length) sendBody.images = imageDataList;

    const r = await fetch('/api/send', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      signal: state.abortCtrl.signal,
      body: JSON.stringify(sendBody)
    });

    if(r.status === 429){
      const err = await r.json();
      hideTyping();
      showRateBanner(err.error || 'Mesaj limiti aşıldı.');
      if(err.rate) { Object.assign(state.limits, { hourly_used: err.rate.hourly_used||state.limits.hourly_used, daily_used: err.rate.daily_used||state.limits.daily_used }); updateQuotaPill(); }
      return;
    }
    if(!r.ok){
      const err = await r.json().catch(()=>({error:'Sunucu hatası'}));
      hideTyping();
      appendBubble('assistant', `⚠ ${err.error||'Bir hata oluştu.'}`);
      scrollToBottom();
      return;
    }

    // Stream
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    let thinkingEl = null;
    let countdownTimer = null;
    let tokenBuffer = [];
    let isThinking = false;
    let countdownDone = false;

    // ─── Thinking bubble helpers ───
    function showThinkingBubble(){
      hideTyping();
      const chat = $('chat');
      const wrap = document.createElement('div');
      wrap.className = 'thinking-wrap';
      wrap.id = 'thinking-bubble-wrap';
      wrap.innerHTML = `
        <div class="thinking-bubble">
          <div class="thinking-icon">🧠</div>
          <div class="thinking-label">Hesap Makinesi Uzmanı düşünüyor...</div>
          <div class="thinking-countdown" id="thinking-count">8</div>
          <div class="thinking-sub">saniye</div>
          <div class="thinking-progress"><div class="thinking-progress-bar" style="--countdown-duration:8s"></div></div>
          <div id="thinking-result-slot"></div>
        </div>
      `;
      chat.appendChild(wrap);
      scrollToBottom();
      thinkingEl = wrap;
      isThinking = true;
      countdownDone = false;

      // Geri sayım
      let count = 8;
      countdownTimer = setInterval(()=>{
        count--;
        const el = $('thinking-count');
        if(el) el.textContent = Math.max(0, count);
        if(count <= 0){
          clearInterval(countdownTimer);
          countdownTimer = null;
          countdownDone = true;
          flushTokenBuffer();
        }
      }, 1000);
    }

    function showCalcResult(expr, result){
      const slot = $('thinking-result-slot');
      if(slot){
        slot.innerHTML = `
          <div class="thinking-result">
            <div class="expr">${expr}</div>
            <div class="result">= ${result}</div>
          </div>
        `;
        const label = thinkingEl?.querySelector('.thinking-label');
        if(label) label.textContent = 'Sonuç bulundu! Cevap hazırlanıyor...';
      }
      scrollToBottom();
    }

    function hideThinkingBubble(){
      if(countdownTimer){ clearInterval(countdownTimer); countdownTimer = null; }
      const wrap = $('thinking-bubble-wrap');
      if(wrap){
        wrap.style.transition = 'opacity .3s, transform .3s';
        wrap.style.opacity = '0';
        wrap.style.transform = 'translateY(-10px)';
        setTimeout(()=> wrap.remove(), 300);
      }
      isThinking = false;
    }

    function flushTokenBuffer(){
      if(tokenBuffer.length === 0) return;
      hideThinkingBubble();
      if(!asstBubble){
        asstBubble = appendBubble('assistant', '', null);
        cursor = document.createElement('span');
        cursor.className = 'cursor';
        asstBubble.appendChild(cursor);
      }
      fullReply = tokenBuffer.join('');
      renderBubbleContent(asstBubble, fullReply);
      asstBubble.appendChild(cursor);
      scrollToBottom();
      tokenBuffer = [];
    }

    // ─── Parse SSE stream ───
    let currentEvent = null;

    while(true){
      const { done, value } = await reader.read();
      if(done) break;
      buf += dec.decode(value, {stream:true});
      const lines = buf.split('\n'); buf = lines.pop();
      for(const line of lines){
        // SSE event type
        if(line.startsWith('event: ')){
          currentEvent = line.slice(7).trim();
          continue;
        }

        if(!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if(raw === '[DONE]'){ currentEvent = null; continue; }

        try{
          const j = JSON.parse(raw);

          // ── Handle named events ──
          if(currentEvent === 'thinking'){
            if(j.status === 'start'){
              showThinkingBubble();
            } else if(j.status === 'done'){
              countdownDone = true;
              if(countdownTimer){ clearInterval(countdownTimer); countdownTimer = null; }
              flushTokenBuffer();
            } else if(j.status === 'skip'){
              // Math yoktu — normal akışa geç
              hideThinkingBubble();
              if(!asstBubble){
                asstBubble = appendBubble('assistant', '', null);
                cursor = document.createElement('span');
                cursor.className = 'cursor';
                asstBubble.appendChild(cursor);
              }
            }
            currentEvent = null;
            continue;
          }

          if(currentEvent === 'calc_result'){
            showCalcResult(j.expr || '', j.result || '');
            currentEvent = null;
            continue;
          }

          // ── Normal data event (tokens) ──
          currentEvent = null;
          if(j.error){
            console.error('LLM error:', j.error);
            fullReply += `\n\n⚠ Hata: ${j.error}`;
            if(!asstBubble){
              hideThinkingBubble();
              asstBubble = appendBubble('assistant', '', null);
            }
            renderBubbleContent(asstBubble, fullReply);
            continue;
          }
          const tok = j.choices?.[0]?.delta?.content || '';
          if(!tok) continue;

          if(isThinking && !countdownDone){
            // Buffer tokens while counting down
            tokenBuffer.push(tok);
          } else {
            // Direct render
            if(isThinking){
              hideThinkingBubble();
            }
            if(!asstBubble){
              hideTyping();
              asstBubble = appendBubble('assistant', '', null);
              cursor = document.createElement('span');
              cursor.className = 'cursor';
              asstBubble.appendChild(cursor);
            }
            fullReply += tok;
            renderBubbleContent(asstBubble, fullReply);
            asstBubble.appendChild(cursor);
          }

        }catch{}
      }
      scrollToBottom();
    }

    // Flush remaining buffered tokens
    if(tokenBuffer.length > 0) flushTokenBuffer();

    // Update quota after success
    state.limits.hourly_used = (state.limits.hourly_used||0) + 1;
    state.limits.daily_used  = (state.limits.daily_used||0)  + 1;
    updateQuotaPill();

  } catch(e){
    hideTyping();
    if(e.name === 'AbortError'){
      if(asstBubble && fullReply){
        renderBubbleContent(asstBubble, fullReply + ' ');
        const stopped = document.createElement('span');
        stopped.style.cssText='font-size:11px;color:var(--muted);font-family:var(--mono)';
        stopped.textContent=' [durduruldu]';
        asstBubble.appendChild(stopped);
      } else if(asstBubble){
        asstBubble.textContent = '[Durduruldu]';
        asstBubble.style.opacity = '.5';
      }
    } else {
      if(asstBubble) asstBubble.innerHTML = `<span style="color:#e07070">⚠ Bağlantı hatası: ${escHtml(e.message)}</span>`;
      $('offline-banner').style.display='block';
      setTimeout(()=>$('offline-banner').style.display='none', 5000);
    }
  } finally {
    if(cursor) cursor.remove();
    state.generating = false;
    state.abortCtrl  = null;
    btn.className = 'send-btn';
    btn.innerHTML = '<svg viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>';
    scrollToBottom();
  }
}

function stopGeneration(){
  state.abortCtrl?.abort();
}

// ─── Textarea auto-grow ───────────────────────────────────────
$('msg-input').addEventListener('input', function(){
  this.style.height='auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});
$('msg-input').addEventListener('keydown', e=>{
  if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); handleSend(); }
  if(e.key==='Escape' && state.generating) stopGeneration();
});

// ─── Settings / Experts logic ──────────────────────────────────
let currentExperts = {};

async function fetchSettings(){
  try{
    const r = await fetch('/api/session', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ uid: state.uid, username: state.username })
    });
    const d = await r.json();
    if(d.ok && d.experts){
      currentExperts = JSON.parse(d.experts);
      if(currentExperts.calculator) $('toggle-calculator').classList.add('on');
      else $('toggle-calculator').classList.remove('on');
      
      if(currentExperts.sandbox) $('toggle-sandbox').classList.add('on');
      else $('toggle-sandbox').classList.remove('on');
    }
  } catch(e){}
}

function openSettingsModal(){
  $('settings-modal').style.display='flex';
  fetchSettings();
}
function closeSettingsModal(){ $('settings-modal').style.display='none'; }

async function toggleExpert(name){
  const el = $(`toggle-${name}`);
  const isOn = el.classList.toggle('on');
  currentExperts[name] = isOn;
  
  try {
    await fetch('/api/settings/experts', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ uid: state.uid, experts: JSON.stringify(currentExperts) })
    });
  } catch(e){
    console.warn("Couldn't save experts setting", e);
  }
}

// ─── Sandbox logic ──────────────────────────────────────────────
function openSandboxModal(){
  $('sandbox-modal').style.display='flex';
  loadSandbox();
}
function closeSandboxModal(){ $('sandbox-modal').style.display='none'; }

async function loadSandbox(){
  const list = $('sandbox-list');
  list.innerHTML = '<div style="padding:15px;text-align:center;color:var(--muted);font-size:13px">Yükleniyor...</div>';
  try{
    const r = await fetch(`/api/sandbox/list?uid=${state.uid}`);
    const d = await r.json();
    if(d.ok && Array.isArray(d.files)){
      if(d.files.length === 0){
         list.innerHTML = '<div style="padding:15px;text-align:center;color:var(--muted);font-size:13px">Henüz dosya yok.</div>';
         return;
      }
      list.innerHTML = '';
      d.files.forEach(f => {
         const row = document.createElement('div');
         row.className = 'sb-file';
         row.innerHTML = `
           <span class="sb-file-name">${escHtml(f.name)} <span style="color:var(--faint)">(${fmtSize(f.size)})</span></span>
           <div class="sb-actions">
             <button class="sb-btn" onclick="window.open('/api/sandbox/download/'+encodeURIComponent(${JSON.stringify(state.uid)})+'/'+encodeURIComponent(${JSON.stringify(f.name)}))">İndir</button>
             <button class="sb-btn del" onclick="deleteSandboxFile(${JSON.stringify(f.name)})">Sil</button>
           </div>
         `;
         list.appendChild(row);
      });
    }
  }catch(e){
    list.innerHTML = `<div style="padding:15px;text-align:center;color:#e74c3c;font-size:13px">Hata: ${escHtml(e.message)}</div>`;
  }
}

async function deleteSandboxFile(filename){
  if(!confirm(`"${filename}" dosyasını silmek istediğinize emin misiniz?`)) return;
  try{
    await fetch(`/api/sandbox/delete`, {
      method: 'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ uid: state.uid, filename })
    });
    loadSandbox();
  }catch(e){
    alert('Silinemedi: ' + e.message);
  }
}

async function saveToSandbox(code){
  const filename = prompt("Dosya adı (örn: script.py):", "code.py");
  if(!filename) return;
  try{
    const r = await fetch('/api/sandbox/save', {
      method: 'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ uid: state.uid, filename, content: code })
    });
    const d = await r.json();
    if(d.ok) alert("Dosya Sandbox'a kaydedildi: " + filename);
    else alert("Hata: " + d.error);
  }catch(e){
    alert("Kaydedilemedi: " + e.message);
  }
}

// ─── Boot ─────────────────────────────────────────────────────
$('app-title').textContent = '{{ title }}';
initSession();
</script>
</body>
</html>
"""

# ─── Flask Routes ────────────────────────────────────────────

def _get_uid() -> str:
    """Request'ten UID al (cookie veya JSON body)."""
    return request.cookies.get(COOKIE_NAME, '')


def _main(path: str, method: str = 'GET', **kwargs) -> http_req.Response:
    """Ana sunucuya HTTP isteği gönder."""
    url = f"{MAIN_SERVER}{path}"
    return http_req.request(method=method, url=url,
                           timeout=kwargs.pop('timeout', 10), **kwargs)


@app.route('/')
def index():
    """Chat arayüzünü sun. Cookie yoksa yeni UID ata."""
    uid = request.cookies.get(COOKIE_NAME)
    resp = make_response(render_template_string(HTML, title=APP_TITLE))
    if not uid:
        new_uid = str(uuid.uuid4())
        resp.set_cookie(COOKIE_NAME, new_uid, max_age=COOKIE_TTL, samesite='Lax')
    return resp


@app.route('/api/session', methods=['POST'])
def session_route():
    """
    Kullanıcıyı ana sunucuya kaydet; limit bilgisini döndür.
    chat_client.py frontend'i tarafından init sırasında çağrılır.
    """
    d   = request.json or {}
    uid = d.get('uid') or request.cookies.get(COOKIE_NAME, '')
    if not uid:
        return jsonify({"ok": False, "error": "UID bulunamadı"}), 400

    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '')
    ip = ip.split(',')[0].strip()

    try:
        # Register / update on main server
        reg_r = _main('/api/webchat/register', method='POST', json={
            'uid': uid,
            'username': d.get('username', 'Anonim'),
        }, headers={'X-Forwarded-For': ip})
        reg   = reg_r.json()

        # Get full limits
        lim_r = _main(f'/api/webchat/limits/{uid}', method='GET')
        lim   = lim_r.json()

        # Get user data (includes experts)
        user_data = reg.get("user", {})
        experts = user_data.get("experts", "{}")

        return jsonify({
            "ok": True,
            "user":   user_data,
            "experts": experts,
            "limits": {
                "hourly_used":  lim.get("hourly_used",  0),
                "hourly_limit": lim.get("rate_limit_hour", 20),
                "daily_used":   lim.get("daily_used",   0),
                "daily_limit":  lim.get("daily_limit",  100),
                "max_tokens":   lim.get("max_tokens",  2048),
            }
        })
    except Exception as e:
        log.warning(f"Session register error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route('/api/username', methods=['POST'])
def username_route():
    """Kullanıcı adını güncelle."""
    d   = request.json or {}
    uid = d.get('uid') or request.cookies.get(COOKIE_NAME, '')
    username = d.get('username', '').strip() or 'Anonim'
    if not uid:
        return jsonify({"ok": False, "error": "UID bulunamadı"}), 400
    try:
        _main(f'/api/webchat/users/{uid}', method='POST', json={'username': username})
    except Exception as e:
        log.warning(f"Username update error: {e}")
    return jsonify({"ok": True})


@app.route('/api/history')
def history_route():
    """Sohbet geçmişini ana sunucudan çek."""
    uid   = request.args.get('uid') or request.cookies.get(COOKIE_NAME, '')
    limit = int(request.args.get('limit', 20))
    if not uid:
        return jsonify({"ok": False, "messages": []})
    try:
        r = _main(f'/api/webchat/history/{uid}', method='GET',
                  params={'limit': limit})
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "messages": [], "error": str(e)})


@app.route('/api/history/clear', methods=['POST'])
def history_clear_route():
    """Kullanıcının sohbet geçmişini temizle."""
    d   = request.json or {}
    uid = d.get('uid') or request.cookies.get(COOKIE_NAME, '')
    if not uid:
        return jsonify({"ok": False, "error": "UID bulunamadı"}), 400
    try:
        chat_id = f"web:{uid}"
        r = _main(f'/api/db/chat/{chat_id}/clear', method='DELETE')
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route('/api/send', methods=['POST'])
def send_route():
    """
    Mesajı ana sunucuya ilet.
    Ana sunucu rate-check + LLM + kayıt işlemlerini yapar.
    SSE stream'i doğrudan geçirir.
    """
    d       = request.json or {}
    uid     = d.get('uid') or request.cookies.get(COOKIE_NAME, '')
    content = (d.get('content') or '').strip()

    if not uid:
        return jsonify({"ok": False, "error": "Oturum bulunamadı"}), 401
    if not content:
        return jsonify({"ok": False, "error": "Mesaj boş olamaz"}), 400

    # ── Rate-limit kontrolü ÖNCE (SSE stream'i açmadan) ───────
    try:
        lim_resp = http_req.get(f"{MAIN_SERVER}/api/webchat/limits/{uid}", timeout=5)
        lim = lim_resp.json()
        if not lim.get("enabled", True):
            return jsonify({"ok": False, "error": "Hesabınız devre dışı bırakıldı."}), 403
    except Exception:
        pass  # ana sunucu kendi kontrolünü yapacak

    # ── SSE stream bağlantısı ─────────────────────────────────
    try:
        send_data = {'uid': uid, 'content': content}
        images = d.get('images')
        if images:
            send_data['images'] = images

        resp = http_req.post(
            f"{MAIN_SERVER}/api/webchat/chat",
            json=send_data,
            stream=True, timeout=180
        )
        if not resp.ok:
            try:
                err_body = resp.json()
            except Exception:
                err_body = {"error": f"Sunucu hatası ({resp.status_code})"}
            return jsonify(err_body), resp.status_code

        def stream_gen():
            got_data = False
            try:
                for chunk in resp.iter_content(chunk_size=None):
                    if chunk:
                        got_data = True
                        yield chunk.decode('utf-8', errors='replace')
            except http_req.exceptions.ConnectionError:
                yield f'data: {{"error":"Ana sunucuya bağlanılamadı. Uygulama çalışıyor mu?"}}\n\n'
                return
            except http_req.exceptions.Timeout:
                yield f'data: {{"error":"Ana sunucu yanıt süresi aşıldı."}}\n\n'
                return
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"
                return
            if not got_data:
                yield f'data: {{"error":"Sunucudan yanıt alınamadı. Model çalışıyor mu?"}}\n\n'

    except http_req.exceptions.ConnectionError:
        return jsonify({"ok": False, "error": f"Ana sunucuya bağlanılamadı ({MAIN_SERVER}). Çalışıyor mu?"}), 502
    except http_req.exceptions.Timeout:
        return jsonify({"ok": False, "error": "Ana sunucu yanıt vermiyor (timeout)."}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": f"Sunucuya bağlanılamadı: {e}"}), 500

    return Response(
        stream_with_context(stream_gen()),
        content_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ─── Settings Proxy ──────────────────────────────────────────

@app.route('/api/settings/experts', methods=['POST'])
def update_experts():
    data = request.json or {}
    uid = data.get('uid') or _get_uid()
    experts_json = data.get('experts', '{}')
    if not uid:
        return jsonify({"ok": False, "error": "auth"}), 401
    try:
        r = _main(f"/api/webchat/users/{uid}", method="PUT", json={"experts": experts_json})
        return Response(r.content, status=r.status_code, headers=dict(r.headers))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ─── Sandbox Proxy ───────────────────────────────────────────

@app.route('/api/sandbox/list')
def sandbox_list():
    uid = request.args.get('uid') or _get_uid()
    if not uid: return jsonify({"ok": False}), 401
    try:
        r = _main(f"/api/webchat/sandbox/list/{uid}")
        return Response(r.content, status=r.status_code, headers=dict(r.headers))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route('/api/sandbox/delete', methods=['POST'])
def sandbox_delete():
    data = request.json or {}
    uid = data.get('uid') or _get_uid()
    filename = data.get('filename')
    if not uid or not filename: return jsonify({"ok": False}), 400
    # Sanitize to prevent path traversal via proxy URL
    safe_uid = werkzeug.utils.secure_filename(uid)
    safe_name = werkzeug.utils.secure_filename(filename)
    if not safe_uid or not safe_name:
        return jsonify({"ok": False, "error": "Geçersiz dosya adı"}), 400
    try:
        r = _main(f"/api/webchat/sandbox/{safe_uid}/{safe_name}", method="DELETE")
        return Response(r.content, status=r.status_code, headers=dict(r.headers))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route('/api/sandbox/save', methods=['POST'])
def sandbox_save():
    data = request.json or {}
    data['uid'] = data.get('uid') or _get_uid()
    try:
        r = _main("/api/webchat/sandbox/save", method="POST", json=data)
        return Response(r.content, status=r.status_code, headers=dict(r.headers))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route('/api/sandbox/download/<uid>/<filename>')
def sandbox_download(uid, filename):
    # Sanitize to prevent path traversal via proxy URL
    safe_uid = werkzeug.utils.secure_filename(uid)
    safe_name = werkzeug.utils.secure_filename(filename)
    if not safe_uid or not safe_name:
        return jsonify({"ok": False, "error": "Geçersiz dosya adı"}), 400
    try:
        r = _main(f"/api/webchat/sandbox/download/{safe_uid}/{safe_name}", stream=True)
        headers = {'Content-Type': r.headers.get('Content-Type', 'application/octet-stream')}
        if 'Content-Disposition' in r.headers:
            headers['Content-Disposition'] = r.headers['Content-Disposition']
        return Response(r.iter_content(chunk_size=1024), headers=headers)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ─── Files Proxy ─────────────────────────────────────────────

@app.route('/api/files/upload', methods=['POST'])
def files_upload_proxy():
    data = request.json or {}
    data['uid'] = data.get('uid') or _get_uid()
    try:
        r = _main("/api/webchat/files/upload", method="POST", json=data, timeout=30)
        return Response(r.content, status=r.status_code,
                        content_type='application/json')
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route('/api/files/list')
def files_list_proxy():
    uid = request.args.get('uid') or _get_uid()
    if not uid: return jsonify({"ok": False}), 401
    try:
        r = _main(f"/api/webchat/files/list/{uid}")
        return Response(r.content, status=r.status_code,
                        content_type='application/json')
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route('/api/files/serve/<uid>/<filename>')
def files_serve_proxy(uid, filename):
    # Sanitize to prevent path traversal via proxy URL
    safe_uid = werkzeug.utils.secure_filename(uid)
    safe_name = werkzeug.utils.secure_filename(filename)
    if not safe_uid or not safe_name:
        return jsonify({"ok": False, "error": "Geçersiz dosya adı"}), 400
    try:
        r = _main(f"/api/webchat/files/{safe_uid}/{safe_name}", stream=True)
        headers = {'Content-Type': r.headers.get('Content-Type', 'application/octet-stream')}
        if 'Content-Disposition' in r.headers:
            headers['Content-Disposition'] = r.headers['Content-Disposition']
        return Response(r.iter_content(chunk_size=4096), headers=headers)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route('/api/files/delete', methods=['POST'])
def files_delete_proxy():
    data = request.json or {}
    uid = data.get('uid') or _get_uid()
    filename = data.get('filename')
    if not uid or not filename: return jsonify({"ok": False}), 400
    # Sanitize to prevent path traversal via proxy URL
    safe_uid = werkzeug.utils.secure_filename(uid)
    safe_name = werkzeug.utils.secure_filename(filename)
    if not safe_uid or not safe_name:
        return jsonify({"ok": False, "error": "Geçersiz dosya adı"}), 400
    try:
        r = _main(f"/api/webchat/files/{safe_uid}/{safe_name}", method="DELETE")
        return Response(r.content, status=r.status_code,
                        content_type='application/json')
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ─── Sağlık kontrolü ─────────────────────────────────────────

@app.route('/health')
def health():
    try:
        r = http_req.get(f"{MAIN_SERVER}/api/server/status", timeout=3)
        llm_running = r.json().get("running", False)
    except Exception:
        llm_running = False
    return jsonify({"ok": True, "llm_running": llm_running,
                    "main_server": MAIN_SERVER})


# ─── Main ────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="Web Chat Client")
    parser.add_argument('--port',     type=int, default=CHAT_PORT)
    parser.add_argument('--host',     type=str, default=CHAT_HOST)
    parser.add_argument('--server',   type=str, default=MAIN_SERVER,
                        help='Ana sunucu adresi (örn. http://192.168.1.5:5050)')
    parser.add_argument('--title',    type=str, default=APP_TITLE)
    parser.add_argument('--gunicorn', action='store_true',
                        help='Gunicorn ile başlat (production modu)')
    args = parser.parse_args()

    MAIN_SERVER = args.server   # noqa: F811 — override from CLI
    APP_TITLE   = args.title

    if args.gunicorn:
        print("╔══════════════════════════════════════════════════════╗")
        print("║   💬 Web Chat Client (Gunicorn)                      ║")
        print(f"║   → http://localhost:{args.port:<28}║")
        print(f"║   → Ana Sunucu: {args.server:<37}║")
        print("╚══════════════════════════════════════════════════════╝")
        conf_path = pathlib.Path(__file__).parent / 'gunicorn.conf.py'
        os.execvp('gunicorn', [
            'gunicorn', 'chat_client:app',
            '-c', str(conf_path),
            '--bind', f'{args.host}:{args.port}',
        ])
    else:
        print("╔══════════════════════════════════════════════════════╗")
        print("║   💬 Web Chat Client                                 ║")
        print(f"║   → http://localhost:{args.port:<28}║")
        print(f"║   → Ana Sunucu: {args.server:<37}║")
        print("║   💡 Production için: python chat_client.py --gunicorn║")
        print("╚══════════════════════════════════════════════════════╝")
        app.run(host=args.host, port=args.port, debug=False, threaded=True)


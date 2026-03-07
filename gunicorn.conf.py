# -*- coding: utf-8 -*-
"""
Gunicorn Yapılandırması — EfeMultiAIbot

Kullanım:
    gunicorn app:app -c gunicorn.conf.py --bind 0.0.0.0:5050
    gunicorn chat_client:app -c gunicorn.conf.py --bind 0.0.0.0:5051
"""

import multiprocessing
import os

# ── Worker ───────────────────────────────────────────────────
# gthread: SSE streaming + SQLite thread-local bağlantıları için ideal.
# ÖNEMLİ: 1 worker! Uygulama module-level singleton'lar kullanıyor
# (MemoryManager, BotMonitor, subprocess yönetimi). Birden fazla
# worker fork'u bu paylaşılan durumu bozar.
worker_class = "gthread"
workers = 1
threads = int(os.environ.get("GUNICORN_THREADS", 8))

# ── Timeout ──────────────────────────────────────────────────
# LLM yanıtları 2+ dakika sürebilir. SSE stream'leri bu süre
# boyunca açık kalmalı.
timeout = 300           # worker timeout (saniye)
graceful_timeout = 30   # graceful shutdown bekleme süresi
keepalive = 5           # keep-alive bağlantı süresi

# ── Bind ─────────────────────────────────────────────────────
# CLI'dan --bind ile override edilebilir.
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:5050")

# ── Logging ──────────────────────────────────────────────────
accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "-")  # stdout
errorlog  = os.environ.get("GUNICORN_ERROR_LOG", "-")   # stderr
loglevel  = os.environ.get("GUNICORN_LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" %(D)sμs'

# ── Diğer ────────────────────────────────────────────────────
preload_app = True      # Tek worker, preload avantajlı: daha hızlı başlatma, daha az bellek
forwarded_allow_ips = "*"  # Nginx arkasındayken X-Forwarded-For'a güven
proxy_allow_from = "*"

# ── Lifecycle Hooks ──────────────────────────────────────────

def on_starting(server):
    """Gunicorn başlamadan önce."""
    server.log.info("🤖 EfeMultiAIbot — Gunicorn başlatılıyor…")


def post_fork(server, worker):
    """Her worker fork'undan sonra."""
    server.log.info(f"Worker {worker.pid} hazır")


def worker_exit(server, worker):
    """Worker kapanırken temizlik."""
    server.log.info(f"Worker {worker.pid} kapanıyor")

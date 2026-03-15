#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════╗
║      EfeMultiAIbot — LLaMA Panel + WhatsApp Bot                      ║
║                                                                      ║
║  Yenilikler:                                                         ║
║  • 3 katmanlı bellek: L1 LRU-RAM → L2 sıkıştırılmış SQLite → L3 arşiv
║  • RAG: ChromaDB vektör bellek — geçmiş konuşmaları semantik arama  ║
║  • Adaptif sıkıştırma: zlib lvl1/6/9 + delta encoding               ║
║  • LLM özetleme: eski bağlamı sil değil, özetle → token tasarrufu   ║
║  • Bağlantı havuzu: thread-local SQLite + WAL + mmap                ║
║  • Token bütçesi: context window'u aşmamak için otomatik budama     ║
║  • Görsel tekil depolama: SHA-256 + thumbnail önbelleği             ║
║  • Bot süreç monitörü: otomatik yeniden başlatma + sağlık kontrolü  ║
║  • Gerçek zamanlı SSE metrikleri: canlı dashboard                   ║
║  • Yazma tamponlama: sık yazmaları toplu commit                     ║
║  • Arşivleme: 30+ günlük mesajlar L3'e taşınır, hâlâ erişilebilir  ║
╚══════════════════════════════════════════════════════════════════════╝

Kullanım:
    python app.py                   → Panel (5050) + WhatsApp bot
    python app.py --panel-only      → Sadece web panel
    python app.py --bot-only        → Sadece WhatsApp bot
    python app.py --setup           → npm bağımlılıklarını yükle
    python app.py --create-project  → Tauri P2P iskelet oluştur
    python app.py --stats           → Anlık istatistik
    python app.py --maintenance     → Manuel bakım çalıştır

Gereksinimler:
    pip install flask requests
    npm install whatsapp-web.js qrcode-terminal axios googlethis sqlite3
"""

# ═══════════════════════════════════════════════════════════════
#  IMPORTS
# ═══════════════════════════════════════════════════════════════
import argparse
import ast
import atexit
import base64 as b64mod
import glob
import hashlib
import io
import json
import logging
import math as _math_mod
import operator
import os
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import uuid as uuid_mod
import zlib
from collections import OrderedDict, defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from flask import Flask, Response, jsonify, request, send_file, send_from_directory, stream_with_context
import requests as http_req
import werkzeug.utils

# Maksimum mesaj uzunluğu (DoS koruması)
MAX_MESSAGE_LENGTH = 50_000  # karakter
MAX_CHAT_ID_LENGTH = 256

# ═══════════════════════════════════════════════════════════════
#  SAFE MATH EVALUATOR (AST-based, replaces eval())
# ═══════════════════════════════════════════════════════════════
_SAFE_MATH_NAMES: Dict[str, Any] = {
    k: v for k, v in _math_mod.__dict__.items() if not k.startswith("__")
}
_SAFE_MATH_NAMES.update({
    "abs": abs, "round": round, "min": min, "max": max,
    "sum": sum, "int": int, "float": float,
})

_AST_ALLOWED_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}

def _safe_calc_eval(expr_str: str):
    """AST-based allowlist evaluator — permits only numeric literals,
    whitelisted math functions/constants, and arithmetic operators."""
    tree = ast.parse(expr_str.strip(), mode="eval")

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float, complex)):
                return node.value
            raise ValueError(f"İzin verilmeyen sabit: {node.value!r}")
        if isinstance(node, ast.BinOp):
            op_fn = _AST_ALLOWED_OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"İzin verilmeyen operatör: {type(node.op).__name__}")
            return op_fn(_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            op_fn = _AST_ALLOWED_OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"İzin verilmeyen operatör: {type(node.op).__name__}")
            return op_fn(_eval(node.operand))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("Yalnızca basit fonksiyon çağrılarına izin verilir")
            fn = _SAFE_MATH_NAMES.get(node.func.id)
            if fn is None or not callable(fn):
                raise ValueError(f"İzin verilmeyen fonksiyon: {node.func.id}")
            args = [_eval(a) for a in node.args]
            return fn(*args)
        if isinstance(node, ast.Name):
            val = _SAFE_MATH_NAMES.get(node.id)
            if val is None:
                raise ValueError(f"İzin verilmeyen isim: {node.id}")
            return val
        if isinstance(node, ast.Tuple):
            return tuple(_eval(e) for e in node.elts)
        if isinstance(node, ast.List):
            return [_eval(e) for e in node.elts]
        raise ValueError(f"İzin verilmeyen AST düğümü: {type(node).__name__}")

    return _eval(tree)

# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("app")

# ═══════════════════════════════════════════════════════════════
#  SABITLER
# ═══════════════════════════════════════════════════════════════
APP_DIR               = Path(__file__).parent.resolve()
DB_PATH               = str(APP_DIR / "chat_history.db")
SANDBOX_DIR           = APP_DIR / "Sandbox"
UPLOADS_DIR           = APP_DIR / "uploads"

# Dosya yükleme limitleri
MAX_UPLOAD_SIZE       = 10 * 1024 * 1024   # 10 MB
ALLOWED_EXTENSIONS    = {
    # Görseller
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp',
    # Belgeler
    'pdf', 'txt', 'md', 'csv', 'rtf', 'doc', 'docx',
    # Kod
    'py', 'js', 'ts', 'html', 'css', 'json', 'xml', 'yaml', 'yml',
    'c', 'cpp', 'h', 'java', 'rs', 'go', 'sh', 'sql',
}
IMAGE_EXTENSIONS      = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg'}

# Sıkıştırma
COMPRESS_THRESHOLD    = 128        # bayt — daha kısası ham saklanır
COMPRESS_LEVELS       = (1, 6, 9)  # hızlı / dengeli / maksimum
FLAG_RAW              = b'\x00'
FLAG_ZLIB             = b'\x01'
FLAG_ZLIB_MAX         = b'\x02'    # seviye-9 arşiv

# Bellek katmanları
L1_CACHE_SIZE         = 1024       # RAM'de max farklı anahtar
L1_CACHE_TTL          = 300        # saniye
L2_MAX_MSGS_PER_CHAT  = 250        # aktif tablo limiti
L3_ARCHIVE_THRESHOLD  = 30         # gün — bu kadar eskisi L3'e gider

# Token tahmini (yaklaşık, model bağımsız)
AVG_CHARS_PER_TOKEN   = 4          # Türkçe için muhafazakâr tahmin
DEFAULT_CTX_WINDOW    = 32768      # token — model context boyutu
SUMMARY_RESERVE_RATIO = 0.15       # context'in %15'ini özetlere ayır

# Yazma tamponu
WRITE_BUFFER_SIZE     = 50         # bu kadar yazma birikince toplu commit
WRITE_BUFFER_TIMEOUT  = 2.0        # veya bu kadar saniye geçince commit

# Bot izleme
BOT_RESTART_DELAY     = 5          # saniye
BOT_MAX_RESTARTS      = 10
BOT_HEALTH_INTERVAL   = 15         # saniye

# Güvenlik & yapılandırma (ortam değişkenlerinden)
PANEL_API_KEY         = os.environ.get("PANEL_API_KEY", "")   # boş → auth yok
WHATSAPP_GROUPS       = [
    g.strip()
    for g in os.environ.get("WHATSAPP_GROUPS", "MyGroup1,MyGroup2").split(",")
    if g.strip()
]

# ─── RAG (Retrieval-Augmented Generation) ────────────────────
RAG_DIR               = str(APP_DIR / "rag_store")    # ChromaDB kalıcı dizini
RAG_COLLECTION        = "messages"
RAG_TOP_K             = 5          # sorgu başına döndürülecek en benzer parça
RAG_MIN_SCORE         = 0.25       # 1-distance; bunun altı atlanır
RAG_CHUNK_SIZE        = 480        # karakter — uzun mesajları bu boyutta parçala
RAG_CHUNK_OVERLAP     = 80         # parçalar arası örtüşme
RAG_INDEX_BATCH       = 256        # toplu indeksleme boyutu
RAG_ENABLED           = os.environ.get("RAG_ENABLED", "1") == "1"

# ═══════════════════════════════════════════════════════════════
#  KATMAN 1 — LRU RAM Cache (thread-safe, TTL, metriklere sahip)
# ═══════════════════════════════════════════════════════════════
class LRUCache:
    """
    Çift-uçlu bağlı liste (OrderedDict) tabanlı LRU.
    TTL aşılan girişler lazy silinir; periyodik sweep de çalışır.
    """

    __slots__ = (
        "_data", "_lock", "_maxsize", "_ttl",
        "hits", "misses", "evictions", "expirations"
    )

    def __init__(self, maxsize: int = 1024, ttl: int = 300):
        self._data: OrderedDict[str, Tuple[Any, float]] = OrderedDict()
        self._lock  = threading.RLock()
        self._maxsize = maxsize
        self._ttl     = ttl
        self.hits = self.misses = self.evictions = self.expirations = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            val, ts = entry
            if time.monotonic() - ts > self._ttl:
                del self._data[key]
                self.expirations += 1
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return val

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (value, time.monotonic())
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)
                self.evictions += 1

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                del self._data[k]
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def sweep(self) -> int:
        """Bayat girişleri temizle."""
        with self._lock:
            now = time.monotonic()
            stale = [k for k, (_, ts) in self._data.items() if now - ts > self._ttl]
            for k in stale:
                del self._data[k]
            self.expirations += len(stale)
            return len(stale)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "size": len(self._data),
                "maxsize": self._maxsize,
                "ttl_s": self._ttl,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "expirations": self.expirations,
                "hit_rate": round(self.hits / total * 100, 1) if total else 0.0,
            }


# ═══════════════════════════════════════════════════════════════
#  KATMAN 2 — Adaptif Sıkıştırma
# ═══════════════════════════════════════════════════════════════

def _encode(text: str, archive: bool = False) -> bytes:
    """
    Metni encode et. archive=True → en yüksek sıkıştırma (L3).
    Sıkıştırılmış büyükse ham sakla.
    """
    raw = text.encode("utf-8")
    if len(raw) < COMPRESS_THRESHOLD:
        return FLAG_RAW + raw

    if archive:
        comp = zlib.compress(raw, level=9)
        if len(comp) < len(raw):
            return FLAG_ZLIB_MAX + comp
        return FLAG_RAW + raw

    # Hızlı sıkıştırma (L2 için seviye-6)
    comp = zlib.compress(raw, level=6)
    if len(comp) < len(raw):
        return FLAG_ZLIB + comp
    return FLAG_RAW + raw


def _decode(data: bytes) -> str:
    """encode() ile saklanmış bytes → metin."""
    if not data:
        return ""
    try:
        flag, payload = data[:1], data[1:]
        if flag in (FLAG_ZLIB, FLAG_ZLIB_MAX):
            return zlib.decompress(payload).decode("utf-8")
        return payload.decode("utf-8")
    except Exception as e:
        log.warning(f"_decode hatası (len={len(data)}): {e}")
        return data[1:].decode("utf-8", errors="replace")


def _compress_ratio(data: bytes) -> float:
    """Sıkıştırma oranı (düşük = daha iyi)."""
    if len(data) < COMPRESS_THRESHOLD:
        return 1.0
    comp = zlib.compress(data, level=6)
    return len(comp) / len(data)


# ═══════════════════════════════════════════════════════════════
#  KATMAN 3 — Yazma Tamponu
# ═══════════════════════════════════════════════════════════════

class WriteBuffer:
    """
    Sık mesaj yazmalarını biriktirip toplu INSERT ile commit eder.
    Hem gecikmeyi hem de SQLite kilit çekişmesini azaltır.
    """

    def __init__(self, flush_size: int = WRITE_BUFFER_SIZE,
                 flush_timeout: float = WRITE_BUFFER_TIMEOUT):
        self._buf: List[Tuple] = []
        self._lock = threading.Lock()
        self._flush_size = flush_size
        self._flush_timeout = flush_timeout
        self._last_flush = time.monotonic()
        self._flush_cb = None   # set by MemoryManager

    def add(self, row: Tuple) -> bool:
        """Satır ekle. True → hemen flush gerekiyor."""
        with self._lock:
            self._buf.append(row)
            should_flush = (
                len(self._buf) >= self._flush_size or
                time.monotonic() - self._last_flush >= self._flush_timeout
            )
            return should_flush

    def drain(self) -> List[Tuple]:
        """Tamponu boşalt ve içeriği döndür."""
        with self._lock:
            rows = self._buf[:]
            self._buf.clear()
            self._last_flush = time.monotonic()
            return rows

    def size(self) -> int:
        with self._lock:
            return len(self._buf)


# ═══════════════════════════════════════════════════════════════
#  TOKEN BÜTÇE HESAPLAYICI
# ═══════════════════════════════════════════════════════════════

def estimate_tokens(text: str) -> int:
    """Yaklaşık token sayısı (chars / 4, Türkçe için muhafazakâr)."""
    return max(1, len(text) // AVG_CHARS_PER_TOKEN)


def fit_messages_to_budget(
    messages: List[Dict[str, str]],
    budget: int,
    keep_last: int = 2,
) -> List[Dict[str, str]]:
    """
    Mesaj listesini token bütçesine sığdır.
    Önce en eski mesajlar çıkarılır; son keep_last mesaj her zaman korunur.
    """
    if not messages:
        return messages
    total = sum(estimate_tokens(m["content"]) for m in messages)
    if total <= budget:
        return messages

    result = list(messages)
    protected = result[-keep_last:] if keep_last else []
    candidates = result[:-keep_last] if keep_last else result

    prot_tokens = sum(estimate_tokens(m["content"]) for m in protected)
    cand_tokens = sum(estimate_tokens(m["content"]) for m in candidates)
    while candidates and cand_tokens + prot_tokens > budget:
        cand_tokens -= estimate_tokens(candidates.pop(0)["content"])

    return candidates + protected


# ═══════════════════════════════════════════════════════════════
#  RAG — Retrieval-Augmented Generation (Vektör Bellek)
# ═══════════════════════════════════════════════════════════════

class LLMEmbeddingFunction:
    """
    Yerel LLM sunucusunun /v1/embeddings endpointini embedding kaynağı olarak kullanır.
    ChromaDB EmbeddingFunction protokolüne uyumludur.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8080",
                 model: str = "local"):
        self._url = f"{base_url}/v1/embeddings"
        self._model = model
        self._dim: Optional[int] = None

    def __call__(self, input: List[str]) -> List[List[float]]:
        if not input:
            return []
        resp = http_req.post(
            self._url,
            json={"model": self._model, "input": input},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        embeddings = [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]
        if embeddings and self._dim is None:
            self._dim = len(embeddings[0])
        return embeddings

    @property
    def dimension(self) -> Optional[int]:
        return self._dim


class VectorStore:
    """
    ChromaDB tabanlı vektör bellek — RAG sistemi.

    Mesajları chunk'lara bölerek indeksler ve semantik arama ile
    geçmiş bağlamı çeker. L3 arşivideki yıl önceki konuşmalar bile
    sorgulanabilir.

    Embedding kaynakları (öncelik sırasıyla):
        1. Yerel LLM /v1/embeddings endpoint
        2. ChromaDB default (all-MiniLM-L6-v2 ONNX)
        3. RAG devre dışı kalır
    """

    def __init__(self, persist_dir: str = RAG_DIR,
                 collection_name: str = RAG_COLLECTION,
                 llm_port: int = 8080):
        self._persist_dir = persist_dir
        self._collection_name = collection_name
        self._llm_port = llm_port

        self._client = None
        self._collection = None
        self._embed_fn = None
        self._available = False
        self._stats = {"indexed": 0, "queries": 0, "errors": 0}
        self._lock = threading.Lock()

        self._init()

    # ── Başlatma ──────────────────────────────────────────────

    def _init(self) -> None:
        """ChromaDB istemcisini ve embedding fonksiyonunu başlat."""
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings
        except ImportError:
            log.warning("RAG: chromadb yüklü değil → pip install chromadb")
            return

        os.makedirs(self._persist_dir, exist_ok=True)

        # Embedding fonksiyonu seç
        self._embed_fn = self._pick_embedding_fn()
        if self._embed_fn is None:
            log.warning("RAG: Hiçbir embedding kaynağı bulunamadı, RAG devre dışı")
            return

        try:
            self._client = chromadb.PersistentClient(
                path=self._persist_dir,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                embedding_function=self._embed_fn,
                metadata={"hnsw:space": "cosine"},
            )
            count = self._collection.count()
            self._stats["indexed"] = count
            self._available = True
            log.info(f"RAG: ChromaDB hazır — {count} vektör ({self._persist_dir})")
        except Exception as e:
            log.error(f"RAG: ChromaDB başlatma hatası: {e}")
            self._available = False

    def _pick_embedding_fn(self):
        """En uygun embedding fonksiyonunu seç."""
        # 1. Yerel LLM endpoint
        try:
            fn = LLMEmbeddingFunction(
                base_url=f"http://127.0.0.1:{self._llm_port}",
            )
            test = fn(["test"])
            if test and len(test[0]) > 0:
                log.info(f"RAG: LLM embedding aktif (dim={len(test[0])})")
                return fn
        except Exception as e:
            log.debug(f"RAG: LLM embedding kullanılamıyor: {e}")

        # 2. ChromaDB default embedding (ONNX all-MiniLM-L6-v2)
        try:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
            fn = DefaultEmbeddingFunction()
            test = fn(["test"])
            if test and len(test[0]) > 0:
                log.info(f"RAG: Default embedding aktif (dim={len(test[0])})")
                return fn
        except Exception as e:
            log.debug(f"RAG: Default embedding kullanılamıyor: {e}")

        return None

    # ── Durum ─────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self._available and self._collection is not None

    @property
    def count(self) -> int:
        """Vektör deposundaki toplam belge sayısı."""
        if not self.available:
            return 0
        try:
            return self._collection.count()
        except Exception:
            return 0

    def get_stats(self) -> Dict[str, Any]:
        """RAG istatistikleri."""
        with self._lock:
            stats = dict(self._stats)
        stats["available"] = self.available
        stats["enabled"] = RAG_ENABLED
        stats["persist_dir"] = self._persist_dir
        if self.available:
            try:
                stats["indexed"] = self._collection.count()
            except Exception:
                pass
            # Disk boyutu
            total = 0
            for root, dirs, files in os.walk(self._persist_dir):
                for f in files:
                    total += os.path.getsize(os.path.join(root, f))
            stats["disk_mb"] = round(total / 1024**2, 2)
        else:
            stats["disk_mb"] = 0
        stats["embed_type"] = (
            "llm" if isinstance(self._embed_fn, LLMEmbeddingFunction)
            else "default" if self._embed_fn else "none"
        )
        return stats

    # ── Metin parçalama ───────────────────────────────────────

    @staticmethod
    def _chunk_text(text: str, size: int = RAG_CHUNK_SIZE,
                    overlap: int = RAG_CHUNK_OVERLAP) -> List[str]:
        """Uzun metni kelime sınırlarından parçalara böl."""
        if len(text) <= size:
            return [text]

        chunks: List[str] = []
        start = 0
        while start < len(text):
            end = start + size
            if end < len(text):
                # Kelime sınırında kes
                space_idx = text.rfind(' ', start, end)
                if space_idx > start:
                    end = space_idx
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start = end - overlap if overlap and end < len(text) else end
        return chunks

    # ── İndeksleme ────────────────────────────────────────────

    def index_message(self, chat_id: str, role: str, content: str,
                      ts: int = 0, msg_id: Optional[int] = None) -> int:
        """
        Tek bir mesajı vektör deposuna indeksle.
        Mesaj uzunsa parçalara bölünür.
        Dönüş: eklenen chunk sayısı.
        """
        if not self.available or not content or len(content.strip()) < 20:
            return 0

        chunks = self._chunk_text(content)
        ids, docs, metas = [], [], []

        for i, chunk in enumerate(chunks):
            doc_id = f"{chat_id}:{msg_id or ts}:{i}"
            ids.append(doc_id)
            docs.append(chunk)
            metas.append({
                "chat_id": chat_id,
                "role": role,
                "ts": ts or int(time.time()),
                "chunk_idx": i,
                "total_chunks": len(chunks),
                "msg_id": msg_id or 0,
            })

        try:
            with self._lock:
                self._collection.upsert(
                    ids=ids,
                    documents=docs,
                    metadatas=metas,
                )
            self._stats["indexed"] = self._collection.count()
            return len(chunks)
        except Exception as e:
            self._stats["errors"] += 1
            log.warning(f"RAG index hatası: {e}")
            return 0

    def index_messages_batch(self, messages: List[Dict[str, Any]]) -> int:
        """
        Toplu mesaj indeksleme.
        Her dict: {chat_id, role, content, ts, msg_id}
        """
        if not self.available:
            return 0

        all_ids, all_docs, all_metas = [], [], []

        for msg in messages:
            content = msg.get("content", "")
            if not content or len(content.strip()) < 20:
                continue
            chat_id = msg["chat_id"]
            role = msg.get("role", "user")
            ts = msg.get("ts", int(time.time()))
            msg_id = msg.get("msg_id", 0)

            chunks = self._chunk_text(content)
            for i, chunk in enumerate(chunks):
                doc_id = f"{chat_id}:{msg_id or ts}:{i}"
                all_ids.append(doc_id)
                all_docs.append(chunk)
                all_metas.append({
                    "chat_id": chat_id,
                    "role": role,
                    "ts": ts,
                    "chunk_idx": i,
                    "total_chunks": len(chunks),
                    "msg_id": msg_id,
                })

        if not all_ids:
            return 0

        total = 0
        try:
            with self._lock:
                # Batch'ler halinde yükle (ChromaDB limitleri için)
                for start in range(0, len(all_ids), RAG_INDEX_BATCH):
                    end = start + RAG_INDEX_BATCH
                    self._collection.upsert(
                        ids=all_ids[start:end],
                        documents=all_docs[start:end],
                        metadatas=all_metas[start:end],
                    )
                    total += min(end, len(all_ids)) - start
            self._stats["indexed"] = self._collection.count()
        except Exception as e:
            self._stats["errors"] += 1
            log.error(f"RAG batch index hatası: {e}")
        return min(total, len(all_ids))

    # ── Arama / Retrieval ─────────────────────────────────────

    def search(self, query: str, chat_id: Optional[str] = None,
               k: int = RAG_TOP_K, min_score: float = RAG_MIN_SCORE,
               ) -> List[Dict[str, Any]]:
        """
        Semantik arama. En benzer k belge parçasını döndürür.

        Dönüş: [{"content", "chat_id", "role", "ts", "score"}, …]
        """
        if not self.available or not query.strip():
            return []

        self._stats["queries"] += 1

        where_filter = {"chat_id": chat_id} if chat_id else None
        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=k,
                where=where_filter,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            self._stats["errors"] += 1
            log.warning(f"RAG arama hatası: {e}")
            return []

        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]

        items: List[Dict[str, Any]] = []
        for doc, meta, dist in zip(docs, metas, dists):
            score = 1.0 - dist   # cosine distance → similarity
            if score < min_score:
                continue
            items.append({
                "content": doc,
                "chat_id": meta.get("chat_id", ""),
                "role": meta.get("role", ""),
                "ts": meta.get("ts", 0),
                "score": round(score, 4),
                "msg_id": meta.get("msg_id", 0),
            })
        return items

    def build_rag_context(self, query: str, chat_id: Optional[str] = None,
                          token_budget: int = 1024) -> str:
        """
        Semantik arama sonuçlarını LLM'e enjekte edilecek
        tek bir bağlam stringine dönüştür.
        """
        results = self.search(query, chat_id=chat_id, k=RAG_TOP_K * 2)
        if not results:
            return ""

        lines: List[str] = []
        used_tokens = 0
        seen: set = set()

        for r in results:
            snippet = r["content"].strip()
            # Dedup
            sig = snippet[:100]
            if sig in seen:
                continue
            seen.add(sig)

            tok = estimate_tokens(snippet)
            if used_tokens + tok > token_budget:
                break

            ts_str = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M") if r["ts"] else "?"
            role_label = {"user": "Kullanıcı", "assistant": "AI", "system": "Özet"}.get(r["role"], r["role"])
            lines.append(f"[{ts_str} | {role_label} | Skor: {r['score']}]\n{snippet}")
            used_tokens += tok

        if not lines:
            return ""

        return (
            "══ İLGİLİ GEÇMİŞ BAĞLAM (RAG) ══\n"
            "Aşağıdaki geçmiş konuşma parçaları, kullanıcının mevcut sorusuyla "
            "anlamsal olarak benzer bulundu. Yanıtında bu bilgileri doğal şekilde "
            "kullan, ancak 'geçmiş bağlamda gördüm' gibi ifadeler kullanma.\n\n"
            + "\n---\n".join(lines)
        )

    # ── Silme / Sıfırlama ────────────────────────────────────

    def delete_chat(self, chat_id: str) -> int:
        """Belirli bir sohbetin tüm vektörlerini sil."""
        if not self.available:
            return 0
        try:
            with self._lock:
                self._collection.delete(where={"chat_id": chat_id})
            n = self._stats["indexed"] - self._collection.count()
            self._stats["indexed"] = self._collection.count()
            return max(0, n)
        except Exception as e:
            log.warning(f"RAG silme hatası: {e}")
            return 0

    def reset(self) -> bool:
        """Tüm vektör verisini sıfırla."""
        if not self._client:
            return False
        try:
            with self._lock:
                self._client.delete_collection(self._collection_name)
                self._collection = self._client.get_or_create_collection(
                    name=self._collection_name,
                    embedding_function=self._embed_fn,
                    metadata={"hnsw:space": "cosine"},
                )
            self._stats["indexed"] = 0
            log.info("RAG: Vektör deposu sıfırlandı")
            return True
        except Exception as e:
            log.error(f"RAG reset hatası: {e}")
            return False

    def try_reinit(self, llm_port: int = 8080) -> bool:
        """
        Embedding kaynağı değiştiğinde (ör. LLM başlatıldığında)
        bağlantıyı yeniden dene.
        """
        self._llm_port = llm_port
        self._available = False
        self._init()
        return self.available


# ═══════════════════════════════════════════════════════════════
#  KATMAN 2+3 — SQLite Bağlantı Havuzu
# ═══════════════════════════════════════════════════════════════

class ConnectionPool:
    """Thread-yerel SQLite bağlantıları."""

    def __init__(self, db_path: str):
        self._path = db_path
        self._local = threading.local()

    def get(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        else:
            # Stale connection check
            try:
                conn.execute("SELECT 1")
            except Exception:
                log.warning("Stale DB connection detected, reconnecting")
                try:
                    conn.close()
                except Exception:
                    pass
                conn = self._new_conn()
                self._local.conn = conn
        return conn

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-65536")     # 64 MB sayfa önbelleği
        conn.execute("PRAGMA mmap_size=536870912")   # 512 MB bellek haritalama
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA wal_autocheckpoint=2000")
        conn.execute("PRAGMA locking_mode=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def tx(self):
        """Otomatik commit/rollback context manager."""
        conn = self.get()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close_local(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None


# ═══════════════════════════════════════════════════════════════
#  ANA BELLEK YÖNETİCİSİ
# ═══════════════════════════════════════════════════════════════

class MemoryManager:
    """
    3+RAG katmanlı akıllı bellek sistemi:

    L1  LRU RAM cache        — en hızlı, sınırlı kapasiteli
    L2  SQLite compressed    — aktif mesajlar, zlib-6
    L3  SQLite archive       — 30+ günlük, zlib-9 + budanmış
    L4  VectorStore (RAG)    — ChromaDB semantik arama, tüm geçmiş

    Ek özellikler:
    - LLM özetleme: L2 → özetle sıkıştır (token tasarrufu)
    - Görsel SHA-256 dedup + thumbnail
    - Yazma tamponu: toplu commit
    - Periyodik bakım: VACUUM + WAL checkpoint + L2→L3 taşıma
    - Gerçek zamanlı metrikler
    - RAG: Semantik benzerlik ile geçmiş bağlam çekme
    """

    def __init__(
        self,
        db_path: str = DB_PATH,
        max_msgs_per_chat: int = L2_MAX_MSGS_PER_CHAT,
        cache_size: int = L1_CACHE_SIZE,
        cache_ttl: int = L1_CACHE_TTL,
        ctx_window: int = DEFAULT_CTX_WINDOW,
        maintenance_interval: int = 3600,
        llm_port: int = 8080,            # özetleme için
        enable_summarization: bool = True,
    ):
        self.db_path   = str(Path(db_path).resolve())
        self.max_msgs  = max_msgs_per_chat
        self.ctx_window = ctx_window
        self.maint_interval = maintenance_interval
        self.llm_port = llm_port
        self.enable_summarization = enable_summarization

        self._pool    = ConnectionPool(self.db_path)
        self._cache   = LRUCache(maxsize=cache_size, ttl=cache_ttl)
        self._wbuf    = WriteBuffer()
        self._wlock   = threading.Lock()   # toplu yazma kilidi

        # Metrikler
        self._metrics: Dict[str, Any] = defaultdict(int)
        self._metrics["start_time"] = time.time()

        # RAG — Vektör bellek (L4)
        if RAG_ENABLED:
            self._rag = VectorStore(
                persist_dir=RAG_DIR,
                collection_name=RAG_COLLECTION,
                llm_port=llm_port,
            )
        else:
            self._rag = None

        self._init_schema()
        self._start_workers()
        log.info(f"MemoryManager hazır → {self.db_path}"
                 f" | RAG: {'✓' if self._rag and self._rag.available else '✗'}")

    # ── Şema ────────────────────────────────────────────────

    def _init_schema(self) -> None:
        with self._pool.tx() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id      TEXT    NOT NULL,
                    role         TEXT    NOT NULL CHECK(role IN ('user','assistant','system','summary')),
                    content      BLOB    NOT NULL,
                    content_len  INTEGER NOT NULL DEFAULT 0,
                    token_est    INTEGER NOT NULL DEFAULT 0,
                    is_summary   INTEGER NOT NULL DEFAULT 0,
                    ts           INTEGER NOT NULL DEFAULT (strftime('%s','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_msg_chat_id
                    ON messages(chat_id, id);
                CREATE INDEX IF NOT EXISTS idx_msg_ts
                    ON messages(ts);

                CREATE TABLE IF NOT EXISTS messages_archive (
                    id           INTEGER PRIMARY KEY,
                    chat_id      TEXT    NOT NULL,
                    role         TEXT    NOT NULL,
                    content      BLOB    NOT NULL,
                    content_len  INTEGER NOT NULL DEFAULT 0,
                    is_summary   INTEGER NOT NULL DEFAULT 0,
                    archived_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                    ts           INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_arch_chat
                    ON messages_archive(chat_id, ts);

                CREATE TABLE IF NOT EXISTS images (
                    hash        TEXT    PRIMARY KEY,
                    mime        TEXT    NOT NULL,
                    data        BLOB    NOT NULL,
                    thumb       BLOB,
                    size_orig   INTEGER NOT NULL DEFAULT 0,
                    size_stored INTEGER NOT NULL DEFAULT 0,
                    ref_count   INTEGER NOT NULL DEFAULT 1,
                    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                    last_used   INTEGER NOT NULL DEFAULT (strftime('%s','now'))
                );

                CREATE TABLE IF NOT EXISTS contacts (
                    id          TEXT    PRIMARY KEY,
                    name        TEXT    DEFAULT '',
                    pushname    TEXT    DEFAULT '',
                    ai_enabled  INTEGER NOT NULL DEFAULT 1,
                    msg_count   INTEGER NOT NULL DEFAULT 0,
                    last_seen   INTEGER NOT NULL DEFAULT 0,
                    updated_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
                );

                CREATE TABLE IF NOT EXISTS maintenance_log (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    action   TEXT    NOT NULL,
                    details  TEXT,
                    ts       INTEGER NOT NULL DEFAULT (strftime('%s','now'))
                );

                CREATE TABLE IF NOT EXISTS metrics_hourly (
                    hour       INTEGER PRIMARY KEY,
                    msgs_in    INTEGER DEFAULT 0,
                    msgs_out   INTEGER DEFAULT 0,
                    searches   INTEGER DEFAULT 0,
                    img_saves  INTEGER DEFAULT 0,
                    errors     INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS webchat_users (
                    id               TEXT    PRIMARY KEY,
                    username         TEXT    NOT NULL DEFAULT 'Anonim',
                    ip               TEXT    NOT NULL DEFAULT '',
                    enabled          INTEGER NOT NULL DEFAULT 1,
                    rate_limit_hour  INTEGER NOT NULL DEFAULT 20,
                    daily_limit      INTEGER NOT NULL DEFAULT 100,
                    max_tokens       INTEGER NOT NULL DEFAULT 2048,
                    sys_prompt       TEXT    NOT NULL DEFAULT '',
                    msg_count        INTEGER NOT NULL DEFAULT 0,
                    experts          TEXT    NOT NULL DEFAULT '{}',
                    created_at       INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                    last_seen        INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS webchat_rate_log (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id  TEXT    NOT NULL,
                    ts       INTEGER NOT NULL DEFAULT (strftime('%s','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_rate_log ON webchat_rate_log(user_id, ts);
            """)
            self._migrate_if_needed(conn)

    def _migrate_if_needed(self, conn: sqlite3.Connection) -> None:
        """Eski TEXT sütunlu tabloları BLOB'a göç et."""
        info = {r["name"]: r["type"]
                for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if info.get("content", "").upper() == "TEXT":
            log.info("Şema göçü: TEXT → BLOB sıkıştırma başlıyor…")
            rows = conn.execute(
                "SELECT id, chat_id, role, content FROM messages"
            ).fetchall()
            conn.execute("ALTER TABLE messages RENAME TO messages_old")
            conn.executescript("""
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL, role TEXT NOT NULL,
                    content BLOB NOT NULL, content_len INTEGER NOT NULL DEFAULT 0,
                    token_est INTEGER NOT NULL DEFAULT 0,
                    is_summary INTEGER NOT NULL DEFAULT 0,
                    ts INTEGER NOT NULL DEFAULT (strftime('%s','now'))
                );
                CREATE INDEX idx_msg_chat_id ON messages(chat_id, id);
                CREATE INDEX idx_msg_ts ON messages(ts);
            """)
            for row in rows:
                text = row["content"] or ""
                blob = _encode(text)
                conn.execute(
                    "INSERT INTO messages(id,chat_id,role,content,content_len,token_est) VALUES(?,?,?,?,?,?)",
                    (row["id"], row["chat_id"], row["role"],
                     blob, len(text), estimate_tokens(text))
                )
            conn.execute("DROP TABLE messages_old")
            log.info(f"Göç tamamlandı: {len(rows)} mesaj sıkıştırıldı.")

    # ── Arka Plan İşçileri ───────────────────────────────────

    def _start_workers(self) -> None:
        import atexit
        atexit.register(self._flush_remaining)

        def _flush_worker():
            while True:
                time.sleep(WRITE_BUFFER_TIMEOUT)
                rows = self._wbuf.drain()
                if rows:
                    self._bulk_insert(rows)

        def _maintenance_worker():
            time.sleep(60)
            while True:
                try:
                    self.run_maintenance()
                except Exception as e:
                    log.error(f"Bakım hatası: {e}")
                time.sleep(self.maint_interval)

        def _cache_sweep_worker():
            while True:
                time.sleep(120)
                n = self._cache.sweep()
                if n:
                    log.debug(f"Cache sweep: {n} bayat giriş temizlendi")

        for fn, name in [
            (_flush_worker,      "mm-flush"),
            (_maintenance_worker,"mm-maint"),
            (_cache_sweep_worker,"mm-sweep"),
        ]:
            t = threading.Thread(target=fn, daemon=True, name=name)
            t.start()

    def _flush_remaining(self) -> None:
        """Shutdown sırasında tamponu boşalt — veri kaybını önle."""
        rows = self._wbuf.drain()
        if rows:
            try:
                self._bulk_insert(rows)
                log.info(f"Shutdown flush: {len(rows)} mesaj kaydedildi")
            except Exception as e:
                log.error(f"Shutdown flush hatası: {e}")

    # ── Mesaj Yazma ─────────────────────────────────────────

    def save_message(self, chat_id: str, role: str, content: str) -> None:
        """Mesajı tampona ekle; dolunca toplu commit. RAG'a da indeksle."""
        blob      = _encode(content)
        tok_est   = estimate_tokens(content)
        ts        = int(time.time())
        row       = (chat_id, role, blob, len(content), tok_est, ts)

        should_flush = self._wbuf.add(row)
        self._cache.delete_prefix(f"msgs:{chat_id}")
        self._metrics["msgs_in"] += 1

        if should_flush:
            rows = self._wbuf.drain()
            if rows:
                self._bulk_insert(rows)

        # RAG indeksleme (arka plan thread'de, bloklamaz)
        if self._rag and self._rag.available:
            threading.Thread(
                target=self._rag.index_message,
                args=(chat_id, role, content, ts),
                daemon=True,
            ).start()

    def _bulk_insert(self, rows: List[Tuple]) -> None:
        with self._wlock:
            with self._pool.tx() as conn:
                conn.executemany(
                    """INSERT INTO messages
                       (chat_id,role,content,content_len,token_est,ts)
                       VALUES (?,?,?,?,?,?)""",
                    rows
                )
                # Budama: her etkilenen chat_id için
                chat_ids = set(r[0] for r in rows)
                for cid in chat_ids:
                    self._prune_chat(conn, cid)
                # Saatlik metrik güncelle
                hour = int(time.time() // 3600) * 3600
                conn.execute("""
                    INSERT INTO metrics_hourly(hour, msgs_in)
                    VALUES(?,?)
                    ON CONFLICT(hour) DO UPDATE SET msgs_in=msgs_in+excluded.msgs_in
                """, (hour, len(rows)))

    def _prune_chat(self, conn: sqlite3.Connection, chat_id: str) -> int:
        """
        chat_id için L2_MAX_MSGS_PER_CHAT limitini uygula.
        Eğer özetleme aktifse: eski N mesajı sil-değil-özetle (async).
        Değilse: doğrudan sil.
        """
        cnt = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,)
        ).fetchone()[0]

        if cnt <= self.max_msgs:
            return 0

        overflow = cnt - self.max_msgs
        # Silinecek satırları al
        old_rows = conn.execute(
            "SELECT id FROM messages WHERE chat_id=? ORDER BY id ASC LIMIT ?",
            (chat_id, overflow)
        ).fetchall()
        old_ids = [r[0] for r in old_rows]

        conn.execute(
            f"DELETE FROM messages WHERE id IN ({','.join('?'*len(old_ids))})",
            old_ids
        )
        return len(old_ids)

    # ── Mesaj Okuma (3 katmanlı) ─────────────────────────────

    def get_recent_messages(
        self,
        chat_id: str,
        limit: int = 10,
        token_budget: Optional[int] = None,
        include_archive: bool = False,
    ) -> List[Dict[str, str]]:
        """
        Son N mesajı döndür.
        token_budget verilirse context window'a sığdırır.
        include_archive=True → arşivlenmiş mesajları da dahil eder.
        """
        cache_key = f"msgs:{chat_id}:{limit}:{token_budget}:{include_archive}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._metrics["cache_hits"] = self._metrics.get("cache_hits", 0) + 1
            return cached

        # Tampondaki henüz kaydedilmemiş mesajları zorla flush et
        if self._wbuf.size() > 0:
            pending = self._wbuf.drain()
            if pending:
                self._bulk_insert(pending)

        conn = self._pool.get()
        rows = conn.execute("""
            SELECT role, content FROM (
                SELECT role, content, id
                FROM messages WHERE chat_id=?
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
        """, (chat_id, limit)).fetchall()

        result = [
            {"role": r["role"], "content": _decode(r["content"])}
            for r in rows
        ]

        if include_archive and len(result) < limit:
            remaining = limit - len(result)
            arch_rows = conn.execute("""
                SELECT role, content FROM messages_archive
                WHERE chat_id=?
                ORDER BY ts DESC LIMIT ?
            """, (chat_id, remaining)).fetchall()
            arch = [
                {"role": r["role"], "content": _decode(r["content"])}
                for r in reversed(arch_rows)
            ]
            result = arch + result

        if token_budget:
            result = fit_messages_to_budget(
                result, token_budget,
                keep_last=min(4, len(result))
            )

        self._cache.set(cache_key, result)
        return result

    # ── LLM Özetleme ─────────────────────────────────────────

    def summarize_old_context(self, chat_id: str) -> Optional[str]:
        """
        LLM ile eski mesajları özetle, orijinalleri sil.
        Arşivden önce son özetleme şansı.
        """
        if not self.enable_summarization:
            return None

        conn = self._pool.get()
        # En eski %40'ı al (son %60 korunacak)
        total = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,)
        ).fetchone()[0]
        if total < 20:
            return None  # özetlemek için yeterli mesaj yok

        old_limit = max(10, int(total * 0.4))
        old_rows = conn.execute("""
            SELECT id, role, content FROM messages
            WHERE chat_id=?
            ORDER BY id ASC LIMIT ?
        """, (chat_id, old_limit)).fetchall()

        if not old_rows:
            return None

        msgs_text = "\n".join(
            f"{r['role'].upper()}: {_decode(r['content'])[:500]}"
            for r in old_rows
        )
        prompt = (
            "Aşağıdaki sohbet geçmişini 3-5 cümleyle Türkçe özetle. "
            "Önemli kararları, bilgileri ve bağlamı koru. "
            "Sadece özeti yaz, başka hiçbir şey ekleme.\n\n"
            f"{msgs_text}"
        )

        try:
            resp = http_req.post(
                f"http://127.0.0.1:{self.llm_port}/v1/chat/completions",
                json={
                    "model": "local",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 512,
                    "stream": False,
                },
                timeout=30,
            )
            summary = resp.json()["choices"][0]["message"]["content"].strip()
            if not summary:
                return None

            old_ids = [r["id"] for r in old_rows]
            with self._pool.tx() as conn2:
                # Orijinalleri arşive taşı
                conn2.executemany("""
                    INSERT OR IGNORE INTO messages_archive
                    (id, chat_id, role, content, content_len, is_summary, ts)
                    SELECT id, chat_id, role, content, content_len, is_summary, ts
                    FROM messages WHERE id=?
                """, [(i,) for i in old_ids])
                # Orijinalleri sil
                conn2.execute(
                    f"DELETE FROM messages WHERE id IN ({','.join('?'*len(old_ids))})",
                    old_ids
                )
                # Özet mesajı ekle
                blob = _encode(summary)
                conn2.execute("""
                    INSERT INTO messages
                    (chat_id, role, content, content_len, token_est, is_summary, ts)
                    VALUES (?,?,?,?,?,?,?)
                """, (chat_id, "system", blob, len(summary),
                      estimate_tokens(summary), 1, int(time.time())))

            self._cache.delete_prefix(f"msgs:{chat_id}")
            log.info(f"Özet oluşturuldu: {chat_id} ({old_limit} mesaj → 1 özet)")
            return summary

        except Exception as e:
            log.warning(f"Özetleme başarısız: {e}")
            return None

    # ── Görsel Depolama ──────────────────────────────────────

    def save_image(self, b64_data: str, mime: str) -> str:
        """
        Base64 görsel → SHA-256 dedup + zlib sıkıştırma.
        Aynı görsel varsa ref_count artar, tekrar yazılmaz.
        """
        raw   = b64mod.b64decode(b64_data)
        img_h = hashlib.sha256(raw).hexdigest()

        with self._pool.tx() as conn:
            row = conn.execute(
                "SELECT hash FROM images WHERE hash=?", (img_h,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE images SET ref_count=ref_count+1, last_used=? WHERE hash=?",
                    (int(time.time()), img_h)
                )
                return img_h

            comp = zlib.compress(raw, level=6)
            # Küçük thumbnail oluştur (sadece JPEG/PNG)
            thumb = self._make_thumbnail(raw, mime)

            conn.execute("""
                INSERT INTO images
                (hash, mime, data, thumb, size_orig, size_stored, last_used)
                VALUES (?,?,?,?,?,?,?)
            """, (img_h, mime, comp, thumb,
                  len(raw), len(comp), int(time.time())))

        self._metrics["img_saves"] = self._metrics.get("img_saves", 0) + 1
        ratio = len(comp) / len(raw) * 100
        log.debug(f"Görsel: {img_h[:10]}… {len(raw)//1024}KB → {len(comp)//1024}KB (%{ratio:.0f})")
        return img_h

    def _make_thumbnail(self, raw: bytes, mime: str,
                        size: Tuple[int,int] = (64, 64)) -> Optional[bytes]:
        """Küçük thumbnail oluştur (PIL opsiyonel)."""
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(raw))
            resample = getattr(Image, "Resampling", Image).LANCZOS
            img.thumbnail(size, resample)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=60, optimize=True)
            return buf.getvalue()
        except Exception:
            return None

    def get_image(self, img_hash: str) -> Optional[Tuple[str, str]]:
        """(base64_data, mime) veya None döndür."""
        cache_key = f"img:{img_hash}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        conn = self._pool.get()
        row = conn.execute(
            "SELECT data, mime FROM images WHERE hash=?", (img_hash,)
        ).fetchone()
        if not row:
            return None

        conn.execute(
            "UPDATE images SET last_used=? WHERE hash=?",
            (int(time.time()), img_hash)
        )
        raw = zlib.decompress(row["data"])
        b64 = b64mod.b64encode(raw).decode("ascii")
        result = (b64, row["mime"])
        self._cache.set(cache_key, result)
        return result

    def get_image_thumbnail(self, img_hash: str) -> Optional[bytes]:
        """Thumbnail bytes (JPEG) döndür."""
        conn = self._pool.get()
        row = conn.execute(
            "SELECT thumb FROM images WHERE hash=?", (img_hash,)
        ).fetchone()
        return row["thumb"] if row else None

    # ── Kişi İşlemleri ──────────────────────────────────────

    def upsert_contact(self, cid: str, name: str, pushname: str) -> None:
        with self._pool.tx() as conn:
            conn.execute("""
                INSERT INTO contacts(id,name,pushname,ai_enabled,updated_at)
                VALUES(?,?,?,1,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    pushname=excluded.pushname,
                    updated_at=excluded.updated_at
            """, (cid, name, pushname, int(time.time())))
        self._cache.delete("contacts:all")

    def get_contacts(self) -> List[Dict[str, Any]]:
        cached = self._cache.get("contacts:all")
        if cached is not None:
            return cached
        conn = self._pool.get()
        rows = conn.execute("""
            SELECT id, name, pushname, ai_enabled, msg_count, last_seen
            FROM contacts ORDER BY name ASC, pushname ASC
        """).fetchall()
        result = [dict(r) for r in rows]
        self._cache.set("contacts:all", result)
        return result

    def toggle_ai(self, cid: str, enabled: bool) -> bool:
        with self._pool.tx() as conn:
            c = conn.execute(
                "UPDATE contacts SET ai_enabled=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, int(time.time()), cid)
            )
            rowcount = c.rowcount
        self._cache.delete("contacts:all")
        self._cache.delete(f"ai:{cid}")
        return rowcount > 0

    def is_ai_enabled(self, cid: str) -> bool:
        cached = self._cache.get(f"ai:{cid}")
        if cached is not None:
            return cached
        conn = self._pool.get()
        row = conn.execute(
            "SELECT ai_enabled FROM contacts WHERE id=?", (cid,)
        ).fetchone()
        result = bool(row and row["ai_enabled"] == 1)
        self._cache.set(f"ai:{cid}", result)
        return result

    def record_contact_message(self, cid: str) -> None:
        """Kişinin mesaj sayısını ve son görülme zamanını güncelle."""
        with self._pool.tx() as conn:
            conn.execute("""
                UPDATE contacts SET
                    msg_count=msg_count+1,
                    last_seen=?
                WHERE id=?
            """, (int(time.time()), cid))

    # ── Bakım ───────────────────────────────────────────────

    def purge_old_images(self, days: int = 7) -> int:
        """Eski görselleri temizle."""
        cutoff = int(time.time()) - days * 86400
        with self._pool.tx() as conn:
            conn.execute(
                "DELETE FROM images WHERE last_used < ? AND ref_count <= 1", (cutoff,)
            )
            n = conn.execute("SELECT changes()").fetchone()[0]
        self._cache.delete_prefix("img:")
        return n

    def run_maintenance(self) -> Dict[str, Any]:
        """
        1. Tampondaki mesajları flush et
        2. L2→L3 arşivleme (30+ günlük)
        3. Eski görselleri temizle (60+ gün)
        4. WAL checkpoint
        5. VACUUM
        6. Bakım logu
        """
        start  = time.time()
        report: Dict[str, Any] = {}

        # Flush
        pending = self._wbuf.drain()
        if pending:
            self._bulk_insert(pending)

        cutoff_archive = int(time.time()) - L3_ARCHIVE_THRESHOLD * 86400
        cutoff_images  = int(time.time()) - 60 * 86400

        with self._pool.tx() as conn:
            # L2 → L3 arşivleme
            old = conn.execute("""
                SELECT id, chat_id, role, content, content_len, is_summary, ts
                FROM messages WHERE ts < ?
            """, (cutoff_archive,)).fetchall()

            archived = 0
            if old:
                conn.executemany("""
                    INSERT OR IGNORE INTO messages_archive
                    (id, chat_id, role, content, content_len, is_summary, ts)
                    VALUES(?,?,?,?,?,?,?)
                """, [(r["id"],r["chat_id"],r["role"],r["content"],
                       r["content_len"],r["is_summary"],r["ts"]) for r in old])

                # Arşivlenen mesajları L2'de sıkıştır (seviye-9)
                for r in old:
                    text  = _decode(r["content"])
                    blob9 = _encode(text, archive=True)
                    conn.execute(
                        "UPDATE messages_archive SET content=? WHERE id=?",
                        (blob9, r["id"])
                    )

                ids = [r["id"] for r in old]
                conn.execute(
                    f"DELETE FROM messages WHERE id IN ({','.join('?'*len(ids))})",
                    ids
                )
                archived = len(ids)

                # Arşivlenen mesajları RAG'a indeksle
                if self._rag and self._rag.available:
                    rag_batch = []
                    for r in old:
                        text = _decode(r["content"])
                        if text and len(text.strip()) >= 20:
                            rag_batch.append({
                                "chat_id": r["chat_id"],
                                "role": r["role"],
                                "content": text,
                                "ts": r["ts"],
                                "msg_id": r["id"],
                            })
                    if rag_batch:
                        indexed = self._rag.index_messages_batch(rag_batch)
                        log.info(f"RAG: Bakım sırasında {indexed} arşiv parçası indekslendi")

            # Rate log temizliği (48 saatten eski)
            cutoff_rate = int(time.time()) - 48 * 3600
            conn.execute("DELETE FROM webchat_rate_log WHERE ts < ?", (cutoff_rate,))

            # Görsel temizliği
            conn.execute("DELETE FROM images WHERE last_used < ? AND ref_count <= 1",
                         (cutoff_images,))
            img_purged = conn.execute("SELECT changes()").fetchone()[0]

            # DB boyutu (önce)
            page_size  = conn.execute("PRAGMA page_size").fetchone()[0]
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            db_before  = page_size * page_count

            # WAL checkpoint
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        # VACUUM (transaction dışında)
        raw_conn = self._pool.get()
        raw_conn.execute("VACUUM")
        raw_conn.execute("PRAGMA optimize")
        raw_conn.commit()

        page_count2 = raw_conn.execute("PRAGMA page_count").fetchone()[0]
        page_size2  = raw_conn.execute("PRAGMA page_size").fetchone()[0]
        db_after    = page_size2 * page_count2

        report = {
            "archived_msgs": archived,
            "images_purged": img_purged,
            "db_before_mb":  round(db_before  / 1024**2, 2),
            "db_after_mb":   round(db_after   / 1024**2, 2),
            "freed_mb":      round((db_before - db_after) / 1024**2, 2),
            "duration_s":    round(time.time() - start, 2),
        }

        with self._pool.tx() as conn:
            conn.execute(
                "INSERT INTO maintenance_log(action,details) VALUES(?,?)",
                ("auto", json.dumps(report))
            )

        self._cache.clear()
        log.info(f"Bakım: {report}")
        return report

    # ── İstatistikler ────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        conn = self._pool.get()

        msgs  = conn.execute("""
            SELECT COUNT(*) cnt,
                   COUNT(DISTINCT chat_id) chats,
                   SUM(content_len)  chars,
                   SUM(LENGTH(content)) stored,
                   SUM(token_est) tokens
            FROM messages
        """).fetchone()

        arch  = conn.execute(
            "SELECT COUNT(*) cnt FROM messages_archive"
        ).fetchone()

        imgs  = conn.execute("""
            SELECT COUNT(*) cnt,
                   SUM(size_orig) orig,
                   SUM(size_stored) stored
            FROM images
        """).fetchone()

        db_sz = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

        c  = msgs["chars"] or 1
        s  = msgs["stored"] or c
        ic = imgs["orig"]   or 1
        iss= imgs["stored"] or ic

        return {
            "db": {
                "path":    self.db_path,
                "size_mb": round(db_sz / 1024**2, 2),
            },
            "messages": {
                "active":    msgs["cnt"],
                "archived":  arch["cnt"],
                "chats":     msgs["chats"],
                "max_per_chat": self.max_msgs,
                "total_chars": msgs["chars"] or 0,
                "stored_bytes": msgs["stored"] or 0,
                "compression_pct": round(s/c*100, 1),
                "saved_pct":       round((1 - s/c)*100, 1),
                "tokens_est":      msgs["tokens"] or 0,
            },
            "images": {
                "count":     imgs["cnt"],
                "orig_mb":   round((imgs["orig"]   or 0)/1024**2, 2),
                "stored_mb": round((imgs["stored"] or 0)/1024**2, 2),
                "saved_pct": round((1 - iss/ic)*100, 1),
            },
            "cache": self._cache.stats(),
            "buffer": {"pending": self._wbuf.size()},
            "rag": self.rag_stats(),
            "uptime_s": round(time.time() - self._metrics["start_time"], 0),
        }

    def delete_chat(self, chat_id: str) -> int:
        with self._pool.tx() as conn:
            cur1 = conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            cur2 = conn.execute("DELETE FROM messages_archive WHERE chat_id=?", (chat_id,))
            n = cur1.rowcount + cur2.rowcount
        self._cache.delete_prefix(f"msgs:{chat_id}")
        # RAG vektörlerini de sil
        if self._rag and self._rag.available:
            self._rag.delete_chat(chat_id)
        return n

    # ── RAG Yardımcıları ─────────────────────────────────────

    def rag_search(self, query: str, chat_id: Optional[str] = None,
                   k: int = RAG_TOP_K) -> List[Dict[str, Any]]:
        """Semantik arama — VectorStore üzerinden."""
        if not self._rag or not self._rag.available:
            return []
        return self._rag.search(query, chat_id=chat_id, k=k)

    def rag_build_context(self, query: str, chat_id: Optional[str] = None,
                          token_budget: int = 1024) -> str:
        """RAG bağlam stringi oluştur — LLM prompt'una enjekte için."""
        if not self._rag or not self._rag.available:
            return ""
        return self._rag.build_rag_context(query, chat_id=chat_id,
                                           token_budget=token_budget)

    def rag_stats(self) -> Dict[str, Any]:
        """RAG istatistiklerini döndür."""
        if not self._rag:
            return {"available": False, "enabled": RAG_ENABLED}
        return self._rag.get_stats()

    def rag_reindex_all(self) -> Dict[str, Any]:
        """
        Tüm L2 + L3 mesajları yeniden indeksle.
        Uzun sürebilir — arka plan thread'de çağrılmalı.
        """
        if not self._rag or not self._rag.available:
            return {"ok": False, "error": "RAG kullanılamıyor"}

        self._rag.reset()
        conn = self._pool.get()

        # L2 aktif mesajlar
        l2_rows = conn.execute("""
            SELECT id, chat_id, role, content, ts
            FROM messages ORDER BY id ASC
        """).fetchall()
        l2_batch = []
        for r in l2_rows:
            text = _decode(r["content"])
            if text and len(text.strip()) >= 20:
                l2_batch.append({
                    "chat_id": r["chat_id"], "role": r["role"],
                    "content": text, "ts": r["ts"], "msg_id": r["id"],
                })

        # L3 arşiv mesajları
        l3_rows = conn.execute("""
            SELECT id, chat_id, role, content, ts
            FROM messages_archive ORDER BY id ASC
        """).fetchall()
        l3_batch = []
        for r in l3_rows:
            text = _decode(r["content"])
            if text and len(text.strip()) >= 20:
                l3_batch.append({
                    "chat_id": r["chat_id"], "role": r["role"],
                    "content": text, "ts": r["ts"], "msg_id": r["id"],
                })

        total_l2 = self._rag.index_messages_batch(l2_batch) if l2_batch else 0
        total_l3 = self._rag.index_messages_batch(l3_batch) if l3_batch else 0

        result = {
            "ok": True,
            "l2_messages": len(l2_batch),
            "l3_messages": len(l3_batch),
            "l2_indexed": total_l2,
            "l3_indexed": total_l3,
            "total_vectors": self._rag.count,
        }
        log.info(f"RAG reindex: {result}")
        return result

    def rag_reset(self) -> bool:
        """RAG vektör deposunu sıfırla."""
        if not self._rag:
            return False
        return self._rag.reset()

    def rag_reinit(self, llm_port: int = 8080) -> bool:
        """RAG embedding bağlantısını yeniden dene."""
        if not self._rag:
            if RAG_ENABLED:
                self._rag = VectorStore(persist_dir=RAG_DIR,
                                        collection_name=RAG_COLLECTION,
                                        llm_port=llm_port)
                return self._rag.available
            return False
        return self._rag.try_reinit(llm_port)

    # ── Web Chat Kullanıcı Yönetimi ──────────────────────────

    def webchat_register(self, uid: str, username: str, ip: str) -> Dict[str, Any]:
        """Kullanıcıyı kaydet veya güncelle; bilgilerini döndür."""
        with self._pool.tx() as conn:
            conn.execute("""
                INSERT INTO webchat_users(id, username, ip, last_seen)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    username  = CASE WHEN excluded.username != 'Anonim'
                                     THEN excluded.username ELSE username END,
                    ip        = excluded.ip,
                    last_seen = excluded.last_seen
            """, (uid, username or "Anonim", ip, int(time.time())))
        self._cache.delete(f"wcu:{uid}")
        return self.webchat_get_user(uid)

    def webchat_get_user(self, uid: str) -> Optional[Dict[str, Any]]:
        cached = self._cache.get(f"wcu:{uid}")
        if cached is not None:
            return cached
        conn = self._pool.get()
        row = conn.execute(
            "SELECT * FROM webchat_users WHERE id=?", (uid,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        self._cache.set(f"wcu:{uid}", result)
        return result

    def webchat_list_users(self) -> List[Dict[str, Any]]:
        conn = self._pool.get()
        rows = conn.execute(
            "SELECT * FROM webchat_users ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def webchat_update_user(self, uid: str, **kwargs) -> bool:
        """Güncellenebilir alanlar: username, enabled, rate_limit_hour,
           daily_limit, max_tokens, sys_prompt, experts"""
        allowed = {"username", "enabled", "rate_limit_hour", "daily_limit",
                   "max_tokens", "sys_prompt", "experts"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        sets  = ", ".join(f'"{k}"=?' for k in fields)
        vals  = list(fields.values()) + [uid]
        with self._pool.tx() as conn:
            c = conn.execute(f"UPDATE webchat_users SET {sets} WHERE id=?", vals)
        self._cache.delete(f"wcu:{uid}")
        return c.rowcount > 0

    def webchat_check_rate(self, uid: str) -> Dict[str, Any]:
        """
        Kullanıcının bu saat ve bugünkü mesaj sayısını kontrol et.
        Döner: {allowed:bool, hourly_used, hourly_limit, daily_used, daily_limit}
        """
        user = self.webchat_get_user(uid)
        if not user:
            return {"allowed": False, "reason": "Kullanıcı bulunamadı"}
        if not user["enabled"]:
            return {"allowed": False, "reason": "Hesap devre dışı"}

        now   = int(time.time())
        hour_start = now - 3600
        day_start  = now - 86400

        conn = self._pool.get()
        hourly = conn.execute(
            "SELECT COUNT(*) FROM webchat_rate_log WHERE user_id=? AND ts >= ?",
            (uid, hour_start)
        ).fetchone()[0]
        daily = conn.execute(
            "SELECT COUNT(*) FROM webchat_rate_log WHERE user_id=? AND ts >= ?",
            (uid, day_start)
        ).fetchone()[0]

        if hourly >= user["rate_limit_hour"]:
            return {
                "allowed": False,
                "reason": f"Saatlik limit aşıldı ({hourly}/{user['rate_limit_hour']})",
                "hourly_used": hourly, "hourly_limit": user["rate_limit_hour"],
                "daily_used": daily,  "daily_limit": user["daily_limit"],
            }
        if daily >= user["daily_limit"]:
            return {
                "allowed": False,
                "reason": f"Günlük limit aşıldı ({daily}/{user['daily_limit']})",
                "hourly_used": hourly, "hourly_limit": user["rate_limit_hour"],
                "daily_used": daily,  "daily_limit": user["daily_limit"],
            }

        return {
            "allowed": True,
            "hourly_used": hourly, "hourly_limit": user["rate_limit_hour"],
            "daily_used": daily,   "daily_limit": user["daily_limit"],
        }

    def webchat_log_message(self, uid: str) -> None:
        """Rate log'a mesaj kaydı ekle ve msg_count artır."""
        with self._pool.tx() as conn:
            conn.execute(
                "INSERT INTO webchat_rate_log(user_id, ts) VALUES(?,?)",
                (uid, int(time.time()))
            )
            conn.execute(
                "UPDATE webchat_users SET msg_count=msg_count+1, last_seen=? WHERE id=?",
                (int(time.time()), uid)
            )
            # 48 saatten eski rate log girişlerini temizle (lazy cleanup)
            conn.execute(
                "DELETE FROM webchat_rate_log WHERE ts < ?",
                (int(time.time()) - 172800,)
            )
        self._cache.delete(f"wcu:{uid}")

    def webchat_delete_user(self, uid: str) -> bool:
        with self._pool.tx() as conn:
            conn.execute("DELETE FROM webchat_users WHERE id=?", (uid,))
            conn.execute("DELETE FROM webchat_rate_log WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM messages WHERE chat_id=?", (f"web:{uid}",))
            conn.execute("DELETE FROM messages_archive WHERE chat_id=?", (f"web:{uid}",))
        self._cache.delete(f"wcu:{uid}")
        return True

    def webchat_get_stats(self) -> Dict[str, Any]:
        conn = self._pool.get()
        row = conn.execute("""
            SELECT COUNT(*) total,
                   SUM(enabled) active,
                   SUM(msg_count) total_msgs
            FROM webchat_users
        """).fetchone()
        return {
            "total":      row[0] or 0,
            "active":     row[1] or 0,
            "disabled":   (row[0] or 0) - (row[1] or 0),
            "total_msgs": row[2] or 0,
        }

    # ── Context Manager ──────────────────────────────────────
    def __enter__(self): return self
    def __exit__(self, *_):
        rows = self._wbuf.drain()
        if rows:
            self._bulk_insert(rows)
        self._cache.clear()


# ═══════════════════════════════════════════════════════════════
#  BOT SÜREÇ MONİTÖRÜ
# ═══════════════════════════════════════════════════════════════

class BotMonitor:
    """WhatsApp bot sürecini izler, çöktüğünde yeniden başlatır."""

    def __init__(self, bot_js_path: str, cwd: str):
        self.bot_path = bot_js_path
        self.cwd      = cwd
        self._proc: Optional[subprocess.Popen] = None
        self._lock    = threading.Lock()
        self._restarts = 0
        self._enabled  = False
        self._start_time: Optional[float] = None

    def start(self) -> bool:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return False  # zaten çalışıyor

            node = subprocess.run(
                ["which","node"], capture_output=True, text=True
            ).stdout.strip()
            if not node:
                log.error("Node.js bulunamadı. Bot başlatılamıyor.")
                return False

            self._proc = subprocess.Popen(
                ["node", self.bot_path],
                cwd=self.cwd,
            )
            self._enabled    = True
            self._start_time = time.time()
            log.info(f"Bot başlatıldı: PID {self._proc.pid}")
            return True

    def stop(self) -> None:
        with self._lock:
            self._enabled = False
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None

    def status(self) -> Dict[str, Any]:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            uptime  = round(time.time() - self._start_time, 0) if (running and self._start_time) else 0
            return {
                "running":   running,
                "pid":       self._proc.pid if running else None,
                "restarts":  self._restarts,
                "uptime_s":  uptime,
            }

    def watch(self) -> None:
        """Arka planda süreç izle ve gerekirse yeniden başlat."""
        def _watcher():
            while True:
                time.sleep(BOT_HEALTH_INTERVAL)
                with self._lock:
                    if not self._enabled:
                        continue
                    if self._proc is None or self._proc.poll() is not None:
                        if self._restarts >= BOT_MAX_RESTARTS:
                            log.error(f"Bot max yeniden başlatma ({BOT_MAX_RESTARTS}) aşıldı. İzleme durdu.")
                            self._enabled = False
                            continue
                        log.warning(f"Bot çöktü. {BOT_RESTART_DELAY}s sonra yeniden başlatılıyor… (#{self._restarts+1})")
                time.sleep(BOT_RESTART_DELAY)
                with self._lock:
                    if self._enabled and (self._proc is None or self._proc.poll() is not None):
                        node = subprocess.run(
                            ["which","node"], capture_output=True, text=True
                        ).stdout.strip()
                        if node:
                            self._proc = subprocess.Popen(
                                ["node", self.bot_path], cwd=self.cwd
                            )
                            self._restarts += 1
                            self._start_time = time.time()
                            log.info(f"Bot yeniden başlatıldı: PID {self._proc.pid}")

        t = threading.Thread(target=_watcher, daemon=True, name="bot-watcher")
        t.start()


# ═══════════════════════════════════════════════════════════════
#  FLASK UYGULAMASI
# ═══════════════════════════════════════════════════════════════

# llama-server durumu (Flask app tanımından önce başlatılmalı)
_llm_proc: Optional[subprocess.Popen] = None
_llm_queue: queue.Queue = queue.Queue(maxsize=1000)
_llm_status = {"running": False, "pid": None, "port": 8080}
_llm_lock   = threading.Lock()

app  = Flask(__name__)
mm   = MemoryManager()
bot  = BotMonitor(str(APP_DIR/"whatsapp_bot.js"), str(APP_DIR))

# ── Request ID middleware — her isteğe benzersiz ID atayarak debug kolaylaştır ──
@app.before_request
def _inject_request_id():
    request.req_id = uuid_mod.uuid4().hex[:12]

@app.after_request
def _add_request_id_header(response):
    rid = getattr(request, 'req_id', None)
    if rid:
        response.headers['X-Request-ID'] = rid
    return response

# ── CORS desteği — chat_client.py farklı originden bağlanabilir ──
@app.after_request
def _add_cors_headers(response):
    origin = request.headers.get('Origin', '')
    if origin:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-API-Key'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def _handle_options(path):
    return '', 204

# ── İsteğe bağlı API anahtarı doğrulaması ───────────────────
@app.before_request
def _check_api_key():
    """PANEL_API_KEY ayarlıysa tüm /api/* yollarını koru."""
    if request.method == 'OPTIONS':
        return  # preflight'ları geçir
    if not PANEL_API_KEY:
        return  # auth devre dışı
    if not request.path.startswith('/api/'):
        return  # HTML paneli korumasız bırak
    key = request.headers.get('X-API-Key')
    if not key:
        key = request.args.get('api_key')
    if not key and request.is_json:
        key = (request.json or {}).get('api_key')
    if key != PANEL_API_KEY:
        return jsonify({"ok": False, "error": "Yetkisiz erişim"}), 401

# ─────────────────────────────────────────────────────────────
#  HTML PANEL
# ─────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>EfeMultiAIbot</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;600;700&family=IBM+Plex+Sans:wght@300;400;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.0.6/purify.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>
:root{
  --bg:#0a0a0d;--panel:#101014;--panel2:#141418;--panel3:#1a1a20;
  --border:#1e1e2a;--border2:#2a2a38;
  --amber:#f59e0b;--amber2:#fcd34d;--amber3:#78350f;
  --green:#10b981;--green2:#064e3b;
  --red:#ef4444;--red2:#7f1d1d;
  --blue:#3b82f6;--purple:#8b5cf6;--cyan:#06b6d4;
  --text:#d4d4e8;--dim:#4a4a60;--bright:#f0f0ff;--muted:#6b7280;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;background:var(--bg);color:var(--text);
  font-family:'IBM Plex Mono',monospace;font-size:13px;overflow:hidden}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}

/* ── Layout ── */
.app{display:grid;grid-template-rows:52px 28px 1fr;height:100vh}
.main{display:grid;grid-template-columns:360px 1fr;overflow:hidden}

/* ── Header ── */
header{display:flex;align-items:center;justify-content:space-between;
  padding:0 20px;background:var(--panel);border-bottom:1px solid var(--border);gap:16px}
.brand{display:flex;align-items:center;gap:10px;flex-shrink:0}
.brand-icon{font-size:22px}
.brand-name{font-size:13px;font-weight:700;letter-spacing:3px;color:var(--bright);text-transform:uppercase}
.brand-name em{color:var(--amber);font-style:normal}
.header-stats{display:flex;gap:16px;font-size:10px;color:var(--dim);letter-spacing:1px;flex:1;justify-content:center}
.hstat{display:flex;flex-direction:column;align-items:center;gap:2px}
.hstat-val{color:var(--amber);font-weight:600;font-size:12px}
.hstat-lbl{font-size:9px;text-transform:uppercase;letter-spacing:1px}
.hstatus{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--dim);flex-shrink:0}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim);transition:all .3s}
.dot.on{background:var(--green);box-shadow:0 0 8px var(--green);animation:blink 2s infinite}
.dot.off{background:var(--red)}
.dot.warn{background:var(--amber);animation:blink 1s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.4}}

/* ── Status bar ── */
.statusbar{display:flex;align-items:center;gap:16px;padding:0 16px;
  background:var(--panel2);border-bottom:1px solid var(--border);
  font-size:10px;color:var(--dim);letter-spacing:1px;overflow:hidden}
.sb-item{display:flex;align-items:center;gap:5px;white-space:nowrap}
.sb-dot{width:6px;height:6px;border-radius:50%;background:var(--dim)}
.sb-dot.on{background:var(--green)}
.sb-dot.off{background:var(--red)}

/* ── Left Panel ── */
.left{border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
.tabs{display:flex;border-bottom:1px solid var(--border);flex-shrink:0}
.tab{flex:1;padding:9px 0;text-align:center;cursor:pointer;font-size:10px;
  letter-spacing:2px;color:var(--dim);text-transform:uppercase;
  border-bottom:2px solid transparent;transition:all .2s}
.tab.active{color:var(--amber);border-bottom-color:var(--amber);background:rgba(245,158,11,.04)}
.tab:hover:not(.active){color:var(--text)}
.tab-content{flex:1;overflow-y:auto;padding:14px;display:none}
.tab-content.active{display:block}

/* ── Form ── */
.field{margin-bottom:12px}
.field label{display:block;font-size:10px;letter-spacing:2px;color:var(--dim);
  text-transform:uppercase;margin-bottom:5px}
.field input,.field select,.field textarea{
  width:100%;background:var(--panel2);border:1px solid var(--border);
  color:var(--text);padding:7px 10px;border-radius:6px;
  font-family:'IBM Plex Mono',monospace;font-size:12px;transition:border-color .2s}
.field input:focus,.field select:focus,.field textarea:focus{outline:none;border-color:var(--amber)}
.field input[type=range]{padding:0;height:4px;accent-color:var(--amber);cursor:pointer}
.range-row{display:flex;align-items:center;gap:10px}
.range-row input{flex:1}
.range-val{min-width:46px;text-align:right;color:var(--amber);font-size:12px}

/* ── Buttons ── */
.btn{width:100%;padding:9px;border-radius:6px;border:none;cursor:pointer;
  font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;
  letter-spacing:2px;text-transform:uppercase;transition:all .2s}
.btn-green{background:var(--green);color:#000}
.btn-green:hover:not(:disabled){background:#34d399;transform:translateY(-1px)}
.btn-red{background:var(--red);color:#fff}
.btn-red:hover:not(:disabled){background:#f87171}
.btn-amber{background:var(--amber);color:#000}
.btn-amber:hover:not(:disabled){background:var(--amber2)}
.btn-dim{background:var(--border);color:var(--dim);cursor:not-allowed}
.btn-outline{background:transparent;border:1px solid var(--border2);color:var(--text)}
.btn-outline:hover{border-color:var(--amber);color:var(--amber)}
.btn-row{display:flex;gap:8px;margin-bottom:12px}
.btn-row .btn{flex:1}

.section-title{font-size:9px;letter-spacing:3px;color:var(--amber);
  text-transform:uppercase;margin-bottom:10px;padding-bottom:5px;
  border-bottom:1px solid var(--border)}
.section-title:not(:first-child){margin-top:16px}

/* ── Log ── */
.log-box{background:var(--bg);border:1px solid var(--border);border-radius:6px;
  padding:10px;height:160px;overflow-y:auto;font-size:11px;line-height:1.6;color:var(--dim)}
.log-line{white-space:pre-wrap;word-break:break-all}
.log-line.ok{color:var(--green)}.log-line.err{color:var(--red)}
.log-line.info{color:var(--amber)}.log-line.warn{color:#fb923c}

/* ── Stats Cards ── */
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}
.stat-card{background:var(--panel2);border:1px solid var(--border);border-radius:8px;
  padding:10px 12px}
.stat-card-label{font-size:9px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;margin-bottom:4px}
.stat-card-value{font-size:16px;font-weight:700;color:var(--bright)}
.stat-card-sub{font-size:10px;color:var(--muted);margin-top:2px}
.stat-card.green .stat-card-value{color:var(--green)}
.stat-card.amber .stat-card-value{color:var(--amber)}
.stat-card.red   .stat-card-value{color:var(--red)}
.stat-card.blue  .stat-card-value{color:var(--blue)}

/* ── Progress bar ── */
.progress-wrap{margin-bottom:10px}
.progress-label{display:flex;justify-content:space-between;font-size:10px;
  color:var(--dim);margin-bottom:4px}
.progress-bar{height:4px;background:var(--border);border-radius:2px;overflow:hidden}
.progress-fill{height:100%;background:var(--green);border-radius:2px;transition:width .5s}
.progress-fill.amber{background:var(--amber)}
.progress-fill.red{background:var(--red)}

/* ── Chat area ── */
.right{display:flex;flex-direction:column;overflow:hidden}
.chat-header{padding:11px 16px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  background:var(--panel);flex-shrink:0;gap:10px}
.chat-title{font-size:11px;letter-spacing:3px;color:var(--dim);text-transform:uppercase}
.chat-actions{display:flex;gap:6px}
.icon-btn{background:none;border:1px solid var(--border);color:var(--dim);
  padding:4px 10px;border-radius:5px;cursor:pointer;font-size:10px;
  font-family:'IBM Plex Mono',monospace;letter-spacing:1px;transition:all .2s}
.icon-btn:hover{border-color:var(--amber);color:var(--amber)}
.icon-btn.red:hover{border-color:var(--red);color:var(--red)}
#messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px}
.msg{display:flex;gap:10px;animation:fadeUp .2s ease-out}
@keyframes fadeUp{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
.msg.user{flex-direction:row-reverse;align-self:flex-end;max-width:72%}
.msg.assistant{align-self:flex-start;max-width:84%}
.msg.system{align-self:center;max-width:90%;opacity:.7}
.avatar{width:26px;height:26px;border-radius:6px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;font-size:12px;margin-top:2px}
.msg.user      .avatar{background:linear-gradient(135deg,#f59e0b,#ef4444)}
.msg.assistant .avatar{background:linear-gradient(135deg,#10b981,#3b82f6)}
.msg.system    .avatar{background:linear-gradient(135deg,#8b5cf6,#06b6d4)}
.bubble{padding:9px 13px;border-radius:10px;font-size:13px;line-height:1.65;
  word-break:break-word;font-family:'IBM Plex Sans',sans-serif}
.msg.user .bubble{background:#1c1408;border:1px solid rgba(245,158,11,.15);
  border-top-right-radius:2px;color:var(--text)}
.msg.assistant .bubble{background:#080f0e;border:1px solid rgba(16,185,129,.12);
  border-top-left-radius:2px;color:var(--text)}
.msg.system .bubble{background:var(--panel2);border:1px solid var(--border2);
  font-size:11px;color:var(--muted);font-family:'IBM Plex Mono',monospace}
.bubble pre{background:var(--bg);border:1px solid var(--border);border-radius:6px;
  padding:10px;margin:8px 0;overflow-x:auto;font-size:11.5px;font-family:'IBM Plex Mono',monospace}
.bubble code{background:rgba(255,255,255,.06);padding:1px 5px;border-radius:3px;font-size:12px}
.bubble pre code{background:none;padding:0;display:block}
.bubble p{margin-bottom:8px}
.bubble p:last-child{margin-bottom:0}
.bubble ul, .bubble ol {margin: 8px 0 8px 18px}
.bubble table {border-collapse: collapse; margin: 10px 0; width: 100%; font-size: 13.5px}
.bubble th, .bubble td {border: 1px solid var(--border); padding: 6px 10px}
.cursor{display:inline-block;width:6px;height:12px;background:var(--green);
  border-radius:1px;vertical-align:text-bottom;animation:cblink .7s step-end infinite}
@keyframes cblink{0%,100%{opacity:1}50%{opacity:0}}
.typing-dots{display:flex;gap:4px;padding:10px 14px}
.typing-dots span{width:6px;height:6px;border-radius:50%;background:var(--green);
  animation:bounce .9s infinite}
.typing-dots span:nth-child(2){animation-delay:.15s}
.typing-dots span:nth-child(3){animation-delay:.3s}
@keyframes bounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}
.welcome{flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:8px;color:var(--dim);pointer-events:none}
.welcome-big{font-size:44px}
.welcome-title{font-size:15px;font-weight:700;letter-spacing:5px;color:var(--bright);text-transform:uppercase}
.welcome-sub{font-size:10px;letter-spacing:2px;text-transform:uppercase}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:14px;justify-content:center;
  pointer-events:all;max-width:520px}
.chip{background:var(--panel2);border:1px solid var(--border);border-radius:6px;
  padding:7px 13px;font-size:11px;cursor:pointer;transition:all .2s;color:var(--dim)}
.chip:hover{border-color:var(--amber);color:var(--amber);transform:translateY(-1px)}
.input-area{padding:12px 16px;background:var(--panel);border-top:1px solid var(--border);flex-shrink:0}
.input-wrap{display:flex;gap:10px;align-items:flex-end;background:var(--panel2);
  border:1px solid var(--border);border-radius:10px;padding:9px 13px;transition:border-color .2s}
.input-wrap:focus-within{border-color:var(--amber);box-shadow:0 0 0 2px rgba(245,158,11,.06)}
#prompt{flex:1;background:none;border:none;color:var(--text);font-family:'IBM Plex Sans',sans-serif;
  font-size:13.5px;line-height:1.6;resize:none;outline:none;max-height:140px;overflow-y:auto}
#prompt::placeholder{color:var(--dim)}
.send-btn{width:32px;height:32px;background:var(--amber);border:none;border-radius:7px;
  cursor:pointer;font-size:15px;transition:all .2s;flex-shrink:0;
  box-shadow:0 0 10px rgba(245,158,11,.25)}
.send-btn:hover:not(:disabled){background:var(--amber2);transform:translateY(-1px)}
.send-btn:disabled{opacity:.35;cursor:not-allowed;box-shadow:none}
.send-btn.stop{background:var(--red);box-shadow:0 0 10px rgba(239,68,68,.25)}
.hint{text-align:center;margin-top:5px;font-size:10px;color:var(--dim);letter-spacing:1px}
.token-bar{display:flex;gap:12px;font-size:10px;color:var(--dim);letter-spacing:1px;align-items:center}
.token-bar span{color:var(--amber)}
.file-row{display:flex;gap:6px}
.file-row input{flex:1}
.mini-btn{background:var(--panel2);border:1px solid var(--border);color:var(--text);
  padding:0 10px;border-radius:6px;cursor:pointer;font-size:10px;
  font-family:'IBM Plex Mono',monospace;transition:all .2s;white-space:nowrap;height:100%}
.mini-btn:hover{border-color:var(--amber);color:var(--amber)}
.contact-item{display:flex;justify-content:space-between;align-items:center;
  padding:9px;border:1px solid var(--border);border-radius:6px;margin-bottom:6px;background:var(--panel2)}
.contact-info{display:flex;flex-direction:column;gap:3px;overflow:hidden}
.contact-name{font-weight:600;color:var(--bright);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px}
.contact-sub{font-size:10px;color:var(--dim)}
.contact-badge{font-size:9px;color:var(--green);background:var(--green2);
  padding:1px 6px;border-radius:8px;border:1px solid rgba(16,185,129,.2)}
.switch{position:relative;display:inline-block;width:32px;height:18px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;
  background:var(--border);transition:.2s;border-radius:18px}
.slider:before{position:absolute;content:"";height:12px;width:12px;
  left:3px;bottom:3px;background:#fff;transition:.2s;border-radius:50%}
input:checked+.slider{background:var(--green)}
input:checked+.slider:before{transform:translateX(14px)}
.search-box{width:100%;padding:7px 10px;margin-bottom:10px;border-radius:6px;
  border:1px solid var(--border);background:var(--panel2);color:var(--text);
  font-family:inherit;font-size:12px}
.search-box:focus{outline:none;border-color:var(--amber)}
.model-item{background:var(--panel2);border:1px solid var(--border);border-radius:6px;
  padding:7px 10px;margin-bottom:5px;cursor:pointer;font-size:11px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:all .2s;color:var(--muted)}
.model-item:hover{border-color:var(--amber);color:var(--amber)}
.toast{position:fixed;bottom:24px;right:24px;background:var(--panel);border:1px solid var(--border2);
  color:var(--text);padding:10px 16px;border-radius:8px;font-size:12px;
  opacity:0;transform:translateY(8px);transition:all .3s;z-index:999;pointer-events:none}
.toast.show{opacity:1;transform:translateY(0)}
.toast.ok{border-color:var(--green);color:var(--green)}
.toast.err{border-color:var(--red);color:var(--red)}
</style>
</head>
<body>
<div class="app">

<!-- HEADER -->
<header>
  <div class="brand">
    <div class="brand-icon">🦙</div>
    <div class="brand-name">LLAMA<em>.</em>ULTIMATE</div>
  </div>
  <div class="header-stats" id="hdr-stats">
    <div class="hstat"><span class="hstat-val" id="hs-msgs">–</span><span class="hstat-lbl">Mesaj</span></div>
    <div class="hstat"><span class="hstat-val" id="hs-saved">–</span><span class="hstat-lbl">Tasarruf</span></div>
    <div class="hstat"><span class="hstat-val" id="hs-db">–</span><span class="hstat-lbl">DB</span></div>
    <div class="hstat"><span class="hstat-val" id="hs-cache">–</span><span class="hstat-lbl">Cache Hit</span></div>
    <div class="hstat"><span class="hstat-val" id="hs-bot">–</span><span class="hstat-lbl">Bot</span></div>
  </div>
  <div class="hstatus">
    <div class="dot off" id="status-dot"></div>
    <span id="status-text">Kapalı</span>
    &nbsp;|&nbsp;
    <div class="token-bar" id="token-bar"></div>
  </div>
</header>

<!-- STATUS BAR -->
<div class="statusbar" id="status-bar">
  <div class="sb-item"><div class="sb-dot off" id="sb-llm"></div><span id="sb-llm-txt">LLM: Kapalı</span></div>
  <div class="sb-item"><div class="sb-dot off" id="sb-bot"></div><span id="sb-bot-txt">Bot: Kapalı</span></div>
  <div class="sb-item" style="margin-left:auto;font-size:9px;color:var(--dim)" id="sb-time"></div>
</div>

<div class="main">
<!-- LEFT PANEL -->
<div class="left">
  <div class="tabs">
    <div class="tab active" data-tab="server"  onclick="switchTab(this,'server')">LLM</div>
    <div class="tab"        data-tab="params"  onclick="switchTab(this,'params')">PARAMS</div>
    <div class="tab"        data-tab="contacts"onclick="switchTab(this,'contacts')">KİŞİLER</div>
    <div class="tab"        data-tab="memory"  onclick="switchTab(this,'memory')">BELLEK</div>
    <div class="tab"        data-tab="rag"     onclick="switchTab(this,'rag')">RAG</div>
    <div class="tab"        data-tab="webchat" onclick="switchTab(this,'webchat')">WEB</div>
    <div class="tab"        data-tab="log"     onclick="switchTab(this,'log')">LOG</div>
  </div>

  <!-- SERVER TAB -->
  <div class="tab-content active" id="tab-server">
    <div class="section-title">Model</div>
    <div class="field">
      <label>Model Yolu (.gguf)</label>
      <div class="file-row">
        <input type="text" id="model-path" placeholder="Model seçin...">
        <button class="mini-btn" onclick="browseModels()" style="padding:0 12px">TARA</button>
      </div>
    </div>
    <div id="model-list" style="display:none;margin-bottom:10px">
      <div id="model-items"></div>
    </div>
    <div class="field">
      <label style="display:flex;align-items:center;gap:8px">
        Multimodal Projeksiyon (mmproj)
        <label class="switch" title="mmproj etkinleştir/devre dışı bırak">
          <input type="checkbox" id="mmproj-enabled" onchange="onMmprojToggle()">
          <span class="slider"></span>
        </label>
        <span id="mmproj-status" style="font-size:11px;color:var(--dim)">Kapalı</span>
      </label>
      <div id="mmproj-controls" style="display:none;margin-top:6px">
        <select id="mmproj-select" onchange="onMmprojChange()" style="width:100%;padding:7px 10px;border-radius:8px;border:1px solid var(--border);background:var(--card);color:var(--fg);font-size:12px">
          <option value="__manual__">✏️ Manuel yol gir…</option>
        </select>
        <div id="mmproj-manual-row" style="margin-top:6px">
          <div class="file-row">
            <input type="text" id="mmproj-manual" placeholder="mmproj dosya yolunu girin…">
            <button class="mini-btn" onclick="browseMmproj()" style="padding:0 12px">TARA</button>
          </div>
          <div id="mmproj-browse-list" style="display:none;margin-top:4px">
            <div id="mmproj-browse-items"></div>
          </div>
        </div>
      </div>
    </div>
    <div class="section-title">Ayarlar</div>
    <div class="field"><label>Port</label><input type="number" id="port" value="8080"></div>
    <div class="field"><label>Context (-c)</label>
      <div class="range-row">
        <input type="range" id="ctx" min="512" max="131072" step="512" value="32768"
          oninput="$('ctx-val').textContent=this.value">
        <span class="range-val" id="ctx-val">32768</span>
      </div>
    </div>
    <div class="field"><label>GPU Katmanları (-ngl)</label>
      <div class="range-row">
        <input type="range" id="ngl" min="0" max="99" value="99"
          oninput="$('ngl-val').textContent=this.value">
        <span class="range-val" id="ngl-val">99</span>
      </div>
    </div>
    <div class="field"><label>Thread (-t)</label>
      <div class="range-row">
        <input type="range" id="threads" min="1" max="32" value="8"
          oninput="$('threads-val').textContent=this.value">
        <span class="range-val" id="threads-val">8</span>
      </div>
    </div>
    <div class="field"><label>Parallel Slot (-np)</label>
      <div class="range-row">
        <input type="range" id="parallel" min="1" max="16" value="4"
          oninput="$('par-val').textContent=this.value">
        <span class="range-val" id="par-val">4</span>
      </div>
    </div>
    <div class="btn-row" style="margin-top:14px">
      <button class="btn btn-green" id="start-btn" onclick="startServer()">▶ BAŞLAT</button>
      <button class="btn btn-red btn-dim" id="stop-btn" onclick="stopServer()" disabled>■ DURDUR</button>
    </div>
    <div class="section-title">WhatsApp Bot</div>
    <div class="btn-row">
      <button class="btn btn-amber" id="bot-start-btn" onclick="startBot()">🤖 BOTU BAŞLAT</button>
      <button class="btn btn-outline" id="bot-stop-btn" onclick="stopBot()">■ DURDUR</button>
    </div>
  </div>

  <!-- PARAMS TAB -->
  <div class="tab-content" id="tab-params">
    <div class="section-title">Üretim Parametreleri</div>
    <div class="field"><label>Temperature</label>
      <div class="range-row">
        <input type="range" id="temperature" min="0" max="2" step="0.05" value="0.7"
          oninput="$('temp-val').textContent=parseFloat(this.value).toFixed(2)">
        <span class="range-val" id="temp-val">0.70</span>
      </div>
    </div>
    <div class="field"><label>Top P</label>
      <div class="range-row">
        <input type="range" id="top-p" min="0" max="1" step="0.05" value="0.8"
          oninput="$('topp-val').textContent=parseFloat(this.value).toFixed(2)">
        <span class="range-val" id="topp-val">0.80</span>
      </div>
    </div>
    <div class="field"><label>Top K</label>
      <div class="range-row">
        <input type="range" id="top-k" min="1" max="100" step="1" value="20"
          oninput="$('topk-val').textContent=this.value">
        <span class="range-val" id="topk-val">20</span>
      </div>
    </div>
    <div class="field"><label>Repeat Penalty</label>
      <div class="range-row">
        <input type="range" id="rep-pen" min="1" max="2" step="0.05" value="1.1"
          oninput="$('rep-val').textContent=parseFloat(this.value).toFixed(2)">
        <span class="range-val" id="rep-val">1.10</span>
      </div>
    </div>
    <div class="field"><label>Max Tokens</label>
      <div class="range-row">
        <input type="range" id="max-tokens" min="64" max="65536" step="64" value="16384"
          oninput="$('maxt-val').textContent=this.value">
        <span class="range-val" id="maxt-val">16384</span>
      </div>
    </div>
    <div class="section-title">Context Penceresi</div>
    <div class="field"><label>Token Bütçesi (bot için)</label>
      <div class="range-row">
        <input type="range" id="ctx-budget" min="512" max="16384" step="128" value="4096"
          oninput="$('budget-val').textContent=this.value">
        <span class="range-val" id="budget-val">4096</span>
      </div>
    </div>
    <div class="field"><label>Geçmiş Mesaj Limiti (bot)</label>
      <div class="range-row">
        <input type="range" id="hist-limit" min="2" max="30" step="1" value="10"
          oninput="$('hist-val').textContent=this.value">
        <span class="range-val" id="hist-val">10</span>
      </div>
    </div>
    <div class="section-title">System Prompt</div>
    <div class="field">
      <textarea id="sys-prompt" rows="5" style="resize:vertical">Yardımsever, saygılı ve son derece yetenekli bir yapay zeka asistanısın.</textarea>
    </div>
  </div>

  <!-- CONTACTS TAB -->
  <div class="tab-content" id="tab-contacts">
    <div class="section-title">Rehber & AI İzinleri</div>
    <input type="text" id="contact-search" class="search-box"
      placeholder="Kişi ara..." onkeyup="filterContacts()">
    <div id="contacts-list"></div>
  </div>

  <!-- MEMORY TAB -->
  <div class="tab-content" id="tab-memory">
    <div class="section-title">Veritabanı</div>
    <div class="stat-grid" id="mem-stats"></div>
    <div id="mem-progress"></div>
    <div class="section-title">İşlemler</div>
    <div class="btn-row">
      <button class="btn btn-amber" onclick="runMaintenance()">🔧 BAKIM</button>
      <button class="btn btn-outline" onclick="loadMemStats()">↺ YENİLE</button>
    </div>
    <div class="btn-row">
      <button class="btn btn-outline" onclick="purgeImages()">🗑 Görselleri Temizle</button>
      <button class="btn btn-outline" onclick="runSummarize()">✏ Özetle</button>
    </div>
    <div class="section-title">Cache</div>
    <div id="cache-stats" style="font-size:11px;color:var(--dim);line-height:1.8"></div>
  </div>

  <!-- RAG TAB -->
  <div class="tab-content" id="tab-rag">
    <div class="section-title">Vektör Bellek (RAG)</div>
    <div class="stat-grid" id="rag-stats-grid"></div>
    <div id="rag-progress"></div>

    <div class="section-title">Semantik Arama</div>
    <div style="background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:12px">
      <div class="field" style="margin-bottom:8px"><label>Sorgu</label>
        <div class="file-row">
          <input type="text" id="rag-query" placeholder="Geçmiş konuşmalarda ara…"
            onkeydown="if(event.key==='Enter')ragSearch()">
          <button class="mini-btn" onclick="ragSearch()" style="padding:0 12px">ARA</button>
        </div>
      </div>
      <div class="field" style="margin-bottom:8px"><label>Filtre: Chat ID (opsiyonel)</label>
        <input type="text" id="rag-filter-chat" placeholder="Tüm sohbetler">
      </div>
    </div>
    <div id="rag-results" style="max-height:300px;overflow-y:auto"></div>

    <div class="section-title">İşlemler</div>
    <div class="btn-row">
      <button class="btn btn-amber" onclick="ragReindex()">📦 YENİDEN İNDEKSLE</button>
      <button class="btn btn-outline" onclick="loadRagStats()">↺ YENİLE</button>
    </div>
    <div class="btn-row">
      <button class="btn btn-outline" onclick="ragReinit()">🔌 Embedding Yenile</button>
      <button class="btn btn-outline" style="color:var(--red)" onclick="ragReset()">🗑 Sıfırla</button>
    </div>
  </div>

  <!-- WEBCHAT TAB -->
  <div class="tab-content" id="tab-webchat">
    <div class="section-title">Web Kullanıcıları</div>
    <div class="stat-grid" id="wc-stats-grid"></div>

    <div class="section-title">Varsayılan Limitler</div>
    <div style="background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:12px">
      <div class="field" style="margin-bottom:8px"><label>Saatlik Mesaj Limiti</label>
        <div class="range-row">
          <input type="range" id="wc-def-hour" min="1" max="200" step="1" value="20"
            oninput="$('wc-def-hour-val').textContent=this.value">
          <span class="range-val" id="wc-def-hour-val">20</span>
        </div>
      </div>
      <div class="field" style="margin-bottom:8px"><label>Günlük Mesaj Limiti</label>
        <div class="range-row">
          <input type="range" id="wc-def-daily" min="1" max="1000" step="5" value="100"
            oninput="$('wc-def-daily-val').textContent=this.value">
          <span class="range-val" id="wc-def-daily-val">100</span>
        </div>
      </div>
      <div class="field" style="margin-bottom:8px"><label>Max Token (yanıt)</label>
        <div class="range-row">
          <input type="range" id="wc-def-tokens" min="64" max="8192" step="64" value="2048"
            oninput="$('wc-def-tokens-val').textContent=this.value">
          <span class="range-val" id="wc-def-tokens-val">2048</span>
        </div>
      </div>
      <div class="field" style="margin-bottom:8px"><label>Varsayılan System Prompt</label>
        <textarea id="wc-def-sysprompt" rows="3" style="resize:vertical;width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 9px;border-radius:6px;font-family:inherit;font-size:11px"></textarea>
      </div>
      <button class="btn btn-amber" onclick="applyDefaultLimits()" style="margin-top:4px">↓ TÜM KULLANICILARA UYGULA</button>
    </div>

    <div class="section-title">Kullanıcılar <span id="wc-user-count" style="color:var(--dim)">(0)</span></div>
    <div class="btn-row">
      <button class="btn btn-outline" style="font-size:9px" onclick="wcEnableAll()">✓ HEPSİNİ AKTİF</button>
      <button class="btn btn-outline" style="font-size:9px" onclick="wcDisableAll()">✗ HEPSİNİ KAPAT</button>
      <button class="btn btn-outline" style="font-size:9px" onclick="loadWebchatUsers()">↺</button>
    </div>
    <div id="wc-user-list"></div>

    <!-- Per-user edit modal (inline) -->
    <div id="wc-edit-panel" style="display:none;background:var(--panel2);border:1px solid var(--amber);border-radius:8px;padding:12px;margin-top:8px">
      <div class="section-title" id="wc-edit-title">Kullanıcı Düzenle</div>
      <input type="hidden" id="wc-edit-uid">
      <div class="field" style="margin-bottom:8px"><label>Kullanıcı Adı</label>
        <input type="text" id="wc-edit-name">
      </div>
      <div class="field" style="margin-bottom:8px"><label>Saatlik Limit</label>
        <div class="range-row">
          <input type="range" id="wc-edit-hour" min="1" max="200" step="1" value="20"
            oninput="$('wc-edit-hour-val').textContent=this.value">
          <span class="range-val" id="wc-edit-hour-val">20</span>
        </div>
      </div>
      <div class="field" style="margin-bottom:8px"><label>Günlük Limit</label>
        <div class="range-row">
          <input type="range" id="wc-edit-daily" min="1" max="1000" step="5" value="100"
            oninput="$('wc-edit-daily-val').textContent=this.value">
          <span class="range-val" id="wc-edit-daily-val">100</span>
        </div>
      </div>
      <div class="field" style="margin-bottom:8px"><label>Max Token</label>
        <div class="range-row">
          <input type="range" id="wc-edit-tokens" min="64" max="8192" step="64" value="2048"
            oninput="$('wc-edit-tokens-val').textContent=this.value">
          <span class="range-val" id="wc-edit-tokens-val">2048</span>
        </div>
      </div>
      <div class="field" style="margin-bottom:8px"><label>System Prompt (boş = varsayılan)</label>
        <textarea id="wc-edit-sysprompt" rows="3" style="resize:vertical;width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 9px;border-radius:6px;font-family:inherit;font-size:11px"></textarea>
      </div>
      <div class="btn-row" style="margin-top:8px">
        <button class="btn btn-green" onclick="wcSaveEdit()">KAYDET</button>
        <button class="btn btn-red" onclick="wcDeleteUser()">SİL</button>
        <button class="btn btn-outline" onclick="$('wc-edit-panel').style.display='none'">İPTAL</button>
      </div>
    </div>
  </div>

  <!-- LOG TAB -->
  <div class="tab-content" id="tab-log">
    <div class="section-title">Sunucu Logu</div>
    <div class="log-box" id="log-box"></div>
    <div style="margin-top:8px">
      <button class="btn btn-outline" onclick="$('log-box').innerHTML=''">LOGU TEMİZLE</button>
    </div>
  </div>
</div>

<!-- RIGHT: CHAT -->
<div class="right">
  <div class="chat-header">
    <span class="chat-title">💬 Sohbet</span>
    <div class="chat-actions">
      <button class="icon-btn" onclick="summarizeCurrent()">✏ Özetle</button>
      <button class="icon-btn red" onclick="clearChat()">✕ Temizle</button>
    </div>
  </div>
  <div id="messages">
    <div class="welcome" id="welcome">
      <div class="welcome-big">🤖</div>
      <div class="welcome-title">EfeMultiAIbot</div>
      <div class="welcome-sub">Sunucuyu başlatıp sohbet et</div>
      <div class="chips">
        <div class="chip" onclick="quickSend('Merhaba! Kendini kısaca tanıt.')">Kendini tanıt</div>
        <div class="chip" onclick="quickSend('Python\'da async/await örneği göster.')">Async Python</div>
        <div class="chip" onclick="quickSend('Transformer mimarisi nedir? Özetle.')">Transformer</div>
        <div class="chip" onclick="quickSend('SQLite performans ipuçları neler?')">SQLite İpuçları</div>
        <div class="chip" onclick="quickSend('Bana Türkçe bir fıkra anlat.')">Fıkra</div>
        <div class="chip" onclick="quickSend('Özet bellek ne işe yarar? Açıkla.')">Özet Bellek</div>
      </div>
    </div>
  </div>
  <div class="input-area">
    <div class="input-wrap">
      <textarea id="prompt" rows="1"
        placeholder="Mesaj yaz… (Enter gönder · Shift+Enter yeni satır · Esc durdur)"></textarea>
      <button class="send-btn" id="send-btn" onclick="handleSend()">➤</button>
    </div>
    <div class="hint">ENTER → Gönder &nbsp;·&nbsp; SHIFT+ENTER → Yeni Satır &nbsp;·&nbsp; ESC → Durdur</div>
  </div>
</div>
</div>
</div>
<div class="toast" id="toast"></div>

<script>
// ── helpers ──
const $  = id => document.getElementById(id);
const qs = sel => document.querySelector(sel);
let isGen=false, abortCtrl=null, history=[];

function toast(msg, type='ok') {
  const t = $('toast');
  t.textContent = msg;
  t.className = `toast show ${type}`;
  setTimeout(() => t.className='toast', 2800);
}

function switchTab(el, name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  $('tab-'+name).classList.add('active');
  if (name==='contacts') loadContacts();
  if (name==='memory')   loadMemStats();
  if (name==='rag')      loadRagStats();
  if (name==='webchat')  loadWebchatUsers();
}

// ── LLM Server ──
function getMmprojValue() {
  if (!$('mmproj-enabled').checked) return '';
  const sel = $('mmproj-select').value;
  if (sel === '__manual__') return ($('mmproj-manual').value || '').trim();
  return sel;
}
function onMmprojToggle() {
  const on = $('mmproj-enabled').checked;
  $('mmproj-controls').style.display = on ? 'block' : 'none';
  $('mmproj-status').textContent = on ? 'Açık' : 'Kapalı';
  $('mmproj-status').style.color = on ? 'var(--green)' : 'var(--dim)';
  if (on) onMmprojChange();
}
function onMmprojChange() {
  const isManual = $('mmproj-select').value === '__manual__';
  $('mmproj-manual-row').style.display = isManual ? 'block' : 'none';
}
async function browseMmproj() {
  const r = await fetch('/api/mmproj?all=1');
  const d = await r.json();
  const list = $('mmproj-browse-list'), items = $('mmproj-browse-items');
  items.innerHTML='';
  if (!d.projectors || !d.projectors.length) { items.innerHTML='<div style="color:var(--dim);font-size:11px">mmproj bulunamadı</div>'; }
  else {
    d.projectors.forEach(p => {
      const el = document.createElement('div');
      el.className='model-item'; el.title=p;
      el.textContent=p.split('/').pop();
      el.onclick=()=>{ $('mmproj-manual').value=p; list.style.display='none'; };
      items.appendChild(el);
    });
  }
  list.style.display = list.style.display==='none' ? 'block' : 'none';
}
async function startServer() {
  const cfg = {
    model: $('model-path').value, port: $('port').value,
    ctx: $('ctx').value, ngl: $('ngl').value,
    threads: $('threads').value, parallel: $('parallel').value,
    mmproj: getMmprojValue(),
  };
  $('start-btn').disabled=true; $('start-btn').textContent='⏳ BAŞLATILIYOR…';
  const r = await fetch('/api/server/start',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  const d = await r.json();
  if (d.ok) { updateLLMStatus(true); switchTab(qs('[data-tab="log"]'),'log'); toast('LLM sunucusu başlatıldı'); }
  else { $('start-btn').disabled=false; $('start-btn').textContent='▶ BAŞLAT'; toast('Hata: '+d.error,'err'); }
}
async function stopServer() {
  await fetch('/api/server/stop',{method:'POST'});
  updateLLMStatus(false); toast('LLM durduruldu','err');
}
function updateLLMStatus(on) {
  $('status-dot').className='dot '+(on?'on':'off');
  $('status-text').textContent = on?'LLM Çalışıyor':'Kapalı';
  $('start-btn').disabled=on; $('start-btn').className='btn '+(on?'btn-dim':'btn-green');
  $('start-btn').textContent='▶ BAŞLAT';
  $('stop-btn').disabled=!on; $('stop-btn').className='btn btn-red'+(on?'':' btn-dim');
  $('sb-llm').className='sb-dot '+(on?'on':'off');
  $('sb-llm-txt').textContent='LLM: '+(on?'Çalışıyor':'Kapalı');
}

// ── Bot Control ──
async function startBot() {
  const r = await fetch('/api/bot/start',{method:'POST'});
  const d = await r.json();
  if (d.ok) toast('Bot başlatıldı 🤖'); else toast('Bot hatası: '+d.error,'err');
}
async function stopBot() {
  await fetch('/api/bot/stop',{method:'POST'});
  toast('Bot durduruldu','err');
}

// ── Log streaming ──
const logEs = new EventSource('/api/logs');
logEs.onmessage = e => {
  if (e.data === '.') return;
  const box = $('log-box');
  const d = document.createElement('div');
  d.className='log-line';
  const t = e.data;
  if (t.includes('[ERR]')||t.includes('error')||t.includes('ERROR')) d.classList.add('err');
  else if (t.includes('[INFO]')||t.includes('main:')||t.includes('init:')) d.classList.add('info');
  else if (t.includes('[OK]')||t.includes('loaded')||t.includes('listening')) d.classList.add('ok');
  else if (t.includes('[WARN]')||t.includes('warn')) d.classList.add('warn');
  d.textContent = t;
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  if (box.children.length > 500) box.removeChild(box.firstChild);
};

// ── Stats SSE ──
const statsEs = new EventSource('/api/stats/stream');
statsEs.onmessage = e => {
  try {
    const d = JSON.parse(e.data);
    if (!d || d==='ping') return;
    // Header stats
    $('hs-msgs').textContent  = (d.messages?.active||0).toLocaleString();
    $('hs-saved').textContent = (d.messages?.saved_pct||0)+'%';
    $('hs-db').textContent    = (d.db?.size_mb||0)+'MB';
    $('hs-cache').textContent = (d.cache?.hit_rate||0)+'%';
    // Bot status
    $('hs-bot').textContent  = d.bot?.running ? '🟢' : '🔴';
    $('sb-bot').className    = 'sb-dot '+(d.bot?.running?'on':'off');
    $('sb-bot-txt').textContent = 'Bot: '+(d.bot?.running?('Çalışıyor PID '+d.bot.pid):'Kapalı');
    // LLM
    if (d.llm?.running !== undefined) updateLLMStatus(d.llm.running);
  } catch{}
};

// ── Clock ──
setInterval(()=>{ $('sb-time').textContent = new Date().toLocaleTimeString('tr-TR'); },1000);

// ── Model browser ──
async function refreshMmproj(modelPath) {
  const sel = $('mmproj-select');
  sel.innerHTML='<option value="__manual__">✏️ Manuel yol gir…</option>';
  if (!modelPath) return;
  try {
    const r = await fetch('/api/mmproj?model='+encodeURIComponent(modelPath));
    const d = await r.json();
    if (d.projectors && d.projectors.length) {
      d.projectors.forEach(p => {
        const o = document.createElement('option');
        o.value = p; o.textContent = p.split('/').pop();
        sel.insertBefore(o, sel.firstElementChild);
      });
      sel.selectedIndex = 0;
      onMmprojChange();
    }
  } catch(e) {}
}
async function browseModels() {
  const r = await fetch('/api/models');
  const d = await r.json();
  const list = $('model-list'), items = $('model-items');
  items.innerHTML='';
  if (!d.models.length) { items.innerHTML='<div style="color:var(--dim);font-size:11px">Model bulunamadı</div>'; }
  else {
    d.models.forEach(m => {
      const el = document.createElement('div');
      el.className='model-item'; el.title=m;
      el.textContent=m.split('/').pop();
      el.onclick=()=>{ $('model-path').value=m; list.style.display='none'; refreshMmproj(m); };
      items.appendChild(el);
    });
  }
  list.style.display = list.style.display==='none' ? 'block' : 'none';
}

// ── Contacts ──
let allContacts=[];
async function loadContacts() {
  const el = $('contacts-list');
  el.innerHTML='<div class="log-line info" style="padding:8px">Yükleniyor…</div>';
  const r = await fetch('/api/contacts');
  const d = await r.json();
  if (!d.ok) { el.innerHTML=`<div class="log-line err">Hata: ${d.error}</div>`; return; }
  allContacts = d.contacts;
  renderContacts(allContacts);
}
function renderContacts(list) {
  const el = $('contacts-list'); el.innerHTML='';
  if (!list.length) { el.innerHTML='<div class="log-line" style="padding:8px">Kişi bulunamadı</div>'; return; }
  list.forEach(c => {
    const item=document.createElement('div'); item.className='contact-item';
    const ai = c.ai_enabled;
    item.innerHTML=`
      <div class="contact-info">
        <div style="display:flex;align-items:center;gap:6px">
          <span class="contact-name">${c.name||c.pushname||'İsimsiz'}</span>
          ${ai?'<span class="contact-badge">AI Aktif</span>':''}
        </div>
        <div class="contact-sub">${c.pushname?'~'+c.pushname:''} ${c.msg_count?'· '+c.msg_count+' msg':''}</div>
      </div>
      <label class="switch" title="AI bu kişiye cevap verebilir mi?">
        <input type="checkbox" ${ai?'checked':''} onchange="toggleContact('${c.id}',this.checked)">
        <span class="slider"></span>
      </label>`;
    el.appendChild(item);
  });
}
function filterContacts() {
  const q=$('contact-search').value.toLowerCase();
  if (!q) { renderContacts(allContacts); return; }
  renderContacts(allContacts.filter(c=>
    (c.name&&c.name.toLowerCase().includes(q))||
    (c.pushname&&c.pushname.toLowerCase().includes(q))));
}
async function toggleContact(id, enabled) {
  const r=await fetch('/api/contacts/toggle',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({id,enabled:enabled?1:0})});
  const d=await r.json();
  if (!d.ok) { toast('Hata: '+d.error,'err'); loadContacts(); }
  else { const t=allContacts.find(c=>c.id===id); if(t) t.ai_enabled=enabled?1:0; }
}

// ── Memory Stats ──
async function loadMemStats() {
  const r = await fetch('/api/db/stats');
  const d = await r.json();
  const g = $('mem-stats');
  const msgs = d.messages||{}, imgs=d.images||{}, cache=d.cache||{}, db=d.db||{};
  g.innerHTML=`
    <div class="stat-card green"><div class="stat-card-label">Aktif Mesaj</div>
      <div class="stat-card-value">${(msgs.active||0).toLocaleString()}</div>
      <div class="stat-card-sub">Arşiv: ${(msgs.archived||0).toLocaleString()}</div></div>
    <div class="stat-card amber"><div class="stat-card-label">Tasarruf</div>
      <div class="stat-card-value">%${msgs.saved_pct||0}</div>
      <div class="stat-card-sub">Sıkıştırma aktif</div></div>
    <div class="stat-card blue"><div class="stat-card-label">DB Boyutu</div>
      <div class="stat-card-value">${db.size_mb||0}MB</div>
      <div class="stat-card-sub">${msgs.chats||0} sohbet</div></div>
    <div class="stat-card"><div class="stat-card-label">Görsel</div>
      <div class="stat-card-value">${imgs.count||0}</div>
      <div class="stat-card-sub">%${imgs.saved_pct||0} sıkıştırma</div></div>`;
  const p=$('mem-progress');
  const compPct=msgs.compression_pct||100;
  const fillCls = compPct < 30 ? '' : compPct < 60 ? 'amber' : 'red';
  p.innerHTML=`
    <div class="progress-wrap">
      <div class="progress-label"><span>Sıkıştırma Oranı</span><span>%${compPct} depolama</span></div>
      <div class="progress-bar"><div class="progress-fill ${fillCls}" style="width:${compPct}%"></div></div>
    </div>
    <div class="progress-wrap">
      <div class="progress-label"><span>Cache Hit Rate</span><span>%${cache.hit_rate||0}</span></div>
      <div class="progress-bar"><div class="progress-fill" style="width:${cache.hit_rate||0}%"></div></div>
    </div>`;
  $('cache-stats').innerHTML=`
    RAM cache: ${cache.size||0} / ${cache.maxsize||0} giriş<br>
    Hit: ${cache.hits||0} · Miss: ${cache.misses||0} · Evict: ${cache.evictions||0}<br>
    Tampon: ${d.buffer?.pending||0} bekleyen yazma`;
}
async function runMaintenance() {
  toast('Bakım başlıyor…');
  const r=await fetch('/api/db/maintenance',{method:'POST'});
  const d=await r.json();
  if (d.ok) { toast(`Bakım tamam · ${d.report.freed_mb}MB serbest bırakıldı`); loadMemStats(); }
  else toast('Bakım hatası','err');
}
async function purgeImages() {
  const r=await fetch('/api/db/images/purge',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({days:7})});
  const d=await r.json();
  toast(`${d.deleted} görsel silindi`);
  loadMemStats();
}
async function runSummarize() {
  const r=await fetch('/api/db/summarize',{method:'POST'});
  const d=await r.json();
  toast(d.ok?`Özetlendi: ${d.chats_processed} sohbet`:'Özetleme başarısız','ok');
  loadMemStats();
}

// ── RAG Yönetimi ──
async function loadRagStats() {
  try {
    const r = await fetch('/api/rag/stats');
    const d = await r.json();
    const g = $('rag-stats-grid');
    if (!d.ok && !d.available) {
      g.innerHTML=`<div class="stat-card red"><div class="stat-card-label">Durum</div>
        <div class="stat-card-value">KAPALI</div>
        <div class="stat-card-sub">chromadb yüklü değil veya devre dışı</div></div>`;
      $('rag-progress').innerHTML='';
      return;
    }
    const avail = d.available;
    const indexed = d.indexed||0;
    const queries = d.queries||0;
    const errors = d.errors||0;
    const diskMb = d.disk_mb||0;
    const embedType = d.embed_type||'none';
    const embedLabel = embedType==='llm'?'LLM Lokal':embedType==='default'?'MiniLM (ONNX)':'Yok';

    g.innerHTML=`
      <div class="stat-card ${avail?'green':'red'}"><div class="stat-card-label">Durum</div>
        <div class="stat-card-value">${avail?'AKTİF':'KAPALI'}</div>
        <div class="stat-card-sub">${embedLabel}</div></div>
      <div class="stat-card amber"><div class="stat-card-label">Vektörler</div>
        <div class="stat-card-value">${indexed.toLocaleString()}</div>
        <div class="stat-card-sub">${diskMb} MB disk</div></div>
      <div class="stat-card blue"><div class="stat-card-label">Sorgular</div>
        <div class="stat-card-value">${queries}</div>
        <div class="stat-card-sub">${errors} hata</div></div>`;
  } catch(e) {
    $('rag-stats-grid').innerHTML=`<div class="stat-card red"><div class="stat-card-label">Hata</div>
      <div class="stat-card-value">!</div><div class="stat-card-sub">${e.message}</div></div>`;
  }
}

async function ragSearch() {
  const q = $('rag-query').value.trim();
  if (!q) { toast('Sorgu boş','err'); return; }
  const chatFilter = $('rag-filter-chat').value.trim();
  const url = `/api/rag/search?q=${encodeURIComponent(q)}&k=10${chatFilter?'&chat_id='+encodeURIComponent(chatFilter):''}`;
  try {
    const r = await fetch(url);
    const d = await r.json();
    const el = $('rag-results');
    if (!d.ok) { el.innerHTML=`<div style="color:var(--red)">${d.error}</div>`; return; }
    if (!d.results||d.results.length===0) {
      el.innerHTML='<div style="color:var(--dim);padding:8px">Sonuç bulunamadı.</div>';
      return;
    }
    el.innerHTML = d.results.map((r,i) => {
      const ts = r.ts ? new Date(r.ts*1000).toLocaleString('tr-TR') : '?';
      const role = r.role==='user'?'👤':'🤖';
      const score = (r.score*100).toFixed(1);
      const badge = r.score>=0.7?'green':r.score>=0.4?'amber':'';
      return `<div style="background:var(--panel2);border:1px solid var(--border);border-radius:6px;padding:8px;margin-bottom:6px">
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--dim);margin-bottom:4px">
          <span>${role} ${r.chat_id?.substring(0,20)||'?'}</span>
          <span>${ts}</span>
          <span class="${badge}" style="font-weight:600">%${score}</span>
        </div>
        <div style="font-size:11px;line-height:1.5;color:var(--text)">${r.content?.substring(0,300)||''}${r.content?.length>300?'…':''}</div>
      </div>`;
    }).join('');
  } catch(e) { toast('Arama hatası: '+e.message,'err'); }
}

async function ragReindex() {
  if (!confirm('Tüm mesajlar yeniden indekslenecek. Bu uzun sürebilir. Devam?')) return;
  toast('Reindex başlıyor…');
  try {
    const r = await fetch('/api/rag/reindex',{method:'POST'});
    const d = await r.json();
    if (d.ok) {
      toast(`Reindex tamam: L2=${d.l2_indexed} + L3=${d.l3_indexed} → ${d.total_vectors} vektör`);
    } else {
      toast('Reindex hatası: '+(d.error||'?'),'err');
    }
    loadRagStats();
  } catch(e) { toast('Reindex hatası: '+e.message,'err'); }
}

async function ragReinit() {
  try {
    const port = $('port')?.value || 8080;
    const r = await fetch('/api/rag/reinit',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({port:parseInt(port)})});
    const d = await r.json();
    toast(d.ok?'RAG embedding yenilendi':'RAG embedding başarısız','ok');
    loadRagStats();
  } catch(e) { toast('Hata: '+e.message,'err'); }
}

async function ragReset() {
  if (!confirm('RAG vektör deposu tamamen sıfırlanacak. Emin misiniz?')) return;
  try {
    const r = await fetch('/api/rag/reset',{method:'DELETE'});
    const d = await r.json();
    toast(d.ok?'RAG sıfırlandı':'Sıfırlama hatası','ok');
    loadRagStats();
  } catch(e) { toast('Hata: '+e.message,'err'); }
}

// ── Web Chat Yönetimi ──
let wcUsers = [];
async function loadWebchatUsers() {
  const [sr, ur] = await Promise.all([
    fetch('/api/webchat/stats').then(r=>r.json()).catch(()=>({})),
    fetch('/api/webchat/users').then(r=>r.json()).catch(()=>({users:[]}))
  ]);
  // Stats cards
  const s = sr.stats || {};
  $('wc-stats-grid').innerHTML=`
    <div class="stat-card blue"><div class="stat-card-label">Toplam</div>
      <div class="stat-card-value">${s.total||0}</div></div>
    <div class="stat-card green"><div class="stat-card-label">Aktif</div>
      <div class="stat-card-value">${s.active||0}</div></div>
    <div class="stat-card red"><div class="stat-card-label">Kapalı</div>
      <div class="stat-card-value">${s.disabled||0}</div></div>
    <div class="stat-card amber"><div class="stat-card-label">Toplam Mesaj</div>
      <div class="stat-card-value">${(s.total_msgs||0).toLocaleString()}</div></div>`;

  wcUsers = ur.users || [];
  $('wc-user-count').textContent = `(${wcUsers.length})`;
  renderWcUsers(wcUsers);
}
function renderWcUsers(list) {
  const el = $('wc-user-list'); el.innerHTML='';
  if (!list.length) {
    el.innerHTML='<div style="color:var(--dim);font-size:11px;padding:8px">Henüz kullanıcı yok</div>';
    return;
  }
  list.forEach(u => {
    const ago = u.last_seen ? Math.round((Date.now()/1000-u.last_seen)/60) + 'd önce' : 'hiç';
    const item = document.createElement('div');
    item.className = 'contact-item';
    item.style.cursor = 'pointer';
    item.innerHTML=`
      <div class="contact-info" onclick="wcOpenEdit('${u.id}')">
        <div style="display:flex;align-items:center;gap:6px">
          <span class="contact-name">${u.username}</span>
          ${u.enabled?'<span class="contact-badge">Aktif</span>':'<span class="contact-badge" style="color:var(--red);background:var(--red2);border-color:rgba(239,68,68,.2)">Kapalı</span>'}
        </div>
        <div class="contact-sub">💬 ${u.msg_count} msg · ${ago} · ${u.ip||'?'}</div>
        <div class="contact-sub">⏱ ${u.rate_limit_hour}/sa · 📅 ${u.daily_limit}/gün · 🔤 ${u.max_tokens} tok</div>
      </div>
      <label class="switch" title="Kullanıcıyı aç/kapa" onclick="event.stopPropagation()">
        <input type="checkbox" ${u.enabled?'checked':''} onchange="wcToggleUser('${u.id}',this.checked)">
        <span class="slider"></span>
      </label>`;
    el.appendChild(item);
  });
}
function wcOpenEdit(uid) {
  const u = wcUsers.find(x=>x.id===uid); if(!u) return;
  $('wc-edit-uid').value = uid;
  $('wc-edit-title').textContent = `Düzenle: ${u.username}`;
  $('wc-edit-name').value = u.username;
  const h=$('wc-edit-hour'); h.value=u.rate_limit_hour; $('wc-edit-hour-val').textContent=u.rate_limit_hour;
  const d=$('wc-edit-daily'); d.value=u.daily_limit; $('wc-edit-daily-val').textContent=u.daily_limit;
  const t=$('wc-edit-tokens'); t.value=u.max_tokens; $('wc-edit-tokens-val').textContent=u.max_tokens;
  $('wc-edit-sysprompt').value = u.sys_prompt||'';
  $('wc-edit-panel').style.display='block';
  $('wc-edit-panel').scrollIntoView({behavior:'smooth'});
}
async function wcSaveEdit() {
  const uid = $('wc-edit-uid').value;
  const body = {
    username: $('wc-edit-name').value,
    rate_limit_hour: parseInt($('wc-edit-hour').value),
    daily_limit: parseInt($('wc-edit-daily').value),
    max_tokens: parseInt($('wc-edit-tokens').value),
    sys_prompt: $('wc-edit-sysprompt').value,
  };
  const r = await fetch(`/api/webchat/users/${uid}`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
  });
  const d = await r.json();
  if(d.ok) { toast('Kaydedildi','ok'); $('wc-edit-panel').style.display='none'; loadWebchatUsers(); }
  else toast('Hata: '+(d.error||'?'),'err');
}
async function wcDeleteUser() {
  const uid = $('wc-edit-uid').value;
  if(!confirm('Bu kullanıcıyı ve tüm sohbet geçmişini sil?')) return;
  const r = await fetch(`/api/webchat/users/${uid}`, {method:'DELETE'});
  const d = await r.json();
  if(d.ok) { toast('Silindi','ok'); $('wc-edit-panel').style.display='none'; loadWebchatUsers(); }
  else toast('Hata','err');
}
async function wcToggleUser(uid, enabled) {
  await fetch(`/api/webchat/users/${uid}`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled: enabled ? 1 : 0})
  });
  const u = wcUsers.find(x=>x.id===uid);
  if(u) u.enabled = enabled ? 1 : 0;
  toast(enabled ? 'Kullanıcı aktif edildi' : 'Kullanıcı kapatıldı');
  loadWebchatUsers();
}
async function wcEnableAll() {
  await Promise.all(wcUsers.map(u=>
    fetch(`/api/webchat/users/${u.id}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled:1})
    })
  ));
  toast(`${wcUsers.length} kullanıcı aktif edildi`,'ok');
  loadWebchatUsers();
}
async function wcDisableAll() {
  if(!confirm(`${wcUsers.length} kullanıcının tamamını kapat?`)) return;
  await Promise.all(wcUsers.map(u=>
    fetch(`/api/webchat/users/${u.id}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled:0})
    })
  ));
  toast(`${wcUsers.length} kullanıcı kapatıldı`);
  loadWebchatUsers();
}
async function applyDefaultLimits() {
  if(!wcUsers.length) { toast('Kullanıcı yok','err'); return; }
  if(!confirm(`${wcUsers.length} kullanıcıya varsayılan limitler uygulanacak. Devam?`)) return;
  const body = {
    rate_limit_hour: parseInt($('wc-def-hour').value),
    daily_limit: parseInt($('wc-def-daily').value),
    max_tokens: parseInt($('wc-def-tokens').value),
    sys_prompt: $('wc-def-sysprompt').value,
  };
  await Promise.all(wcUsers.map(u=>
    fetch(`/api/webchat/users/${u.id}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)
    })
  ));
  toast('Limitler uygulandı','ok');
  loadWebchatUsers();
}


function handleSend() {
  if (isGen) { abortCtrl?.abort(); return; }
  const ta=$('prompt'), text=ta.value.trim();
  if (!text) return;
  ta.value=''; ta.style.height='auto';
  sendMsg(text);
}
function quickSend(t){ $('prompt').value=t; handleSend(); }
function summarizeCurrent() {
  if (!history.length) return;
  const t = history.map(m=>m.role.toUpperCase()+': '+m.content).join('\n');
  sendMsg('Şu sohbeti 3 cümleyle özetle:\n\n'+t.substring(0,3000));
}

async function sendMsg(text) {
  hideWelcome();
  history.push({role:'user',content:text});
  addBubble('user',text);
  isGen=true; abortCtrl=new AbortController();
  const btn=$('send-btn'); btn.textContent='■'; btn.classList.add('stop');
  const bubbleEl=addBubble('assistant','',true);
  const cursor=document.createElement('span'); cursor.className='cursor';
  bubbleEl.appendChild(cursor);
  let fullText='';
  try {
    const params={
      temperature:parseFloat($('temperature').value),
      top_p:parseFloat($('top-p').value),
      top_k:parseInt($('top-k').value),
      max_tokens:parseInt($('max-tokens').value),
      repeat_penalty:parseFloat($('rep-pen').value),
    };
    const messages=[{role:'system',content:$('sys-prompt').value},...history.slice(0,-1),{role:'user',content:text}];
    const res=await fetch('/api/chat',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({messages,...params}),signal:abortCtrl.signal
    });
    const reader=res.body.getReader(), dec=new TextDecoder();
    let buf='';
    while(true){
      const {done,value}=await reader.read();
      if(done) break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n'); buf=lines.pop();
      for(const line of lines){
        if(!line.startsWith('data: ')) continue;
        const raw=line.slice(6).trim();
        if(raw==='[DONE]') continue;
        try{
          const j=JSON.parse(raw);
          const tok=j.choices?.[0]?.delta?.content||'';
          if(!tok) continue;
          fullText+=tok;
          renderBubble(bubbleEl,fullText,cursor);
          if(j.usage) updateTokenBar(j.usage);
        }catch{}
      }
      scrollDown();
    }
    cursor.remove();
    history.push({role:'assistant',content:fullText});
  }catch(e){
    cursor.remove();
    if(e.name==='AbortError'){
      bubbleEl.innerHTML+='<span style="color:var(--dim);font-size:11px"> [durduruldu]</span>';
      if(fullText) history.push({role:'assistant',content:fullText});
    } else {
      bubbleEl.innerHTML=`<span style="color:var(--red)">⚠ ${e.message}</span>`;
      history.pop();
    }
  }finally{
    isGen=false; abortCtrl=null;
    btn.textContent='➤'; btn.classList.remove('stop');
    scrollDown();
  }
}

function renderBubble(el,text,cursor){
  if (typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
    if (!window._markedRendererReady) {
      const renderer = new marked.Renderer();
      const origCode = renderer.code.bind(renderer);
      renderer.code = function(code, lang, escaped) {
        if (lang && hljs.getLanguage(lang)) {
          const highlighted = hljs.highlight(code, { language: lang }).value;
          return '<pre><code class="hljs language-' + lang + '">' + highlighted + '</code></pre>';
        }
        const auto = hljs.highlightAuto(code).value;
        return '<pre><code class="hljs">' + auto + '</code></pre>';
      };
      marked.setOptions({ breaks: true, renderer: renderer });
      window._markedRendererReady = true;
    }
    el.innerHTML = DOMPurify.sanitize(marked.parse(text));
    el.appendChild(cursor);
  } else {
    el.innerHTML='';
    const parts=text.split(/(```[\s\S]*?```)/g);
    for(const p of parts){
      if(p.startsWith('```')){
        const lines=p.split('\n');
        const pre=document.createElement('pre');
        const code=document.createElement('code');
        code.textContent=lines.slice(1,-1).join('\n');
        pre.appendChild(code); el.appendChild(pre);
      } else {
        const sp=document.createElement('span');
        sp.innerHTML=p.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          .replace(/\*\*(.*?)\*\*/g,'<b>$1</b>').replace(/\*(.*?)\*/g,'<i>$1</i>')
          .replace(/`([^`]+)`/g,'<code>$1</code>');
        el.appendChild(sp);
      }
    }
    el.appendChild(cursor);
  }
}

function addBubble(role,text,raw=false){
  const msg=document.createElement('div'); msg.className='msg '+role;
  const av=document.createElement('div'); av.className='avatar';
  av.textContent=role==='user'?'👤':role==='system'?'⚙':'🦙';
  const bub=document.createElement('div'); bub.className='bubble';
  if(text&&!raw) bub.textContent=text;
  msg.appendChild(av); msg.appendChild(bub);
  $('messages').appendChild(msg);
  scrollDown(); return bub;
}

function clearChat(){ history=[]; $('messages').innerHTML=''; $('token-bar').innerHTML=''; showWelcome(); }
function hideWelcome(){ $('welcome')?.remove(); }
function showWelcome(){
  const el=document.createElement('div'); el.className='welcome'; el.id='welcome';
  el.innerHTML=`<div class="welcome-big">🤖</div>
    <div class="welcome-title">EfeMultiAIbot</div>
    <div class="welcome-sub">Sunucuyu başlatıp sohbet et</div>
    <div class="chips">
      <div class="chip" onclick="quickSend('Merhaba! Kendini kısaca tanıt.')">Kendini tanıt</div>
      <div class="chip" onclick="quickSend('Python\\'da async/await örneği')">Async Python</div>
      <div class="chip" onclick="quickSend('Transformer mimarisi nedir?')">Transformer</div>
    </div>`;
  $('messages').appendChild(el);
}
function updateTokenBar(u){
  $('token-bar').innerHTML=`p:<span>${u.prompt_tokens||0}</span> g:<span>${u.completion_tokens||0}</span> t:<span>${u.total_tokens||0}</span>`;
}
function scrollDown(){ const el=$('messages'); el.scrollTop=el.scrollHeight; }

$('prompt').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();handleSend();}
  if(e.key==='Escape'&&isGen) abortCtrl?.abort();
});
$('prompt').addEventListener('input',function(){
  this.style.height='auto';
  this.style.height=Math.min(this.scrollHeight,140)+'px';
});

// initial status poll
fetch('/api/server/status').then(r=>r.json()).then(d=>{if(d.running) updateLLMStatus(true);});
</script>
</body>
</html>
"""

# ═══════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return HTML


@app.route('/health')
def health_check():
    """Sağlık kontrolü — Nginx/Load balancer/uptime monitor için."""
    with _llm_lock:
        llm_ok = _llm_status.get("running", False)
    bot_st = bot.status()
    bot_ok = bot_st.get("running", False)
    db_ok  = False
    try:
        conn = mm._pool.get()
        conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        pass
    healthy = db_ok  # DB en kritik bileşen
    status_code = 200 if healthy else 503
    return jsonify({
        "status":  "healthy" if healthy else "degraded",
        "db":      "ok" if db_ok else "error",
        "llm":     "running" if llm_ok else "stopped",
        "bot":     "running" if bot_ok else "stopped",
        "uptime_s": round(time.time() - mm._metrics.get("start_time", time.time()), 0),
    }), status_code

# ── LLM Server ──────────────────────────────────────────────

def _log(text: str, level: str = '') -> None:
    prefix = {'ok':'[OK] ','err':'[ERR] ','info':'[INFO] ','warn':'[WARN] '}.get(level,'')
    msg = prefix + text
    try:
        _llm_queue.put_nowait(msg)
    except queue.Full:
        pass
    print(msg)


@app.route('/api/server/status')
def server_status():
    with _llm_lock:
        snap = dict(_llm_status)
    return jsonify(snap)


@app.route('/api/server/start', methods=['POST'])
def start_server_route():
    global _llm_proc
    with _llm_lock:
        if _llm_proc and _llm_proc.poll() is None:
            return jsonify({"ok": False, "error": "Zaten çalışıyor"})

        data     = request.json or {}
        model    = data.get('model','')
        port     = int(data.get('port', 8080))
        ctx      = int(data.get('ctx', 32768))
        ngl      = int(data.get('ngl', 99))
        threads  = int(data.get('threads', 8))
        parallel = int(data.get('parallel', 4))
        mmproj_path = data.get('mmproj', '')

        if not os.path.exists(model):
            return jsonify({"ok": False, "error": f"Model bulunamadı: {model}"})

        candidates = [
            os.path.expanduser("~/llama.cpp/build/bin/llama-server"),
            os.path.expanduser("~/llama.cpp/llama.cpp/build/bin/llama-server"),
            "./build/bin/llama-server", "llama-server",
        ]
        binary = None
        for c in candidates:
            if os.path.exists(c):
                binary = c; break
        if not binary:
            r = subprocess.run(['which','llama-server'], capture_output=True, text=True)
            if r.returncode == 0:
                binary = r.stdout.strip()
            else:
                return jsonify({"ok": False, "error": "llama-server bulunamadı"})

        mmproj = None
        if mmproj_path:
            if os.path.exists(mmproj_path):
                mmproj = mmproj_path
                _log(f"mmproj seçildi: {os.path.basename(mmproj)}", 'info')
            else:
                _log(f"mmproj dosyası bulunamadı: {mmproj_path}", 'warn')

        cmd = [binary, '-m', model, '-c', str(ctx),
               '--port', str(port), '-ngl', str(ngl),
               '-t', str(threads), '-np', str(parallel),
               '-b', '512', '-cb', '--flash-attn', '--log-disable']
        if mmproj:
            cmd.extend(['--mmproj', mmproj])

        _log(f"Komut: {' '.join(cmd)}", 'info')
        _llm_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        _llm_status.update({"running": True, "pid": _llm_proc.pid, "port": port})
        # Update mm llm_port
        mm.llm_port = port

        def _read():
            for line in _llm_proc.stdout:
                line = line.rstrip()
                if line: _log(line)
            _llm_status["running"] = False
            _log("LLM durdu.", 'warn')

        def _wait_ready():
            for _ in range(60):
                if _llm_proc.poll() is not None:
                    _log("LLM başlatılamadı — süreç çöktü!", 'err')
                    _llm_status["running"] = False
                    return
                time.sleep(0.5)
                try:
                    r = http_req.get(f"http://127.0.0.1:{port}/health", timeout=1)
                    if r.status_code == 200:
                        _log(f"✓ LLM hazır → http://127.0.0.1:{port}", 'ok')
                        # RAG: LLM embedding'ini yeniden dene
                        if mm.rag_reinit(llm_port=port):
                            _log("✓ RAG: LLM embedding aktif", 'ok')
                        return
                except Exception: pass
            _log("LLM zaman aşımı!", 'err')

        threading.Thread(target=_read,       daemon=True).start()
        threading.Thread(target=_wait_ready, daemon=True).start()
        return jsonify({"ok": True})


@app.route('/api/server/stop', methods=['POST'])
def stop_server_route():
    global _llm_proc
    with _llm_lock:
        if _llm_proc and _llm_proc.poll() is None:
            _llm_proc.terminate()
            try: _llm_proc.wait(timeout=5)
            except Exception: _llm_proc.kill()
        _llm_proc = None
        _llm_status["running"] = False
    _log("LLM durduruldu.", 'warn')
    return jsonify({"ok": True})


@app.route('/api/models')
def list_models():
    dirs = [
        os.path.expanduser("~/Downloads"), os.path.expanduser("~/Desktop"),
        os.path.expanduser("~/models"), os.path.expanduser("~/.local/share/models"),
        str(APP_DIR),
    ]
    found = []
    for d in dirs:
        if os.path.isdir(d):
            found += glob.glob(os.path.join(d, '**/*.gguf'), recursive=True)
            found += glob.glob(os.path.join(d, '*.gguf'))
    models = [f for f in sorted(set(found)) if not os.path.basename(f).lower().startswith('mmproj')]
    return jsonify({"models": models})


@app.route('/api/mmproj')
def list_mmproj():
    """Model dizinindeki veya tüm dizinlerdeki mmproj dosyalarını listele."""
    model = request.args.get('model', '')
    scan_all = request.args.get('all', '')
    dirs = []
    if model and os.path.exists(model):
        dirs.append(os.path.dirname(model))
    if scan_all:
        dirs += [
            os.path.expanduser("~/Downloads"), os.path.expanduser("~/Desktop"),
            os.path.expanduser("~/models"), os.path.expanduser("~/.local/share/models"),
            str(APP_DIR),
        ]
    found = []
    for d in dirs:
        if os.path.isdir(d):
            found += glob.glob(os.path.join(d, '**/mmproj*.gguf'), recursive=True)
            found += glob.glob(os.path.join(d, 'mmproj*.gguf'))
    return jsonify({"projectors": sorted(set(found))})


@app.route('/api/logs')
def stream_logs():
    def gen():
        while True:
            try:
                msg = _llm_queue.get(timeout=25)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield "data: .\n\n"
    return Response(stream_with_context(gen()),
                    content_type='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json or {}
    with _llm_lock:
        port = _llm_status.get("port", 8080)
    payload = {
        "model": "local",
        "messages": data.get("messages", []),
        "temperature": data.get("temperature", 0.7),
        "top_p": data.get("top_p", 0.8),
        "top_k": data.get("top_k", 20),
        "max_tokens": data.get("max_tokens", 16384),
        "repeat_penalty": data.get("repeat_penalty", 1.1),
        "stream": True,
    }
    def gen():
        try:
            r = http_req.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                json=payload, stream=True, timeout=120)
            for chunk in r.iter_lines():
                if chunk: yield chunk.decode() + "\n\n"
        except Exception as e:
            log.error(f"Chat stream error: {e}")
            yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"
    return Response(stream_with_context(gen()),
                    content_type='text/event-stream',
                    headers={'Cache-Control':'no-cache'})

# ── Stats SSE ───────────────────────────────────────────────

@app.route('/api/stats/stream')
def stats_stream():
    def gen():
        while True:
            try:
                stats = mm.get_stats()
                with _llm_lock:
                    stats["llm"] = {"running": _llm_status["running"],
                                    "port": _llm_status.get("port", 8080)}
                stats["bot"] = bot.status()
                yield f"data: {json.dumps(stats)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"
            time.sleep(4)
            # Heartbeat — proxy timeout önlemi
            yield ": heartbeat\n\n"
    return Response(stream_with_context(gen()),
                    content_type='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


@app.route('/api/db/stats')
def db_stats():
    try:
        stats = mm.get_stats()
        with _llm_lock:
            stats["llm"] = dict(_llm_status)
        stats["bot"] = bot.status()
        return jsonify(stats)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/db/maintenance', methods=['POST'])
def db_maintenance():
    try:
        report = mm.run_maintenance()
        return jsonify({"ok": True, "report": report})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/db/images/purge', methods=['POST'])
def purge_images():
    try:
        days = (request.json or {}).get('days', 7)
        deleted = mm.purge_old_images(days)
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/db/summarize', methods=['POST'])
def summarize_all():
    """Tüm aktif chatlar için LLM özetlemesini çalıştır."""
    try:
        conn = mm._pool.get()
        chat_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT chat_id FROM messages"
        ).fetchall()]
        processed = 0
        for cid in chat_ids:
            result = mm.summarize_old_context(cid)
            if result:
                processed += 1
        return jsonify({"ok": True, "chats_processed": processed, "total": len(chat_ids)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/db/chat/<chat_id>/clear', methods=['DELETE'])
def clear_chat_history(chat_id: str):
    try:
        n = mm.delete_chat(chat_id)
        return jsonify({"ok": True, "deleted": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── RAG API ─────────────────────────────────────────────────

@app.route('/api/rag/stats')
def rag_stats_route():
    """RAG vektör deposu istatistikleri."""
    try:
        return jsonify({"ok": True, **mm.rag_stats()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/rag/search')
def rag_search_route():
    """RAG semantik arama."""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({"ok": False, "error": "q parametresi gerekli"}), 400
    chat_id = request.args.get('chat_id') or None
    try:
        k = min(int(request.args.get('k', RAG_TOP_K)), 50)
    except (ValueError, TypeError):
        k = RAG_TOP_K
    try:
        results = mm.rag_search(q, chat_id=chat_id, k=k)
        return jsonify({"ok": True, "results": results, "count": len(results)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/rag/context')
def rag_context_route():
    """RAG bağlam oluştur — LLM'e enjekte edilecek metin."""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({"ok": False, "error": "q parametresi gerekli"}), 400
    chat_id = request.args.get('chat_id') or None
    try:
        budget = int(request.args.get('budget', 1024))
    except (ValueError, TypeError):
        budget = 1024
    try:
        ctx = mm.rag_build_context(q, chat_id=chat_id, token_budget=budget)
        return jsonify({"ok": True, "context": ctx,
                        "tokens_est": estimate_tokens(ctx) if ctx else 0})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/rag/reindex', methods=['POST'])
def rag_reindex_route():
    """Tüm L2+L3 mesajları yeniden indeksle (uzun sürebilir)."""
    try:
        result = mm.rag_reindex_all()
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/rag/reset', methods=['DELETE'])
def rag_reset_route():
    """RAG vektör deposunu sıfırla."""
    try:
        ok = mm.rag_reset()
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/rag/reinit', methods=['POST'])
def rag_reinit_route():
    """RAG embedding bağlantısını yeniden dene (LLM başladıktan sonra)."""
    try:
        port = (request.json or {}).get('port', 8080)
        ok = mm.rag_reinit(llm_port=port)
        return jsonify({"ok": ok, "stats": mm.rag_stats()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Contacts ────────────────────────────────────────────────

@app.route('/api/contacts')
def get_contacts():
    try:
        return jsonify({"ok": True, "contacts": mm.get_contacts()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/contacts/toggle', methods=['POST'])
def toggle_contact():
    data = request.json or {}
    cid  = data.get('id')
    if not cid:
        return jsonify({"ok": False, "error": "id eksik"})
    try:
        ok = mm.toggle_ai(cid, bool(data.get('enabled', 0)))
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Mesaj API (WhatsApp bot için) ───────────────────────────

@app.route('/api/messages/save', methods=['POST'])
def save_message_route():
    d = request.json or {}
    chat_id = d.get('chat_id', '')
    role    = d.get('role', '')
    content = d.get('content', '')
    if not chat_id or not role:
        return jsonify({"ok": False, "error": "chat_id ve role zorunlu"}), 400
    if len(chat_id) > MAX_CHAT_ID_LENGTH:
        return jsonify({"ok": False, "error": "chat_id çok uzun"}), 400
    if role not in ('user', 'assistant', 'system', 'summary'):
        return jsonify({"ok": False, "error": f"Geçersiz rol: {role}"}), 400
    if len(content) > MAX_MESSAGE_LENGTH:
        content = content[:MAX_MESSAGE_LENGTH]
    try:
        mm.save_message(chat_id, role, content)
        if role == 'user':
            mm.record_contact_message(chat_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/messages/<chat_id>')
def get_messages_route(chat_id: str):
    try:
        limit  = int(request.args.get('limit', 10))
        budget = request.args.get('budget', type=int)
        msgs   = mm.get_recent_messages(chat_id, limit=limit, token_budget=budget)
        return jsonify({"ok": True, "messages": msgs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/contacts/upsert', methods=['POST'])
def upsert_contact_route():
    d = request.json or {}
    cid = d.get('id')
    if not cid:
        return jsonify({"ok": False, "error": "id zorunlu"}), 400
    try:
        mm.upsert_contact(cid, d.get('name', ''), d.get('pushname', ''))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/ai_enabled/<contact_id>')
def ai_enabled_route(contact_id: str):
    try:
        return jsonify({"enabled": mm.is_ai_enabled(contact_id)})
    except Exception as e:
        return jsonify({"enabled": False, "error": str(e)}), 500

# ── Chat Export / Import / Search ────────────────────────────

@app.route('/api/export/<chat_id>')
def export_chat_route(chat_id: str):
    """Sohbet geçmişini JSON olarak dışa aktar (arşiv dahil)."""
    try:
        if len(chat_id) > MAX_CHAT_ID_LENGTH:
            return jsonify({"ok": False, "error": "Geçersiz chat_id"}), 400
        limit = int(request.args.get('limit', 1000))
        msgs = mm.get_recent_messages(chat_id, limit=limit, include_archive=True)
        export_data = {
            "chat_id": chat_id,
            "exported_at": datetime.now().isoformat(),
            "message_count": len(msgs),
            "messages": msgs,
        }
        return Response(
            json.dumps(export_data, ensure_ascii=False, indent=2),
            content_type='application/json',
            headers={
                'Content-Disposition': f'attachment; filename=chat_{chat_id[:20]}_{int(time.time())}.json'
            }
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/import', methods=['POST'])
def import_chat_route():
    """JSON formatında sohbet geçmişini içe aktar."""
    try:
        d = request.json or {}
        chat_id = d.get('chat_id')
        messages = d.get('messages', [])
        if not chat_id or not messages:
            return jsonify({"ok": False, "error": "chat_id ve messages zorunlu"}), 400
        if len(chat_id) > MAX_CHAT_ID_LENGTH:
            return jsonify({"ok": False, "error": "Geçersiz chat_id"}), 400
        imported = 0
        for msg in messages:
            role = msg.get('role', '')
            content = msg.get('content', '')
            if role in ('user', 'assistant', 'system', 'summary') and content:
                mm.save_message(chat_id, role, content[:MAX_MESSAGE_LENGTH])
                imported += 1
        return jsonify({"ok": True, "imported": imported})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/search')
def search_messages_route():
    """Mesajlarda arama yap. query (zorunlu), chat_id (opsiyonel), limit."""
    try:
        query = request.args.get('q', '').strip()
        chat_id = request.args.get('chat_id', '')
        limit = min(int(request.args.get('limit', 50)), 200)
        if not query or len(query) < 2:
            return jsonify({"ok": False, "error": "En az 2 karakterlik arama terimi gerekli"}), 400

        conn = mm._pool.get()
        # Search in active messages
        if chat_id:
            rows = conn.execute("""
                SELECT id, chat_id, role, content, ts FROM messages
                WHERE chat_id = ?
                ORDER BY ts DESC LIMIT ?
            """, (chat_id, limit * 5)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, chat_id, role, content, ts FROM messages
                ORDER BY ts DESC LIMIT ?
            """, (limit * 5,)).fetchall()

        results = []
        q_lower = query.lower()
        for r in rows:
            text = _decode(r["content"])
            if q_lower in text.lower():
                # Arama teriminin bağlamını göster (±80 karakter)
                idx = text.lower().find(q_lower)
                start = max(0, idx - 80)
                end = min(len(text), idx + len(query) + 80)
                snippet = ('…' if start > 0 else '') + text[start:end] + ('…' if end < len(text) else '')
                results.append({
                    "id": r["id"],
                    "chat_id": r["chat_id"],
                    "role": r["role"],
                    "snippet": snippet,
                    "ts": r["ts"],
                })
                if len(results) >= limit:
                    break

        return jsonify({"ok": True, "results": results, "total": len(results)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/system/info')
def system_info_route():
    """Sistem bilgilerini döndür — debug ve izleme için."""
    try:
        import platform
        import shutil

        disk = shutil.disk_usage(str(APP_DIR))
        stats = mm.get_stats()

        with _llm_lock:
            llm_snap = dict(_llm_status)

        return jsonify({
            "ok": True,
            "system": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
            },
            "disk": {
                "total_gb": round(disk.total / 1024**3, 2),
                "used_gb": round(disk.used / 1024**3, 2),
                "free_gb": round(disk.free / 1024**3, 2),
                "used_pct": round(disk.used / disk.total * 100, 1),
            },
            "app": {
                "version": "2.0.0",
                "db_size_mb": stats.get("db", {}).get("size_mb", 0),
                "active_messages": stats.get("messages", {}).get("active", 0),
                "archived_messages": stats.get("messages", {}).get("archived", 0),
                "uptime_s": stats.get("uptime_s", 0),
                "cache_hit_rate": stats.get("cache", {}).get("hit_rate", 0),
            },
            "llm": llm_snap,
            "bot": bot.status(),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Bot Control ─────────────────────────────────────────────

@app.route('/api/bot/status')
def bot_status():
    try:
        return jsonify(bot.status())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/bot/start', methods=['POST'])
def bot_start():
    try:
        ensure_bot_file()
        ok = bot.start()
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/bot/stop', methods=['POST'])
def bot_stop():
    try:
        bot.stop()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Web Chat Yönetimi ────────────────────────────────────────

@app.route('/api/webchat/users')
def webchat_list_users_route():
    try:
        return jsonify({"ok": True, "users": mm.webchat_list_users()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/webchat/stats')
def webchat_stats_route():
    try:
        return jsonify({"ok": True, "stats": mm.webchat_get_stats()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/webchat/users/<uid>', methods=['POST', 'PUT'])
def webchat_update_user_route(uid: str):
    data = request.json or {}
    # Remove non-updatable keys
    data.pop('id', None); data.pop('ip', None)
    data.pop('created_at', None); data.pop('msg_count', None)
    try:
        ok = mm.webchat_update_user(uid, **data)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/webchat/users/<uid>', methods=['DELETE'])
def webchat_delete_user_route(uid: str):
    try:
        mm.webchat_delete_user(uid)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/webchat/limits/<uid>')
def webchat_limits_route(uid: str):
    """chat_client.py tarafından çağrılır — kullanıcı limitlerini döndür."""
    try:
        user = mm.webchat_get_user(uid)
        if not user:
            return jsonify({"ok": False, "error": "Kullanıcı bulunamadı"}), 404
        rate = mm.webchat_check_rate(uid)
        return jsonify({
            "ok": True,
            "enabled":         bool(user["enabled"]),
            "rate_limit_hour": user["rate_limit_hour"],
            "daily_limit":     user["daily_limit"],
            "max_tokens":      user["max_tokens"],
            "sys_prompt":      user["sys_prompt"],
            "experts":         user.get("experts", "{}"),
            "username":        user["username"],
            "hourly_used":     rate.get("hourly_used", 0),
            "daily_used":      rate.get("daily_used", 0),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/webchat/register', methods=['POST'])
def webchat_register_route():
    """chat_client.py: kullanıcıyı kaydet / güncelle."""
    d   = request.json or {}
    uid = d.get('uid', '')
    if not uid:
        return jsonify({"ok": False, "error": "uid zorunlu"}), 400
    try:
        ip  = request.headers.get('X-Forwarded-For', request.remote_addr or '')
        ip  = ip.split(',')[0].strip()
        user = mm.webchat_register(uid, d.get('username', 'Anonim'), ip)
        rate = mm.webchat_check_rate(uid)
        return jsonify({"ok": True, "user": user, "rate": rate})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/webchat/chat', methods=['POST'])
def webchat_chat_route():
    """
    chat_client.py: mesaj al, rate-check yap, LLM'e ilet, kaydet.
    SSE stream döner.
    """
    d       = request.json or {}
    uid     = d.get('uid', '')
    content = d.get('content', '').strip()

    if not uid or not content:
        return jsonify({"ok": False, "error": "uid ve content zorunlu"}), 400
    if len(content) > MAX_MESSAGE_LENGTH:
        return jsonify({"ok": False, "error": f"Mesaj çok uzun (max {MAX_MESSAGE_LENGTH} karakter)"}), 400
    if len(uid) > MAX_CHAT_ID_LENGTH:
        return jsonify({"ok": False, "error": "Geçersiz kullanıcı ID"}), 400

    with _llm_lock:
        if not _llm_status.get("running"):
            return jsonify({"ok": False, "error": "LLM sunucusu çalışmıyor"}), 503

    # Rate check
    rate = mm.webchat_check_rate(uid)
    if not rate.get("allowed"):
        return jsonify({"ok": False, "error": rate.get("reason", "Limit aşıldı"),
                        "rate": rate}), 429

    user = mm.webchat_get_user(uid)
    if not user:
        return jsonify({"ok": False, "error": "Kullanıcı bulunamadı"}), 404

    with _llm_lock:
        port = _llm_status.get("port", 8080)
    max_tokens = user.get("max_tokens", 2048)
    sys_prompt = user.get("sys_prompt") or d.get("default_sys_prompt",
        "Yardımsever, saygılı ve yetenekli bir yapay zeka asistanısın.")

    # Tarih/saat bilgisi ekle
    now_str = datetime.now().strftime("%d %B %Y, %A %H:%M")
    sys_prompt += f"\n[Tarih: {now_str}]"

    experts_json = user.get("experts", "{}")
    try:
        experts_dict = json.loads(experts_json)
    except Exception:
        experts_dict = {}

    log.debug(f"webchat_chat: uid={uid}, experts={experts_dict}")
    tool_experts = {k: v for k, v in experts_dict.items() if k != "agentic"}
    any_expert_active = any(tool_experts.values())

    # ── Uzman prompt'ları: aktifse ZORUNLU, deaktifse YOK ────
    if any_expert_active:
        expert_sections = []
        if experts_dict.get("calculator"):
            expert_sections.append(
                "## 🧮 HESAP MAKİNESİ (AKTİF — ZORUNLU KULLANIM)\n"
                "Matematiksel hesaplama içeren HER soruda bu aracı KULLANMAK ZORUNDASIN.\n"
                "Asla kendi kafandan hesaplama yapma — her zaman bu aracı çağır.\n"
                "Kullanım formatı (tam olarak bu XML etiketini yaz):\n"
                "<tool>{\"name\": \"calculator\", \"expr\": \"MATEMATIKSEL_IFADE\"}</tool>\n"
                "Örnekler:\n"
                "- Kullanıcı: \"15 * 37 kaç?\" → <tool>{\"name\": \"calculator\", \"expr\": \"15 * 37\"}</tool>\n"
                "- Kullanıcı: \"Kareköğünü bul: 144\" → <tool>{\"name\": \"calculator\", \"expr\": \"sqrt(144)\"}</tool>\n"
                "- Kullanıcı: \"2^10 + 3^5\" → <tool>{\"name\": \"calculator\", \"expr\": \"2**10 + 3**5\"}</tool>\n"
                "Kullanılabilir fonksiyonlar: sin, cos, tan, sqrt, log, log2, pi, e, factorial, pow, ceil, floor, ve tüm math modülü."
            )
        if experts_dict.get("sandbox"):
            expert_sections.append(
                "## 💻 PYTHON SANDBOX (AKTİF — ZORUNLU KULLANIM)\n"
                "Kullanıcı kod yazmayı, çalıştırmayı, test etmeyi veya programlama istediğinde bu aracı KULLANMAK ZORUNDASIN.\n"
                "Kod sadece gösterme — her zaman Sandbox'ta kaydet ve çalıştır.\n"
                "Kullanım formatı (tam olarak bu XML etiketini yaz):\n"
                "<tool>{\"name\": \"sandbox\", \"code\": \"PYTHON_KODU\", \"filename\": \"DOSYA_ADI.py\"}</tool>\n"
                "Örnekler:\n"
                "- Kullanıcı: \"Fibonacci yaz\" → <tool>{\"name\": \"sandbox\", \"code\": \"def fib(n):\\n    a, b = 0, 1\\n    for _ in range(n):\\n        print(a, end=' ')\\n        a, b = b, a+b\\nfib(10)\", \"filename\": \"fibonacci.py\"}</tool>\n"
                "- Kullanıcı: \"Merhaba dünya\" → <tool>{\"name\": \"sandbox\", \"code\": \"print('Merhaba Dünya!')\", \"filename\": \"hello.py\"}</tool>\n"
                "Kurallar:\n"
                "- Her zaman çalıştırılabilir, tam kod yaz (import'lar dahil)\n"
                "- Dosya adını içeriğe uygun ver (örn: fibonacci.py, calculator.py)\n"
                "- Çıktıyı görmek için print() kullan"
            )
        if experts_dict.get("web_search"):
            expert_sections.append(
                "## 🌐 WEB ARAMA (AKTİF — ZORUNLU KULLANIM)\n"
                "Güncel bilgi gerektiren sorularda (haberler, hava durumu, spor sonuçları, "
                "fiyatlar, güncel olaylar vb.) bu aracı KULLANMAK ZORUNDASIN.\n"
                "Bilgi eski olabilecek veya gerçek zamanlı veri gerektiren HER soruda kullan.\n"
                "Kullanım formatı (tam olarak bu XML etiketini yaz):\n"
                "<tool>{\"name\": \"web_search\", \"query\": \"ARAMA_SORGUSU\"}</tool>\n"
                "Örnekler:\n"
                "- Kullanıcı: \"Bugün hava nasıl?\" → <tool>{\"name\": \"web_search\", \"query\": \"bugün hava durumu\"}</tool>\n"
                "- Kullanıcı: \"Bitcoin fiyatı ne?\" → <tool>{\"name\": \"web_search\", \"query\": \"bitcoin fiyatı güncel\"}</tool>\n"
                "- Kullanıcı: \"En son haberler\" → <tool>{\"name\": \"web_search\", \"query\": \"son dakika haberleri\"}</tool>\n"
                "Kurallar:\n"
                "- Arama sorgusunu kısa ve öz yaz (Türkçe veya İngilizce)\n"
                "- Arama sonuçlarını doğal dilde özetle, kaynak belirt\n"
                "- Birden fazla arama yapabilirsin"
            )

        sys_prompt += (
            "\n\n═══ AKTİF UZMANLAR VE ARAÇLAR ═══\n"
            "Aşağıdaki araçlar aktif. İlgili durumda MUTLAKA kullan. "
            "Aracı kullanmak için tam olarak <tool>...</tool> XML bloğunu yaz. "
            "Sistem otomatik çalıştırıp sonucu sana geri verecek.\n\n"
            + "\n\n".join(expert_sections)
        )
    else:
        # Hiçbir uzman aktif değilse — araç kullanımını yasakla
        sys_prompt += (
            "\n\nÖNEMLİ: Şu anda hiçbir AI aracın/uzmanın aktif değil. "
            "<tool> etiketleri KULLANMA. Eğer kullanıcı hesaplama veya kod "
            "çalıştırma isterse, sonucu doğrudan metin olarak ver."
        )

    # ── Agentic mod: gelişmiş akıl yürütme ───────────────────
    if experts_dict.get("agentic"):
        sys_prompt += (
            "\n\n═══ AGENTİK MOD (AKTİF) ═══\n"
            "Gelişmiş akıl yürütme modundasın. Karmaşık görevlerde şu yaklaşımı kullan:\n"
            "1. **Analiz**: Görevi alt adımlara böl\n"
            "2. **Plan**: Her adımda hangi aracı kullanacağını belirle\n"
            "3. **Uygula**: Araçları sırayla çağır, sonuçları değerlendir\n"
            "4. **Doğrula**: Sonuçları kontrol et, gerekirse tekrar dene\n"
            "5. **Sentezle**: Tüm bilgileri birleştirip kapsamlı bir cevap ver\n\n"
            "Kurallar:\n"
            "- Her adımda düşünce sürecini kısaca paylaş\n"
            "- Birden fazla araç çağrısı yapabilirsin (sırayla)\n"
            "- Emin olmadığın bilgileri web aramasıyla doğrula\n"
            "- Hesaplamaları her zaman hesap makinesiyle yap\n"
            "- Kod gerektiren görevlerde sandbox kullan\n"
            "- Adım adım ilerlediğini kullanıcıya göster"
        )

    # History (son 10 mesaj)
    chat_id = f"web:{uid}"
    history = mm.get_recent_messages(chat_id, limit=12,
                                     token_budget=int(max_tokens * 1.5))

    # ── Görsel eki varsa multimodal mesaj oluştur ─────────────
    images = d.get('images', [])
    if images and isinstance(images, list):
        user_content = [{"type": "text", "text": content}]
        for img in images:
            if isinstance(img, dict) and img.get('data'):
                mime = img.get('mime', 'image/png')
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{img['data']}"}
                })
        user_msg = {"role": "user", "content": user_content}
    else:
        user_msg = {"role": "user", "content": content}

    # ── RAG: Geçmiş bağlam çekme ─────────────────────────────
    rag_context_msg = None
    if not images:  # görsel sorgularında RAG atlansın
        rag_ctx = mm.rag_build_context(
            content, chat_id=chat_id,
            token_budget=min(1024, int(max_tokens * 0.3)),
        )
        if rag_ctx:
            rag_context_msg = {"role": "system", "content": rag_ctx}

    messages = [{"role": "system", "content": sys_prompt}]
    if rag_context_msg:
        messages.append(rag_context_msg)
    messages += history + [user_msg]

    def _execute_web_search(query: str) -> dict:
        """DuckDuckGo HTML arama — API anahtarı gerektirmez."""
        try:
            r = http_req.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
                timeout=10,
            )
            r.raise_for_status()
            results = []
            # Basit regex ile sonuç çek
            for m in re.finditer(
                r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?'
                r'<a class="result__snippet"[^>]*>(.*?)</a>',
                r.text, re.DOTALL
            ):
                href, title, snippet = m.group(1), m.group(2), m.group(3)
                title = re.sub(r'<[^>]+>', '', title).strip()
                snippet = re.sub(r'<[^>]+>', '', snippet).strip()
                if title:
                    results.append(f"• {title}\n  {snippet}")
                if len(results) >= 5:
                    break
            if results:
                return {"text": f"🌐 Arama sonuçları ({query}):\n\n" + "\n\n".join(results)}
            return {"text": f"🌐 '{query}' için sonuç bulunamadı."}
        except Exception as e:
            log.warning(f"Web search failed: {e}")
            return {"text": f"❌ Arama hatası: {str(e)[:200]}"}

    def execute_tool(tool_str):
        """Araç çalıştır — sadece aktif uzmanlar çalıştırılır."""
        try:
            t = json.loads(tool_str)
            name = t.get("name")

            # ── Güvenlik: deaktif uzmanı çalıştırma ──
            if name == "calculator" and not experts_dict.get("calculator"):
                return {"text": "⚠ Hesap Makinesi uzmanı deaktif. Ayarlardan aktif edin."}
            if name == "sandbox" and not experts_dict.get("sandbox"):
                return {"text": "⚠ Python Sandbox uzmanı deaktif. Ayarlardan aktif edin."}
            if name == "web_search" and not experts_dict.get("web_search"):
                return {"text": "⚠ Web Arama uzmanı deaktif. Ayarlardan aktif edin."}

            if name == "calculator":
                expr_str = t.get("expr", "")
                res = _safe_calc_eval(expr_str)
                return {"text": f"✅ Hesap Sonucu: {res}"}
            elif name == "web_search":
                query = t.get("query", "").strip()
                if not query:
                    return {"text": "⚠ Arama sorgusu boş."}
                return _execute_web_search(query)
            elif name == "sandbox":
                user_dir = SANDBOX_DIR / werkzeug.utils.secure_filename(uid)
                user_dir.mkdir(parents=True, exist_ok=True)
                path = user_dir / werkzeug.utils.secure_filename(t.get("filename", "script.py"))
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(t.get("code", ""))

                proc = subprocess.Popen(
                    ["python3", "-u", str(path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    cwd=str(user_dir),
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
                )

                try:
                    # GUI açılması için kısa gecikme
                    time.sleep(1.5)

                    img_b64 = None
                    try:
                        sc = subprocess.run(
                            ["cosmic-screenshot", "--interactive=false",
                             "--modal=false", "--notify=false", "-s", "/tmp"],
                            capture_output=True, text=True, timeout=5
                        )
                        img_path = sc.stdout.strip()
                        if img_path and os.path.exists(img_path):
                            with open(img_path, "rb") as bf:
                                img_b64 = b64mod.b64encode(bf.read()).decode('utf-8')
                            os.unlink(img_path)
                    except Exception:
                        pass

                    try:
                        stdout, stderr = proc.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        stdout, stderr = proc.communicate()
                        stderr += "\n\n[Timeout: 10s sonra zorla durduruldu]"
                finally:
                    # Ensure subprocess is cleaned up even if an unexpected error occurs
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()

                text_out = f"✅ Kod kaydedildi ve çalıştırıldı: {path.name}\n── Çıktı ──\n{stdout}"
                if stderr.strip():
                    text_out += f"\n── Hatalar ──\n{stderr}"
                return {"text": text_out.strip(), "image_b64": img_b64}
            else:
                return {"text": f"⚠ Bilinmeyen araç: {name}"}
        except Exception as e:
            return {"text": f"❌ Araç çalıştırma hatası: {e}"}

    # ── Hesap makinesi için safe eval ──────────────────────────
    def _calc_eval(expr_str: str):
        """Güvenli matematik eval — AST tabanlı allowlist."""
        return _safe_calc_eval(expr_str)

    # ── Aşama 1: Gizli LLM çağrısı — math JSON extract ──────
    def _phase1_extract_math(user_text: str):
        """
        LLM'e gizli prompt gönder: sadece matematiksel ifadeyi JSON olarak çıkar.
        Dönüş: (expr_str, raw_result) veya (None, None) eğer math yoksa.
        """
        log.info(f"[PHASE1] Starting math extraction for: {user_text[:80]}")
        phase1_sys = (
            "Sen bir matematik algılama motorusun. Kullanıcı metnindeki matematiksel "
            "ifadeyi bul ve SADECE aşağıdaki JSON formatında döndür:\n"
            '{"expr": "PYTHON_MATH_IFADESI"}\n\n'
            "Kurallar:\n"
            "- Çarpma: *, bölme: /, üs: **, karekök: sqrt(x)\n"
            "- Trigonometri: sin, cos, tan (radyan cinsinden)\n"
            "- pi, e, factorial, log, log2, ceil, floor kullanabilirsin\n"
            "- Birden fazla işlem varsa hepsini tek expr'de birleştir\n"
            '- Matematiksel işlem YOKSA: {"expr": null}\n'
            "- JSON dışında HİÇBİR ŞEY yazma. Açıklama yapma."
        )
        try:
            r = http_req.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                json={
                    "model": "local",
                    "messages": [
                        {"role": "system", "content": phase1_sys},
                        {"role": "user",   "content": user_text},
                    ],
                    "max_tokens": 120,
                    "temperature": 0.0,
                    "stream": False,
                },
                timeout=30
            )
            raw = r.json()["choices"][0]["message"]["content"].strip()
            log.info(f"[PHASE1] LLM returned: {raw[:200]}")
            # JSON'u parse et — bazen ``` ile sarılabilir
            if "```" in raw:
                raw = raw.split("```")[1].strip()
                if raw.startswith("json"):
                    raw = raw[4:].strip()
            parsed = json.loads(raw)
            expr = parsed.get("expr")
            if not expr or expr == "null":
                log.info("[PHASE1] No math expression found (expr is null)")
                return None, None
            result = _calc_eval(expr)
            log.info(f"[PHASE1] ✅ expr={expr}, result={result}")
            return expr, result
        except Exception as e:
            log.warning(f"[PHASE1] FAILED: {e}")
            return None, None

    full_reply = ""

    def stream_conversation():
        nonlocal messages, full_reply
        calc_active = experts_dict.get("calculator", False)
        sandbox_active = experts_dict.get("sandbox", False)

        try:
            # ═══ DOUBLE PROMPT: Hesap makinesi aktifse ═══
            if calc_active:
                # Frontend'e "düşünüyor" sinyali gönder
                yield f'event: thinking\ndata: {{"status":"start","tool":"calculator"}}\n\n'

                expr, result = _phase1_extract_math(content)

                if expr is not None and result is not None:
                    # Frontend'e hesap sonucunu bildir
                    yield f'event: calc_result\ndata: {{"expr":"{expr}","result":"{result}"}}\n\n'

                    # ── Aşama 2: Nihai cevap (streaming) ──
                    phase2_sys = (
                        f"{sys_prompt}\n\n"
                        f"═══ HESAP MAKİNESİ SONUCU ═══\n"
                        f"Kullanıcının sorusuna cevap verirken aşağıdaki KESİN DOĞRU sonucu kullan.\n"
                        f"Bu sonuç hesap makinesi tarafından hesaplanmıştır, değiştirme.\n"
                        f"İfade: {expr}\n"
                        f"Sonuç: {result}\n"
                        f"Bu sonucu kullanarak doğal, açıklayıcı ve doğru bir cevap yaz. "
                        f"<tool> etiketi KULLANMA. Sonucu doğrudan metin içinde ver."
                    )
                    phase2_messages = [
                        {"role": "system", "content": phase2_sys},
                    ] + history + [
                        {"role": "user", "content": content},
                    ]

                    payload = {
                        "model": "local",
                        "messages": phase2_messages,
                        "max_tokens": max_tokens,
                        "temperature": 0.7,
                        "stream": True,
                    }

                    try:
                        r = http_req.post(
                            f"http://127.0.0.1:{port}/v1/chat/completions",
                            json=payload, stream=True, timeout=120
                        )
                        for chunk in r.iter_lines():
                            if chunk:
                                line = chunk.decode("utf-8", errors="replace")
                                if line.startswith("data: "):
                                    raw = line[6:].strip()
                                    if raw == "[DONE]":
                                        break
                                    try:
                                        tok = json.loads(raw)["choices"][0]["delta"].get("content", "")
                                        if tok:
                                            full_reply += tok
                                            # <tool> etiketini filtrele (model yine de üretebilir)
                                            if "<tool>" not in tok:
                                                yield line + "\n\n"
                                    except Exception:
                                        pass
                    except Exception as e:
                        log.error(f"Webchat stream error (phase2): {e}")
                        yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"

                    # Düşünme bitti sinyali
                    yield f'event: thinking\ndata: {{"status":"done"}}\n\n'

                else:
                    # Math ifadesi bulunamadı — normal akışa geç
                    yield f'event: thinking\ndata: {{"status":"skip","reason":"no_math"}}\n\n'
                    # Normal streaming (sandbox desteğiyle)
                    yield from _normal_stream(messages, max_tokens)
                    return

            else:
                # ═══ NORMAL AKIŞ (calculator deaktif) ═══
                yield from _normal_stream(messages, max_tokens)
                return

        finally:
            if content:
                mm.save_message(chat_id, "user", content)
            if full_reply:
                # Strip <tool>...</tool> blocks so saved history is clean for future context.
                # Uses DOTALL so the pattern spans newlines (e.g. multi-line sandbox code).
                clean_reply = re.sub(r'<tool>.*?</tool>', '', full_reply, flags=re.DOTALL).strip()
                if clean_reply:
                    mm.save_message(chat_id, "assistant", clean_reply)
                mm.webchat_log_message(uid)

    def _normal_stream(msgs, max_tok):
        """Standart tek-prompt streaming akışı (sandbox tool desteğiyle)."""
        nonlocal full_reply
        iteration = 0
        in_tool = False
        max_iterations = 8 if experts_dict.get("agentic") else 4
        try:
            while iteration < max_iterations:
                iteration += 1
                payload = {
                    "model": "local",
                    "messages": msgs,
                    "max_tokens": max_tok,
                    "temperature": 0.7,
                    "stream": True,
                }

                tool_buffer = ""
                in_tool = False
                current_reply = ""
                try:
                    r = http_req.post(
                        f"http://127.0.0.1:{port}/v1/chat/completions",
                        json=payload, stream=True, timeout=120
                    )
                    for chunk in r.iter_lines():
                        if chunk:
                            line = chunk.decode()
                            if line.startswith("data: "):
                                raw = line[6:].strip()
                                if raw == "[DONE]":
                                    break
                                try:
                                    tok = json.loads(raw)["choices"][0]["delta"].get("content", "")
                                    if tok:
                                        current_reply += tok
                                        full_reply += tok
                                        if "<tool>" in current_reply and not in_tool:
                                            in_tool = True
                                            tool_idx = current_reply.find("<tool>")
                                            tool_buffer = current_reply[tool_idx + 6:]
                                            yield f'data: {{"choices":[{{"delta":{{"content": "\\n\\n[⚙️ Expert Çalışıyor...]\\n\\n"}}}}]}}\n\n'
                                            continue

                                        if in_tool:
                                            tool_buffer += tok
                                            if "</tool>" in tool_buffer:
                                                tool_json = tool_buffer.split("</tool>")[0]
                                                tool_res = execute_tool(tool_json)
                                                msgs.append({"role": "assistant", "content": current_reply})

                                                # Araç sonucunu kullanıcıya göster
                                                res_text = tool_res.get("text", str(tool_res)) if isinstance(tool_res, dict) else str(tool_res)
                                                summary = res_text[:300].replace('"', '\\"').replace('\n', '\\n')
                                                yield f'data: {{"choices":[{{"delta":{{"content":"\\n\\n> 📋 **Araç Sonucu:** {summary}\\n\\n"}}}}]}}\n\n'

                                                if isinstance(tool_res, dict):
                                                    arr = [{"type": "text", "text": "Tool Response:\n" + tool_res.get("text", "")}]
                                                    if tool_res.get("image_b64"):
                                                        arr.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tool_res['image_b64']}"}})
                                                    msgs.append({"role": "user", "content": arr})
                                                else:
                                                    msgs.append({"role": "system", "content": f"Tool response: {tool_res}"})

                                                break  # Restart while loop
                                            continue

                                        yield line + "\n\n"
                                except Exception:
                                    pass

                    if not in_tool or "</tool>" in tool_buffer:
                        if not in_tool:
                            break  # Normal exit
                    else:
                        # in_tool=True but stream ended without </tool> — incomplete tool call
                        log.warning("Webchat: stream ended with unclosed <tool> tag")
                        yield f'data: {{"choices":[{{"delta":{{"content":"\\n\\n⚠ Araç çağrısı tamamlanamadı (eksik </tool> etiketi).\\n"}}}}]}}\n\n'
                        break
                except Exception as e:
                    log.error(f"Webchat normal stream error: {e}")
                    yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"
                    break
        except GeneratorExit:
            pass

    return Response(stream_with_context(stream_conversation()),
                    content_type='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/webchat/history/<uid>')
def webchat_history_route(uid: str):
    """chat_client.py: sohbet geçmişini döndür."""
    try:
        limit = int(request.args.get('limit', 20))
        chat_id = f"web:{uid}"
        msgs = mm.get_recent_messages(chat_id, limit=limit)
        return jsonify({"ok": True, "messages": msgs})
    except Exception as e:
        return jsonify({"ok": False, "messages": [], "error": str(e)}), 500

# ── Sandbox ──────────────────────────────────────────────────

@app.route('/api/webchat/sandbox/save', methods=['POST'])
def sandbox_save():
    d = request.json or {}
    uid = d.get('uid')
    filename = d.get('filename')
    content = d.get('content')
    if not uid or not filename or content is None:
        return jsonify({"ok": False, "error": "Eksik parametreler"}), 400
    
    safe_name = werkzeug.utils.secure_filename(filename)
    if not safe_name: safe_name = f"file_{int(time.time())}.txt"
    
    user_dir = SANDBOX_DIR / werkzeug.utils.secure_filename(uid)
    user_dir.mkdir(parents=True, exist_ok=True)
    
    filepath = user_dir / safe_name
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({"ok": True, "filename": safe_name, "path": str(filepath)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/webchat/sandbox/list/<uid>')
def sandbox_list(uid):
    try:
        user_dir = SANDBOX_DIR / werkzeug.utils.secure_filename(uid)
        if not user_dir.exists():
            return jsonify({"ok": True, "files": []})
        
        files = []
        for f in user_dir.iterdir():
            if f.is_file():
                stat = f.stat()
                files.append({
                    "name": f.name,
                    "size": stat.st_size,
                    "mtime": int(stat.st_mtime)
                })
        return jsonify({"ok": True, "files": sorted(files, key=lambda x: x['mtime'], reverse=True)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/webchat/sandbox/<uid>/<filename>', methods=['DELETE'])
def sandbox_delete(uid, filename):
    try:
        user_dir = SANDBOX_DIR / werkzeug.utils.secure_filename(uid)
        filepath = user_dir / werkzeug.utils.secure_filename(filename)
        if filepath.exists() and filepath.is_file():
            filepath.unlink()
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Dosya bulunamadı"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/webchat/sandbox/download/<uid>/<filename>')
def sandbox_download(uid, filename):
    try:
        user_dir = SANDBOX_DIR / werkzeug.utils.secure_filename(uid)
        safe_name = werkzeug.utils.secure_filename(filename)
        return send_from_directory(user_dir, safe_name, as_attachment=True)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Dosya Sistemi (Uploads) ──────────────────────────────────

@app.route('/api/webchat/files/upload', methods=['POST'])
def files_upload():
    """Base64 kodlu dosyaı kullanıcı dizinine kaydet."""
    d = request.json or {}
    uid = d.get('uid', '')
    filename = d.get('filename', '')
    data_b64 = d.get('data', '')  # base64 encoded

    if not uid or not filename or not data_b64:
        return jsonify({"ok": False, "error": "uid, filename ve data zorunlu"}), 400

    safe_uid = werkzeug.utils.secure_filename(uid)
    safe_name = werkzeug.utils.secure_filename(filename)
    if not safe_name:
        safe_name = f"file_{int(time.time())}.bin"

    # Uzantı kontrolü
    ext = safe_name.rsplit('.', 1)[-1].lower() if '.' in safe_name else ''
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"ok": False, "error": f"Desteklenmeyen dosya türü: .{ext}"}), 400

    # Boyut kontrolü
    try:
        raw = b64mod.b64decode(data_b64)
    except Exception:
        return jsonify({"ok": False, "error": "Geçersiz base64 verisi"}), 400

    if len(raw) > MAX_UPLOAD_SIZE:
        return jsonify({"ok": False, "error": f"Dosya çok büyük (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)"}), 400

    # Aynı isim varsa numaralandır
    user_dir = UPLOADS_DIR / safe_uid
    user_dir.mkdir(parents=True, exist_ok=True)

    target = user_dir / safe_name
    if target.exists():
        stem = safe_name.rsplit('.', 1)[0] if '.' in safe_name else safe_name
        for i in range(1, 1000):
            candidate = f"{stem}_{i}.{ext}" if ext else f"{stem}_{i}"
            if not (user_dir / candidate).exists():
                safe_name = candidate
                target = user_dir / safe_name
                break

    try:
        with open(target, 'wb') as f:
            f.write(raw)

        is_image = ext in IMAGE_EXTENSIONS
        return jsonify({
            "ok": True,
            "filename": safe_name,
            "size": len(raw),
            "is_image": is_image,
            "url": f"/api/webchat/files/{safe_uid}/{safe_name}",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/webchat/files/list/<uid>')
def files_list(uid):
    """Kullanıcının yüklediği dosyaları listele."""
    try:
        user_dir = UPLOADS_DIR / werkzeug.utils.secure_filename(uid)
        if not user_dir.exists():
            return jsonify({"ok": True, "files": []})

        files = []
        for f in user_dir.iterdir():
            if f.is_file():
                ext = f.suffix.lstrip('.').lower()
                stat = f.stat()
                files.append({
                    "name": f.name,
                    "size": stat.st_size,
                    "mtime": int(stat.st_mtime),
                    "is_image": ext in IMAGE_EXTENSIONS,
                    "ext": ext,
                    "url": f"/api/webchat/files/{werkzeug.utils.secure_filename(uid)}/{f.name}",
                })
        return jsonify({"ok": True, "files": sorted(files, key=lambda x: x['mtime'], reverse=True)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/webchat/files/<uid>/<filename>')
def files_serve(uid, filename):
    """Dosyayı serve et — görseller inline, diğerleri download."""
    try:
        user_dir = UPLOADS_DIR / werkzeug.utils.secure_filename(uid)
        safe_name = werkzeug.utils.secure_filename(filename)
        if not (user_dir / safe_name).exists():
            return jsonify({"ok": False, "error": "Dosya bulunamadı"}), 404

        ext = safe_name.rsplit('.', 1)[-1].lower() if '.' in safe_name else ''
        as_attachment = ext not in IMAGE_EXTENSIONS
        return send_from_directory(user_dir, safe_name, as_attachment=as_attachment)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/webchat/files/<uid>/<filename>', methods=['DELETE'])
def files_delete(uid, filename):
    """Kullanıcının dosyasını sil."""
    try:
        user_dir = UPLOADS_DIR / werkzeug.utils.secure_filename(uid)
        filepath = user_dir / werkzeug.utils.secure_filename(filename)
        if filepath.exists() and filepath.is_file():
            filepath.unlink()
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Dosya bulunamadı"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Görsel ──────────────────────────────────────────────────

@app.route('/api/image/<img_hash>')
def get_image_route(img_hash: str):
    try:
        result = mm.get_image(img_hash)
        if not result:
            return jsonify({"ok": False, "error": "bulunamadı"}), 404
        b64, mime = result
        return jsonify({"ok": True, "data": b64, "mime": mime})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/image/<img_hash>/thumb')
def get_thumb_route(img_hash: str):
    try:
        thumb = mm.get_image_thumbnail(img_hash)
        if not thumb:
            return '', 404
        return send_file(io.BytesIO(thumb), mimetype='image/jpeg')
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ═══════════════════════════════════════════════════════════════
#  GÖMÜLÜ BOT_JS kaldırıldı — harici whatsapp_bot.js kullanılır.
#  Bkz: ensure_bot_file() fonksiyonu.
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
#  YARDIMCI FONKSİYONLAR
# ═══════════════════════════════════════════════════════════════

def ensure_bot_file() -> Optional[Path]:
    """whatsapp_bot.js dosyasının var olduğunu doğrula. Bulunamazsa None döner."""
    bot_path = APP_DIR / "whatsapp_bot.js"
    if not bot_path.exists():
        log.error(f"whatsapp_bot.js bulunamadı: {bot_path}")
        log.error("Bot başlatılamıyor. Dosyayı proje dizinine kopyalayın.")
        return None
    return bot_path


# ═══════════════════════════════════════════════════════════════
#  PROJE OLUŞTURUCU (create-project.sh → Python)
# ═══════════════════════════════════════════════════════════════

def create_project() -> None:
    project = Path("decentralized-ai-chat")
    print(f"🚀 Proje: {project.resolve()}")

    for d in [
        "src-tauri/src", "src-tauri/capabilities", "src-tauri/binaries",
        "src-tauri/icons", "python-core/p2p", "python-core/ai", "src"
    ]:
        (project / d).mkdir(parents=True, exist_ok=True)

    (project / "src-tauri/Cargo.toml").write_text(textwrap.dedent("""
        [package]
        name = "decentralized-ai-chat"
        version = "0.1.0"
        edition = "2021"
        [lib]
        name = "app_lib"
        crate-type = ["staticlib","cdylib","rlib"]
        [build-dependencies]
        tauri-build = { version = "2", features = [] }
        [dependencies]
        tauri = { version = "2", features = ["protocol-asset"] }
        serde = { version = "1", features = ["derive"] }
        serde_json = "1"
        [profile.release]
        codegen-units = 1; lto = true; opt-level = "s"; panic = "abort"; strip = true
    """).strip())

    (project / "src-tauri/tauri.conf.json").write_text(json.dumps({
        "productName": "P2P AI Chat", "version": "0.1.0",
        "identifier": "com.p2pai.chat",
        "build": {"frontendDist": "../src"},
        "app": {
            "windows": [{"title": "P2P AI Chat", "width": 1280, "height": 800}],
            "security": {"csp": None}
        },
        "bundle": {
            "active": True, "targets": "all",
            "icon": ["icons/32x32.png","icons/128x128.png","icons/icon.ico"]
        }
    }, indent=2))

    (project / "python-core/requirements.txt").write_text(
        "aiortc>=1.6.0\naiohttp>=3.9.0\nzeroconf>=0.131.0\nPyNaCl>=1.5.0\n"
        "flask>=3.0.0\nrequests>=2.31.0\npyinstaller>=6.3.0\n"
    )
    import shutil
    shutil.copy(__file__, project / "python-core/server.py")
    (project / ".gitignore").write_text(
        "target/\nnode_modules/\nsrc-tauri/gen/\nsrc-tauri/binaries/python-core*\n"
        "python-core/__pycache__/\npython-core/dist/\n*.pyc\n.DS_Store\n"
    )
    (project / "package.json").write_text(json.dumps({
        "name": "decentralized-ai-chat", "version": "0.1.0",
        "scripts": {"tauri": "tauri"},
        "devDependencies": {"@tauri-apps/cli": "^2.0.0"}
    }, indent=2))
    print("✅ Proje oluşturuldu.")
    print("   cd", project, "&& cargo tauri android init && cargo tauri android build --debug")


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

_cleanup_done = False

def cleanup(*_):
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    log.info("Kapatılıyor…")
    try:
        bot.stop()
    except Exception:
        pass
    with _llm_lock:
        if _llm_proc and _llm_proc.poll() is None:
            try:
                _llm_proc.terminate()
            except Exception:
                pass
    sys.exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(description="EfeMultiAIbot")
    parser.add_argument('--panel-only',     action='store_true')
    parser.add_argument('--bot-only',       action='store_true')
    parser.add_argument('--setup',          action='store_true')
    parser.add_argument('--create-project', action='store_true')
    parser.add_argument('--stats',          action='store_true')
    parser.add_argument('--maintenance',    action='store_true')
    parser.add_argument('--port',           type=int, default=5050)
    parser.add_argument('--host',           type=str, default='0.0.0.0',
                        help='Dinlenecek IP adresi (varsayılan: 0.0.0.0)')
    parser.add_argument('--gunicorn',       action='store_true',
                        help='Gunicorn ile başlat (production modu)')
    args = parser.parse_args()

    # Signal handler'ları sadece doğrudan çalıştırılınca kaydet.
    # Gunicorn kendi signal yönetimini yapar; çakışma olmasın.
    if not args.gunicorn:
        signal.signal(signal.SIGINT,  cleanup)
        signal.signal(signal.SIGTERM, cleanup)
        atexit.register(cleanup)

    if args.setup:
        pkg = APP_DIR / "package.json"
        if not pkg.exists():
            pkg.write_text(json.dumps({
                "name": "llama-whatsapp-bot", "version": "1.0.0",
                "dependencies": {
                    "whatsapp-web.js": "^1.23.0",
                    "qrcode-terminal": "^0.12.0",
                    "axios": "^1.6.0",
                    "googlethis": "^2.0.0",
                    "sqlite3": "^5.1.6"
                }
            }, indent=2))
        subprocess.run(['npm','install'], cwd=str(APP_DIR), check=True)
        print("✅ npm bağımlılıkları yüklendi.")
        return

    if args.create_project:
        create_project(); return

    if args.stats:
        print(json.dumps(mm.get_stats(), indent=2, ensure_ascii=False)); return

    if args.maintenance:
        report = mm.run_maintenance()
        print(json.dumps(report, indent=2, ensure_ascii=False)); return

    if args.bot_only:
        if not ensure_bot_file():
            sys.exit(1)
        bot.start()
        bot.watch()
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            bot.stop()
        return

    # Panel modu
    if not args.panel_only:
        if ensure_bot_file():
            bot.start()
            bot.watch()
        else:
            log.warning("Bot dosyası bulunamadı. Sadece panel modu başlatılıyor.")

    if args.gunicorn:
        # Gunicorn ile başlat
        print("╔══════════════════════════════════════════════════════╗")
        print("║   🤖 EfeMultiAIbot (Gunicorn)                        ║")
        print(f"║   → http://localhost:{args.port}                         ║")
        print("╚══════════════════════════════════════════════════════╝")
        os.execvp('gunicorn', [
            'gunicorn', 'app:app',
            '-c', str(APP_DIR / 'gunicorn.conf.py'),
            '--bind', f'{args.host}:{args.port}',
        ])
    else:
        print("╔══════════════════════════════════════════════════════╗")
        print("║   🤖 EfeMultiAIbot                                   ║")
        print(f"║   → http://localhost:{args.port}                         ║")
        print("║   💡 Production için: python app.py --gunicorn       ║")
        print("╚══════════════════════════════════════════════════════╝")
        app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == '__main__':
    main()

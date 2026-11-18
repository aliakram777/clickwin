"""MemoryStore (Mərhələ 18.3). Dörd növ: SHORT_TERM (TTL), LONG_TERM (fakt/qərar), EPISODIC (hadisə izi), PROJECT (layihə yaddaşı).

Qaydalar (Stage 11 qaydaları qorunur):
- Yazma səlahiyyəti aktora görədir: OWNER hər növü; SYSTEM hər növü (mənşə system/external); ENGINE (nüvə döngüsü) yalnız EPISODIC; AGENT yalnız SHORT_TERM/PROJECT (mənşə agent, ETİBARSIZ);
  LLM yaddaşı DƏYİŞƏ BİLMƏZ (yalnız oxuma). Silmə yalnız OWNER; vaxtı bitmiş SHORT_TERM təmizlənməsi sistem baxımıdır.
- Sirr bənzər məzmun rədd edilir (dəyər xəta mətninə yazılmır). Mətn ETİBARSIZDIR: LLM/agentə yalnız `render_context` ilə escape olunmuş məlumat bloku kimi verilir, heç vaxt təlimat kimi.
- Mənşə (`origin`), etibar (`trust`: high/medium/low), mənbə və etibar dərəcəsi (`confidence`) hər qeydlə saxlanır; axtarışda `min_trust` ilə süzülür.
- İdempotentlik: eyni `op_id` + eyni məzmun = dublikat; eyni (növ, layihə, məzmun hash-i) = dublikat (yeni sətir yoxdur).
- Axtarış: FTS5 (diakritik qatlama: ö≈o, ü≈u, ç≈c, ş≈s; ə ayrıca qalır), istifadəçi sorğusu escape olunur (FTS sintaksisi enjeksiyası yoxdur); FTS5 yoxdursa LIKE ehtiyatı.
HƏDD: semantik (embedding) axtarış yoxdur; köhnə `agents/memory.MemoryService` ilə miqrasiya yoxdur (ayrı saxlanc)."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Sequence

from kernel.common import ContractError, Origin, canonical_json, clean_text, fmt_dt, parse_dt, render_untrusted
from safety.dbconn import LockedConnection

_OP = re.compile(r"^[a-z0-9][a-z0-9_\-:.]{2,95}$")
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,47}$")
_ID = re.compile(r"^mem_[0-9a-f]{16}$")
SHORT_TTL_DEFAULT_S = 24 * 3600
SHORT_TTL_MAX_S = 7 * 24 * 3600
MAX_CONTENT, MAX_TITLE, MAX_TAGS, MAX_LIMIT = 4000, 200, 8, 50


class MemoryKind(str, Enum):
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"
    EPISODIC = "episodic"
    PROJECT = "project"


class MemActor(str, Enum):
    OWNER = "owner"
    SYSTEM = "system"
    ENGINE = "engine"
    AGENT = "agent"
    LLM = "llm"


_WRITE_KINDS = {MemActor.OWNER: set(MemoryKind), MemActor.SYSTEM: set(MemoryKind), MemActor.ENGINE: {MemoryKind.EPISODIC},
                MemActor.AGENT: {MemoryKind.SHORT_TERM, MemoryKind.PROJECT}, MemActor.LLM: set()}
_ACTOR_ORIGINS = {MemActor.OWNER: {Origin.OWNER}, MemActor.SYSTEM: {Origin.SYSTEM, Origin.EXTERNAL}, MemActor.ENGINE: {Origin.SYSTEM}, MemActor.AGENT: {Origin.AGENT}, MemActor.LLM: set()}
_DEFAULT_ORIGIN = {MemActor.OWNER: Origin.OWNER, MemActor.SYSTEM: Origin.SYSTEM, MemActor.ENGINE: Origin.SYSTEM, MemActor.AGENT: Origin.AGENT}
_TRUST_RANK = {"low": 0, "medium": 1, "high": 2}


class MemoryDenied(ContractError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "memory_denied")


def trust_of(origin: Origin) -> str:
    return "high" if origin is Origin.OWNER else "medium" if origin is Origin.SYSTEM else "low"


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    kind: MemoryKind
    project: str
    title: str
    content: str
    origin: Origin
    trust: str
    source: str
    confidence: float
    tags: tuple[str, ...]
    refs: tuple[str, ...]
    created_at: datetime
    expires_at: datetime | None

    @property
    def untrusted(self) -> bool:
        return self.origin.untrusted


@dataclass(frozen=True)
class WriteResult:
    record: MemoryRecord
    duplicate: bool


@dataclass(frozen=True)
class MemoryHit:
    record: MemoryRecord
    score: float


_DDL = """
CREATE TABLE IF NOT EXISTS memory (rowid INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, project TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
    origin TEXT NOT NULL, trust TEXT NOT NULL, source TEXT NOT NULL, confidence REAL NOT NULL, tags TEXT NOT NULL, refs TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT,
    content_hash TEXT NOT NULL, op_id TEXT UNIQUE, deleted INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_dedupe ON memory(kind, project, content_hash) WHERE deleted = 0;
CREATE INDEX IF NOT EXISTS idx_memory_project ON memory(project, kind, created_at);
"""


def _clean_tags(tags: Sequence[str]) -> tuple[str, ...]:
    if isinstance(tags, (str, bytes)) or len(tags) > MAX_TAGS:
        raise ContractError("tags: ən çoxu 8 etiketli siyahı", "bad_tags")
    out = tuple(dict.fromkeys(clean_text("tag", t, max_len=32, min_len=1) for t in tags))
    return out


def _fts_query(query: str, mode: str) -> str:
    tokens = [t for t in re.findall(r"[^\W_]+", query, flags=re.UNICODE)][:8]
    if not tokens:
        return ""
    quoted = [f'"{t.replace(chr(34), "")}"*' for t in tokens]
    return (" OR " if mode == "any" else " ").join(quoted)


class MemoryStore:
    def __init__(self, conn: LockedConnection, clock: Callable[[], datetime] | None = None) -> None:
        self._db, self._clock, self._lock = conn, clock or (lambda: datetime.now(timezone.utc)), threading.Lock()
        with conn.transaction():
            for stmt in [s.strip() for s in _DDL.split(";\n") if s.strip()]:
                conn.execute(stmt)
        self.fts = True
        try:
            with conn.transaction():
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(title, content, tags)")
        except Exception:                                      # noqa: BLE001  FTS5 yoxdur: LIKE ehtiyatı
            self.fts = False

    def now(self) -> datetime:
        t = self._clock()
        if t.tzinfo is None:
            raise ContractError("saat timezone-aware olmalıdır", "naive_datetime")
        return t.astimezone(timezone.utc)

    # ------------------------------------------------------------------ yazma
    def write(self, actor: MemActor, kind: MemoryKind, content: str, *, title: str = "", project: str = "default", tags: Sequence[str] = (), source: str = "",
              confidence: float = 1.0, refs: Sequence[str] = (), ttl_s: float | None = None, op_id: str | None = None, origin: Origin | None = None) -> WriteResult:
        actor, kind = MemActor(actor), MemoryKind(kind)
        if kind not in _WRITE_KINDS[actor]:
            raise MemoryDenied(f"{actor.value} aktoru {kind.value} yaddaşına yaza bilməz" if actor is not MemActor.LLM else "LLM yaddaşı dəyişə bilməz")
        origin = Origin(origin) if origin is not None else _DEFAULT_ORIGIN[actor]
        if origin not in _ACTOR_ORIGINS[actor]:
            raise MemoryDenied(f"{actor.value} aktoru {origin.value} mənşəli yaza bilməz")
        if not _PROJECT.match(project or ""):
            raise ContractError("project: kiçik hərf/rəqəm/_- (1-48)", "bad_project")
        if op_id is not None and not _OP.match(op_id):
            raise ContractError("op_id yanlışdır", "bad_op_id")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
            raise ContractError("confidence 0..1 olmalıdır", "bad_confidence")
        content = clean_text("content", content, max_len=MAX_CONTENT, min_len=1, multiline=True)            # sirr rədd, NFC, idarəedici simvol rədd
        title = clean_text("title", title, max_len=MAX_TITLE) if title else content[:60].replace("\n", " ")
        source = clean_text("source", source, max_len=120) if source else ""
        tags_t = _clean_tags(tags)
        refs_t = tuple(clean_text("ref", r, max_len=64, min_len=1) for r in list(refs)[:20])
        now = self.now()
        expires = None
        if kind is MemoryKind.SHORT_TERM:
            ttl = SHORT_TTL_DEFAULT_S if ttl_s is None else ttl_s
            if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 1 <= ttl <= SHORT_TTL_MAX_S:
                raise ContractError("ttl_s 1..604800 olmalıdır", "bad_ttl")
            expires = now + timedelta(seconds=float(ttl))
        elif ttl_s is not None:
            raise ContractError("ttl yalnız short_term üçündür", "bad_ttl")
        chash = hashlib.sha256(canonical_json({"c": content, "t": title}).encode()).hexdigest()
        with self._lock, self._db.transaction():
            if op_id:
                row = self._db.execute("SELECT id, content_hash, kind, project FROM memory WHERE op_id = ?", (op_id,)).fetchone()
                if row:
                    if (row[1], row[2], row[3]) != (chash, kind.value, project):
                        raise ContractError("op_id artıq FƏRQLİ məzmunla istifadə olunub", "op_conflict")
                    return WriteResult(self._get(row[0]), True)
            row = self._db.execute("SELECT id FROM memory WHERE kind = ? AND project = ? AND content_hash = ? AND deleted = 0", (kind.value, project, chash)).fetchone()
            if row:
                return WriteResult(self._get(row[0]), True)
            mid = "mem_" + uuid.uuid4().hex[:16]
            cur = self._db.execute("INSERT INTO memory (id, kind, project, title, content, origin, trust, source, confidence, tags, refs, created_at, expires_at, content_hash, op_id) "
                                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (mid, kind.value, project, title, content, origin.value, trust_of(origin), source, float(confidence), json.dumps(list(tags_t)), json.dumps(list(refs_t)),
                                    fmt_dt(now), fmt_dt(expires) if expires else None, chash, op_id))
            if self.fts:
                self._db.execute("INSERT INTO memory_fts (rowid, title, content, tags) VALUES (?,?,?,?)", (cur.lastrowid, title, content, " ".join(tags_t)))
            return WriteResult(self._get(mid), False)

    # ------------------------------------------------------------------ oxuma
    def _rec(self, r: Any) -> MemoryRecord:
        return MemoryRecord(r[0], MemoryKind(r[1]), r[2], r[3], r[4], Origin(r[5]), r[6], r[7], float(r[8]), tuple(json.loads(r[9])), tuple(json.loads(r[10])), parse_dt("created_at", r[11]),
                            parse_dt("expires_at", r[12]) if r[12] else None)

    _COLS = "id, kind, project, title, content, origin, trust, source, confidence, tags, refs, created_at, expires_at"

    def _get(self, mid: str) -> MemoryRecord:
        return self._rec(self._db.execute(f"SELECT {self._COLS} FROM memory WHERE id = ?", (mid,)).fetchone())

    def get(self, memory_id: str) -> MemoryRecord | None:
        if not isinstance(memory_id, str) or not _ID.match(memory_id):
            return None
        row = self._db.execute(f"SELECT {self._COLS}, deleted FROM memory WHERE id = ?", (memory_id,)).fetchone()
        if row is None or row[13] or (row[12] and parse_dt("e", row[12]) <= self.now()):
            return None
        return self._rec(row[:13])

    def search(self, query: str = "", *, kinds: Sequence[MemoryKind] | None = None, project: str | None = None, min_trust: str | None = None, mode: str = "all",
               limit: int = 10) -> list[MemoryHit]:
        if not isinstance(query, str):
            raise ContractError("query mətn olmalıdır", "bad_query")
        lim = max(1, min(int(limit), MAX_LIMIT))
        where, params = ["m.deleted = 0", "(m.expires_at IS NULL OR m.expires_at > ?)"], [fmt_dt(self.now())]
        if kinds:
            where.append(f"m.kind IN ({','.join('?' * len(kinds))})")
            params += [MemoryKind(k).value for k in kinds]
        if project:
            where.append("m.project = ?")
            params.append(project)
        if min_trust:
            if min_trust not in _TRUST_RANK:
                raise ContractError("min_trust: low|medium|high", "bad_trust")
            allowed = [t for t, r in _TRUST_RANK.items() if r >= _TRUST_RANK[min_trust]]
            where.append(f"m.trust IN ({','.join('?' * len(allowed))})")
            params += allowed
        fq = _fts_query(query, mode)
        if query.strip() and not fq:
            return []
        if fq and self.fts:
            sql = (f"SELECT {', '.join('m.' + c.strip() for c in self._COLS.split(','))}, bm25(memory_fts) FROM memory_fts JOIN memory m ON m.rowid = memory_fts.rowid "
                   f"WHERE memory_fts MATCH ? AND {' AND '.join(where)} ORDER BY bm25(memory_fts), m.created_at DESC LIMIT ?")
            rows = self._db.execute(sql, (fq, *params, lim)).fetchall()
            return [MemoryHit(self._rec(r[:13]), -float(r[13])) for r in rows]
        if fq:                                                 # LIKE ehtiyatı
            terms = re.findall(r"[^\W_]+", query, flags=re.UNICODE)[:8]
            where.append("(" + (" OR " if mode == "any" else " AND ").join("(m.content LIKE ? OR m.title LIKE ?)" for _ in terms) + ")")
            for t in terms:
                params += [f"%{t}%", f"%{t}%"]
        rows = self._db.execute(f"SELECT {', '.join('m.' + c.strip() for c in self._COLS.split(','))} FROM memory m WHERE {' AND '.join(where)} ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?",
                                (*params, lim)).fetchall()
        return [MemoryHit(self._rec(r), 0.0) for r in rows]

    # ------------------------------------------------------------------ silmə / baxım
    def delete(self, actor: MemActor, memory_id: str) -> bool:
        if MemActor(actor) is not MemActor.OWNER:
            raise MemoryDenied("yaddaşı yalnız sahib silə bilər")
        with self._lock, self._db.transaction():
            row = self._db.execute("SELECT rowid FROM memory WHERE id = ? AND deleted = 0", (memory_id,)).fetchone()
            if row is None:
                return False
            self._db.execute("UPDATE memory SET deleted = 1, content = '', title = '', tags = '[]' WHERE id = ?", (memory_id,))      # məzmun silinir (iz: sətir qalır)
            if self.fts:
                self._db.execute("DELETE FROM memory_fts WHERE rowid = ?", (row[0],))
            return True

    def purge_expired(self) -> int:
        """Vaxtı bitmiş SHORT_TERM qeydlərin məzmunu təmizlənir (sistem baxımı; başqa növə toxunmur)."""
        n = 0
        with self._lock, self._db.transaction():
            for rid, mid in self._db.execute("SELECT rowid, id FROM memory WHERE kind = ? AND deleted = 0 AND expires_at IS NOT NULL AND expires_at <= ?",
                                             (MemoryKind.SHORT_TERM.value, fmt_dt(self.now()))).fetchall():
                self._db.execute("UPDATE memory SET deleted = 1, content = '', title = '', tags = '[]' WHERE id = ?", (mid,))
                if self.fts:
                    self._db.execute("DELETE FROM memory_fts WHERE rowid = ?", (rid,))
                n += 1
        return n

    def consolidate_episodes(self, actor: MemActor, *, project: str, subject_ref: str, op_id: str, limit: int = 50) -> WriteResult | None:
        """Epizodları (eyni `refs` mövzusu) QISA deterministik xülasəyə çevirir: LONG_TERM, mənşə system, mənbə epizod id-ləri `refs`-də. Orijinal epizodlar SAXLANILIR. Model istifadə etmir."""
        if MemActor(actor) not in (MemActor.OWNER, MemActor.SYSTEM):
            raise MemoryDenied("konsolidasiya yalnız sahib/sistem")
        rows = self._db.execute(f"SELECT {self._COLS} FROM memory WHERE kind = ? AND project = ? AND deleted = 0 AND refs LIKE ? ORDER BY created_at, rowid LIMIT ?",
                                (MemoryKind.EPISODIC.value, project, f"%{subject_ref}%", max(1, min(limit, 200)))).fetchall()
        recs = [self._rec(r) for r in rows if subject_ref in json.loads(r[10])]
        if not recs:
            return None
        counts: dict[str, int] = {}
        for r in recs:
            counts[r.tags[0] if r.tags else "event"] = counts.get(r.tags[0] if r.tags else "event", 0) + 1
        text = f"{subject_ref}: {len(recs)} epizod ({recs[0].created_at.date()} – {recs[-1].created_at.date()}); " + ", ".join(f"{k}×{v}" for k, v in sorted(counts.items()))
        return self.write(MemActor.SYSTEM if actor is MemActor.SYSTEM else MemActor.OWNER, MemoryKind.LONG_TERM, text, title=f"Xülasə: {subject_ref}"[:200], project=project,
                          tags=("consolidated",), source="consolidation", confidence=0.9, refs=tuple(r.memory_id for r in recs[:20]),
                          op_id=op_id, origin=Origin.SYSTEM if actor is MemActor.SYSTEM else Origin.OWNER)

    def stats(self) -> dict[str, int]:
        return {k: int(n) for k, n in self._db.execute("SELECT kind, COUNT(*) FROM memory WHERE deleted = 0 GROUP BY kind").fetchall()}


def render_context(hits: Sequence[MemoryHit], *, per_record: int = 500, max_chars: int = 4000) -> str:
    """Yaddaş mətni ETİBARSIZ məlumatdır: hər qeyd escape olunmuş blokdadır (mənşə/etibar göstərilir), heç vaxt təlimat kimi deyil."""
    out, used = [], 0
    for h in hits:
        r = h.record
        block = render_untrusted("memory", f"[{r.kind.value}|{r.origin.value}|etibar={r.trust}|{r.memory_id}] {r.content[:per_record]}", limit=per_record + 120)
        if used + len(block) > max_chars:
            break
        out.append(block)
        used += len(block)
    return "\n".join(out)

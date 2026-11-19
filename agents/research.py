"""İnternet araşdırması: mənbələrin yoxlanması, müqayisəsi, Azərbaycan dilində izah və yaddaşa yazma.

Prinsiplər:
- İnternet məzmunu ETİBARSIZ məlumatdır: təmizlənir, təlimat bənzər mətn iddialara daxil edilmir, heç bir səhifənin
  göstərişi icra olunmur. Bu modul yalnız mətn işləyir; şəbəkə/shell/eval yoxdur.
- Alət çağırışları (axtarış/oxuma) YALNIZ Tool Runtime -> Effect Broker üzərindən gedir (allowlist, Gate, büdcə, audit,
  stop). Canlı (network) alətlər defoltda bağlıdır; real şəbəkə üçün təsdiqlənmiş OS sandbox adapteri lazımdır.
- Mənbəsiz məlumat "təsdiqlənmiş fakt" kimi təqdim olunmur: `supported` status ən azı 2 müstəqil etibarlı mənbə
  tələb edir və bu qayda `Claim` tipinin özündə tətbiq olunur. Etibarlılıq göstəricisi HEURİSTİKDİR, həqiqət təsdiqi deyil.
- Mock və canlı nəticələr ayrıdır (`provenance`, `is_mock`); mock nəticələr hər yerdə "SİMULYASİYA" kimi işarələnir.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from agents.memory import Actor, MemoryDenied, MemoryService, WriteResult, detect_secrets
from safety.webtools import MockPage, MockWeb, live_tool_specs  # noqa: F401  (yenidən ixrac)

RESEARCH_ACTOR = Actor("research", "system")
STATUSES = ("conflicting", "supported", "partial_support", "single_source", "low_reliability")
STATUS_LABEL = {
    "supported": "Çoxmənbəli dəstək",
    "partial_support": "Qismən dəstək",
    "single_source": "Tək mənbə — təsdiqlənməyib",
    "low_reliability": "Aşağı etibarlılıq — təsdiqlənməyib",
    "conflicting": "Ziddiyyət — təsdiqlənməyib",
}
MULTI_SUFFIXES = frozenset({"gov.az", "edu.az", "com.az", "org.az", "net.az", "co.uk", "ac.uk", "gov.uk", "com.tr",
                            "gov.tr", "edu.tr", "co.jp"})
_BASE_TIER = {"primary": 0.85, "reputable": 0.70, "unknown": 0.40}
_STOP = frozenset({"the", "and", "for", "with", "that", "this", "are", "was", "və", "ilə", "bir", "bəzi"})
_INJECTION = [re.compile(p) for p in (
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)", r"disregard\s+(?:the\s+|all\s+)?(?:system|previous|instructions)",
    r"system\s+prompt", r"you\s+are\s+now", r"reveal\s+(?:your|the)\s+(?:api|key|secret|prompt)",
    r"(?:send|give|show)\s+(?:me\s+)?(?:the\s+|your\s+)?(?:api\s+key|password|secret)",
    r"\bnew\s+instructions\b", r"\bexecute\s+the\s+following\b", r"run\s+(?:this|the\s+following)\s+command",
    r"\brm\s+-rf\b", r"\bcurl\s+https?:", r"\|\s*(?:sh|bash)\b", r"<\s*script",
    r"[əe]vv[əe]lki\s+t[əe]limat", r"qaydalar[ıi]\s+unut", r"\bindi\s+s[əe]n\b", r"t[əe]limatlar[ıi]\s+unut",
    r"gizli\s+a[cç]ar[ıi]?\s+g[öo]nd[əe]r",
)]


class UnsafeURL(ValueError):
    pass


# --- yardımçılar ---

def _norm(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def validate_url(url: Any) -> str:
    """URL-i yoxlayır (SSRF/sızma qorunması): yalnız http/https, DNS adı, istifadəçi məlumatı və IP-literal yoxdur."""
    if not isinstance(url, str) or not url or len(url) > 2048 or re.search(r"[\s\x00-\x1f\x7f\\]", url):
        raise UnsafeURL("URL yanlışdır")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeURL("URL parse olunmur") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURL("yalnız http/https və host adı olan URL-lər")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURL("URL-də istifadəçi məlumatı olmamalıdır")
    if port not in (None, 80, 443):
        raise UnsafeURL("qeyri-standart port")
    host = parts.hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(host.strip("[]"))
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip:
        raise UnsafeURL("IP ünvanı qadağandır")
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal", ".lan", ".intranet")):
        raise UnsafeURL("daxili host qadağandır")
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or not all(re.fullmatch(r"[a-z0-9\u0080-￿](?:[a-z0-9\-\u0080-￿]{0,61}[a-z0-9\u0080-￿])?", l) for l in labels):
        raise UnsafeURL("host adı yanlışdır")
    netloc = host + (f":{port}" if port in (80, 443) and not (port == 443 and parts.scheme == "https")
                     and not (port == 80 and parts.scheme == "http") else "")
    return f"{parts.scheme}://{netloc}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")


def registrable_domain(host: str) -> str:
    labels = host.lower().rstrip(".").split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


_TAG_BLOCKS = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>")
_TAGS = re.compile(r"<[^>]*>")


def sanitize_page_text(text: Any, cap: int = 20000) -> str:
    """Veb mətnini etibarsız məzmun kimi təmizləyir: HTML/skript, idarəedici simvollar, artıq boşluq, ölçü."""
    if not isinstance(text, str):
        return ""
    t = _TAGS.sub(" ", _TAG_BLOCKS.sub(" ", text))
    t = "".join(" " if unicodedata.category(c).startswith("C") else c for c in t)
    return " ".join(t.split())[:cap]


def detect_injection(text: str) -> list[str]:
    """Təlimat bənzər mətn əlamətləri (HEURİSTİK). Aşkarlansa məzmun yenə məlumat sayılır, amma etibarı azaldılır."""
    norm = _norm(text)
    return [p.pattern for p in _INJECTION if p.search(norm)]


# --- məlumat tipləri ---

@dataclass(frozen=True)
class FetchedPage:
    url: str
    title: str
    text: str
    published: str | None
    provenance: str  # "mock" | "live"
    retrieved_at: float = 0.0


@dataclass(frozen=True)
class SourcePolicy:
    primary_suffixes: frozenset[str] = frozenset({"gov.az", "edu.az", "gov", "edu", "int", "europa.eu", "un.org"})
    reputable_domains: frozenset[str] = frozenset()  # sahib tərəfindən müəyyən edilir
    blocked_domains: frozenset[str] = frozenset()
    min_support_reliability: float = 0.5
    max_age_days: int = 365


@dataclass(frozen=True)
class SourceInfo:
    id: int
    url: str
    domain: str
    title: str
    published: date | None
    age_days: int | None
    tier: str
    reliability: float  # HEURİSTİK göstərici (0..1), həqiqətin təsdiqi deyil
    flags: tuple[str, ...]
    provenance: str
    retrieved_at: float


@dataclass(frozen=True)
class Claim:
    id: int
    text: str
    status: str
    source_ids: tuple[int, ...]
    domains: tuple[str, ...]
    values: Mapping[int, tuple[float, ...]]
    reliability: float

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"naməlum status: {self.status}")
        if not self.source_ids or len(self.domains) != len(self.source_ids):
            raise ValueError("iddia mənbəsiz ola bilməz")
        if self.status in ("supported", "partial_support") and len(set(self.domains)) < 2:
            raise ValueError(f"{self.status} üçün ən azı 2 müstəqil domen tələb olunur")
        if self.status == "supported" and len(self.source_ids) < 2:
            raise ValueError("supported üçün ən azı 2 mənbə tələb olunur")


@dataclass(frozen=True)
class ResearchReport:
    query: str
    status: str  # ok | no_sources | no_claims | unavailable | refused
    reason: str
    is_mock: bool
    sources: tuple[SourceInfo, ...]
    claims: tuple[Claim, ...]
    warnings: tuple[str, ...]
    created_at: float


# --- hesabat qurulması (saf funksiya) ---

def _tier(domain: str, host: str, policy: SourcePolicy) -> str:
    if domain in policy.blocked_domains or host in policy.blocked_domains:
        return "blocked"
    if any(host == s or host.endswith("." + s) for s in policy.primary_suffixes):
        return "primary"
    if domain in policy.reputable_domains or host in policy.reputable_domains:
        return "reputable"
    return "unknown"


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"\w+", _norm(text)) if len(w) >= 3 and not w.isdigit() and w not in _STOP}


def _numbers(sentence: str) -> tuple[float, ...]:
    out = []
    for m in re.findall(r"\d+(?:[.,]\d+)?", sentence):
        v = float(m.replace(",", "."))
        if "." not in m and "," not in m and 1900 <= v <= 2100:
            continue  # il kimi görünən tam ədədlər dəyər sayılmır
        out.append(v)
    return tuple(out)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if 20 <= len(s.strip()) <= 300]


def build_report(query: str, pages: Sequence[FetchedPage], *, policy: SourcePolicy | None = None,
                 now: float | None = None, max_age_days: int | None = None,
                 warnings: Sequence[str] = ()) -> ResearchReport:
    policy = policy or SourcePolicy()
    now = time.time() if now is None else now
    max_age = policy.max_age_days if max_age_days is None else max_age_days
    today = datetime.fromtimestamp(now, timezone.utc).date()
    warn: list[str] = list(warnings)
    sources: list[SourceInfo] = []
    texts: dict[int, str] = {}
    seen: set[str] = set()

    for p in pages:
        try:
            url = validate_url(p.url)
        except UnsafeURL:
            warn.append(f"etibarsız URL atıldı: {sanitize_page_text(str(p.url), 60)}")
            continue
        if url in seen:
            warn.append(f"təkrar mənbə atıldı: {urlsplit(url).hostname}")
            continue
        seen.add(url)
        host = urlsplit(url).hostname.lower()
        domain = registrable_domain(host)
        tier = _tier(domain, host, policy)
        if tier == "blocked":
            warn.append(f"bloklanmış mənbə atıldı: {domain}")
            continue
        text = sanitize_page_text(p.text)
        flags: list[str] = []
        score = _BASE_TIER[tier]
        if urlsplit(url).scheme == "http":
            flags.append("http")
            score -= 0.10
        published = parse_date(p.published)
        age = None
        if published is None:
            flags.append("no_date")
            score -= 0.10
        elif published > today:
            flags.append("future_date")
            score -= 0.15
            published = None
        else:
            age = (today - published).days
            score += 0.05
            if age > max_age:
                flags.append("stale")
                score -= 0.20
        if len(text) < 40:
            flags.append("short_text")
            score -= 0.10
        if detect_injection(text):
            flags.append("injection_suspect")
            score -= 0.30
        if p.provenance != "live":
            flags.append("mock")
        sid = len(sources) + 1
        sources.append(SourceInfo(sid, url, domain, sanitize_page_text(p.title, 200), published, age, tier,
                                  round(min(1.0, max(0.0, score)), 4), tuple(flags),
                                  "live" if p.provenance == "live" else "mock", p.retrieved_at))
        texts[sid] = text

    is_mock = any(s.provenance == "mock" for s in sources)
    if is_mock and any(s.provenance == "live" for s in sources):
        warn.append("mock və real nəticələr qarışıqdır: hesabat mock elan olunur")
    elif is_mock:
        warn.append("mock nəticələr: real internet nəticəsi deyil (simulyasiya)")
    for s in sources:
        label = f"[{s.id}] {s.domain}"
        if "http" in s.flags:
            warn.append(f"{label}: şifrəsiz (http) bağlantı")
        if "stale" in s.flags:
            warn.append(f"{label}: məlumat köhnədir ({s.age_days} gün)")
        if "no_date" in s.flags:
            warn.append(f"{label}: nəşr tarixi göstərilməyib")
        if "future_date" in s.flags:
            warn.append(f"{label}: tarix gələcəkdədir (etibarsız)")
        if "injection_suspect" in s.flags:
            warn.append(f"{label}: səhifədə təlimat bənzər məzmun aşkarlandı (məlumat kimi qəbul edildi, icra olunmadı)")
        if "short_text" in s.flags:
            warn.append(f"{label}: mətn çox qısadır")
    if 0 < len(sources) < 2:
        warn.append("bir neçə mənbə müqayisə edilə bilmədi (yalnız bir mənbə)")

    q_stems = _stems(query)
    need = 1 if len(q_stems) <= 2 else 2
    clusters: list[dict[str, Any]] = []
    for s in sources:
        taken = 0
        for sent in _sentences(texts[s.id]):
            if detect_injection(sent):
                continue
            if detect_secrets(sent):
                warn.append(f"[{s.id}] {s.domain}: sirr bənzər məzmun atıldı")
                continue
            st = _stems(sent)
            if len(st & q_stems) < need or taken >= 5:
                continue
            taken += 1
            nums = _numbers(sent)
            for c in clusters:
                union = c["stems"] | st
                if s.id not in c["by_source"] and union and len(c["stems"] & st) / len(union) >= 0.5:
                    c["by_source"][s.id] = (sent, nums)
                    break
            else:
                clusters.append({"stems": st, "by_source": {s.id: (sent, nums)}})

    by_id = {s.id: s for s in sources}
    claims: list[Claim] = []
    for c in clusters:
        ids = tuple(sorted(c["by_source"]))
        doms = tuple(by_id[i].domain for i in ids)
        reliable = {by_id[i].domain for i in ids if by_id[i].reliability >= policy.min_support_reliability}
        distinct = set(doms)
        with_nums = {i: tuple(sorted(round(v, 3) for v in c["by_source"][i][1])) for i in ids if c["by_source"][i][1]}
        conflict = len(set(with_nums.values())) > 1
        if conflict:
            status = "conflicting"
        elif len(reliable) >= 2:
            status = "supported"
        elif len(distinct) >= 2 and len(reliable) >= 1:
            status = "partial_support"
        elif len(reliable) == 0:
            status = "low_reliability"
        else:
            status = "single_source"
        best = max(ids, key=lambda i: (by_id[i].reliability, -i))
        claims.append(Claim(0, c["by_source"][best][0], status, ids, doms, {i: v for i, v in with_nums.items()},
                            max(by_id[i].reliability for i in ids)))
    rank = {s: i for i, s in enumerate(STATUSES)}
    claims.sort(key=lambda cl: (rank[cl.status], -cl.reliability, cl.text))
    claims = [Claim(i + 1, cl.text, cl.status, cl.source_ids, cl.domains, cl.values, cl.reliability)
              for i, cl in enumerate(claims)]
    for cl in claims:
        if cl.status == "conflicting":
            warn.append(f"iddia #{cl.id} üzrə mənbələr arasında ziddiyyət var")
    status = "no_sources" if not sources else ("ok" if claims else "no_claims")
    return ResearchReport(sanitize_page_text(query, 200), status, "", is_mock, tuple(sources), tuple(claims),
                          tuple(dict.fromkeys(warn)), now)


# --- Azərbaycan dilində izah ---

def render_az(report: ResearchReport) -> str:
    lines = [f"Araşdırma: {report.query}"]
    if report.is_mock:
        lines.append("SİMULYASİYA: nəticələr mock mənbələrdəndir və real internet nəticəsi deyil.")
    if report.status in ("unavailable", "refused"):
        what = "əlçatan deyil" if report.status == "unavailable" else "rədd edildi"
        lines.append(f"Araşdırma {what}: {report.reason}")
        return "\n".join(lines)
    if report.status == "no_sources":
        lines.append("mənbə tapılmadı.")
    elif report.status == "no_claims":
        lines.append("Mənbələr tapıldı, lakin sorğuya aid iddia çıxarıla bilmədi.")
    if report.claims:
        lines.append("Nəticələr:")
        by_id = {s.id: s for s in report.sources}
        for c in report.claims:
            cites = ", ".join(f"[{i}] {by_id[i].domain}" for i in c.source_ids)
            extra = ""
            if c.status == "conflicting":
                extra = " Dəyərlər: " + "; ".join(f"[{i}]={'/'.join(str(x) for x in v)}" for i, v in c.values.items()) + "."
            lines.append(f"{c.id}. [{STATUS_LABEL[c.status]}] {c.text} (mənbələr: {cites}){extra}")
    if report.sources:
        lines.append("Mənbələr:")
        for s in report.sources:
            when = f"{s.published.isoformat()} ({s.age_days} gün əvvəl)" if s.published else "göstərilməyib"
            lines.append(f"[{s.id}] {s.domain} — {s.title or 'başlıqsız'} — tarix: {when} — "
                         f"etibarlılıq göstəricisi {s.reliability:.2f} (heuristik)")
    if report.warnings:
        lines.append("Xəbərdarlıqlar:")
        lines += [f"- {w}" for w in report.warnings]
    lines.append("Qeyd: etibarlılıq göstəricisi heuristikdir və həqiqətin təsdiqi deyil. Tək mənbəli, qismən "
                 "dəstəklənən və ziddiyyətli məlumatlar təsdiqlənmiş fakt kimi qəbul edilməməlidir.")
    return "\n".join(lines)


# --- xidmət: Tool Runtime + Effect Broker üzərindən ---

def _report_stub(query: str, status: str, reason: str, now: float, is_mock: bool = True) -> ResearchReport:
    return ResearchReport(sanitize_page_text(str(query), 200), status, reason, is_mock, (), (), (), now)


class ResearchService:
    """`runtime`: `call(agent_spec, tool, args)` metodu olan Tool Runtime. Bütün çağırışlar broker-dən keçir."""

    def __init__(self, runtime: Any, agent: Any, *, policy: SourcePolicy | None = None,
                 clock: Callable[[], float] = time.time, max_sources: int = 5, search_tool: str = "web.search",
                 fetch_tool: str = "web.fetch") -> None:
        if isinstance(max_sources, bool) or not isinstance(max_sources, int) or not 1 <= max_sources <= 10:
            raise ValueError("max_sources 1..10 olmalıdır")
        self.runtime, self.agent = runtime, agent
        self.policy = policy or SourcePolicy()
        self.clock, self.max_sources = clock, max_sources
        self.search_tool, self.fetch_tool = search_tool, fetch_tool

    def research(self, query: Any, *, max_age_days: int | None = None) -> ResearchReport:
        now = self.clock()
        q = query.strip() if isinstance(query, str) else ""
        if not q or len(q) > 200 or any(unicodedata.category(c).startswith("C") for c in q):
            return _report_stub(query if isinstance(query, str) else "", "refused",
                                "sorğu boş, çox uzun və ya yanlış simvollar ehtiva edir", now)
        if detect_secrets(q):
            return _report_stub("[gizli]", "refused", "sorğu sirr bənzər məzmun ehtiva edir və xarici axtarışa göndərilmir", now)

        res = self.runtime.call(self.agent, self.search_tool, {"query": q, "limit": self.max_sources * 2})
        if res.status != "ok":
            return _report_stub(q, "unavailable", res.reason or res.status, now)
        warn: list[str] = []
        urls: list[str] = []
        results = res.output if isinstance(res.output, list) else []
        for item in results:
            if not isinstance(item, dict):
                warn.append("etibarsız nəticə atıldı (format)")
                continue
            try:
                url = validate_url(item.get("url"))
            except UnsafeURL:
                warn.append(f"etibarsız URL atıldı: {sanitize_page_text(str(item.get('url')), 60)}")
                continue
            if url not in urls:
                urls.append(url)
        pages: list[FetchedPage] = []
        mock_flag = False
        for url in urls[: self.max_sources]:
            r = self.runtime.call(self.agent, self.fetch_tool, {"url": url})
            if r.status != "ok":
                reason = r.reason or r.status
                if r.status == "blocked" and "limit" in reason:
                    warn.append(f"büdcə limiti: sonrakı oxumalar dayandırıldı ({reason})")
                    break
                warn.append(f"{urlsplit(url).hostname}: yüklənmədi ({reason})")
                continue
            out = r.output if isinstance(r.output, dict) else {}
            prov = "live" if out.get("provider") == "live" else "mock"
            mock_flag = mock_flag or prov == "mock"
            pub = out.get("published")
            pages.append(FetchedPage(url, str(out.get("title", ""))[:200], str(out.get("text", "")),
                                     pub if isinstance(pub, str) else None, prov, now))
        report = build_report(q, pages, policy=self.policy, now=now, max_age_days=max_age_days, warnings=warn)
        return report


# --- yaddaşa yazma ---

@dataclass(frozen=True)
class RecordOutcome:
    written: list[WriteResult]
    skipped: int


def record_research(memory: MemoryService, report: ResearchReport, *, project: str = "research") -> RecordOutcome:
    """Nəticələri MEVCUD Memory Service ilə saxlayır. Mənşə: mock -> "mock", canlı -> "tool". Hər iddia statusu ilə
    yazılır (təsdiqlənməmiş iddia fakt kimi saxlanmır). Təkrar yazma op_key ilə qarşısı alınır."""
    mock = report.is_mock
    prov, src = ("mock", "research:mock") if mock else ("tool", "research:web")
    day = datetime.fromtimestamp(report.created_at, timezone.utc).date().isoformat()
    by_id = {s.id: s for s in report.sources}
    written: list[WriteResult] = []
    skipped = 0

    def put(content: str, meta: dict) -> None:
        nonlocal skipped
        content = content[:4000]
        # op_key məzmunun hash-indən: eyni nəticə təkrar yazılmır, fərqli nəticə ilə toqquşma olmur
        key = "research:" + hashlib.sha256(f"{day}|{content}".encode("utf-8")).hexdigest()[:24]
        try:
            written.append(memory.write(RESEARCH_ACTOR, "result", content, project=project, provenance=prov,
                                        source=src, op_key=key, meta={**meta, "query": report.query, "is_mock": mock}))
        except MemoryDenied:
            skipped += 1

    put(f"Araşdırma ({report.status}): {report.query} — {len(report.claims)} iddia, {len(report.sources)} mənbə",
        {"kind": "summary", "status": report.status, "n_claims": len(report.claims),
         "n_sources": len(report.sources), "warnings": list(report.warnings[:5])})
    for c in report.claims:
        domains = sorted(set(c.domains))
        put(f"[{STATUS_LABEL[c.status]}] {c.text[:1000]} | mənbələr: {', '.join(domains)}",
            {"kind": "claim", "status": c.status, "domains": domains, "reliability": c.reliability,
             "urls": [by_id[i].url for i in c.source_ids if i in by_id], "claim_id": c.id})
    return RecordOutcome(written, skipped)

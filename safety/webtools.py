"""Veb alətləri: MOCK (oflayn korpus) və CANLI (yalnız OS sandbox adapteri ilə mümkün) alət təsvirləri.

- `MockWeb.tool_specs()` — "pure" növlü, in-process, oflayn alətlər; hər çıxış `provider: "mock"` ilə işarələnir
  və real internet nəticəsi kimi təqdim edilməməlidir.
- `live_tool_specs()` — "network" növlü, `sandboxed=True` alətlər: Tool Runtime-da defoltda BAĞLIDIR və təsdiqlənmiş
  OS sandbox adapteri olmadan icra olunmur (bu repoda real adapter YOXDUR). Real HTTP kodu burada yazılmayıb.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from safety.tools import ToolContext, ToolError, ToolSpec, tool_handler


@dataclass(frozen=True)
class MockPage:
    title: str
    text: str
    published: str | None = None


def _stems(text: str) -> set[str]:
    norm = "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))
    return {w[:5] for w in re.findall(r"\w+", norm) if len(w) >= 3 and not w.isdigit()}


class MockWeb:
    """Oflayn "internet": URL -> MockPage. Heç bir şəbəkə çağırışı yoxdur."""

    def __init__(self, pages: Mapping[str, MockPage], extra_results: Sequence[Any] = (), cost: float = 0.0) -> None:
        self.pages = dict(pages)
        self.extra_results = list(extra_results)
        self.cost = cost

    def tool_specs(self) -> list[ToolSpec]:
        pages, extra = self.pages, self.extra_results

        @tool_handler("web.search")
        def search(ctx: ToolContext, query: str, limit: int = 5) -> list:
            q = _stems(query)
            scored = []
            for url, p in pages.items():
                score = len(q & _stems(p.title + " " + p.text))
                if score > 0:
                    scored.append((-score, url, p))
            scored.sort(key=lambda t: (t[0], t[1]))
            out = [{"url": u, "title": p.title, "snippet": p.text[:160], "published": p.published,
                    "provider": "mock"} for _, u, p in scored[:max(1, limit)]]
            return out + list(extra)

        @tool_handler("web.fetch")
        def fetch(ctx: ToolContext, url: str) -> dict:
            p = pages.get(url)
            if p is None:
                raise ToolError("səhifə tapılmadı", cost=ctx.cost)
            return {"url": url, "title": p.title, "text": p.text, "published": p.published, "provider": "mock"}

        return [
            ToolSpec("web.search", "pure", "read_files", {"query": str, "limit": int}, ("query",), search,
                     cost=self.cost, description="MOCK veb axtarışı (oflayn korpus)"),
            ToolSpec("web.fetch", "pure", "read_files", {"url": str}, ("url",), fetch, cost=self.cost,
                     description="MOCK səhifə oxuma (oflayn korpus)"),
        ]


def live_tool_specs() -> list[ToolSpec]:
    """Canlı veb alətlərinin TƏSVİRİ. `sandboxed=True`, növ `network`: yalnız təsdiqlənmiş OS sandbox adapteri və açıq
    siyasət ilə icra oluna bilər; handler yoxdur (real şəbəkə kodu in-process yazılmır)."""
    return [
        ToolSpec("web.search", "network", "read_files", {"query": str, "limit": int}, ("query",), sandboxed=True,
                 description="Canlı veb axtarışı (yalnız sandbox adapteri ilə)"),
        ToolSpec("web.fetch", "network", "read_files", {"url": str}, ("url",), sandboxed=True,
                 description="Canlı səhifə oxuma (yalnız sandbox adapteri ilə)"),
    ]

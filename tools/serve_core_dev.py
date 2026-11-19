#!/usr/bin/env python3
"""NAYMERA CORE inkişaf serveri (Mərhələ M1): `CoreApiApp` (`/v1/core/*` + mövcud `/v1/*`) YALNIZ 127.0.0.1-də. İnkişaf/test üçündür, istehsal serveri DEYİL.

Təhlükəsizlik:
- Bağlanma ünvanı SABİTDİR (`127.0.0.1`): `--host` yoxdur, mühit dəyişəni oxunmur, ünvan serverdən sonra yenidən yoxlanılır; LAN/ictimai/internet ifşası mümkün deyil.
  (Real cihaz/emulyator üçün `adb reverse tcp:PORT tcp:PORT` istifadə olunur: cihaz da 127.0.0.1 görür.)
- Mövcud autentifikasiya/cütləşmə/STOP/step-up qaydaları DƏYİŞMİR (`build_app` + `CoreApiApp`); heç bir yan keçid, test açarı və ya sabit parol yoxdur. Cütləşmə kodu konsolda göstərilir (bir-dəfəlik).
- Demo döngü (`--demo-loop`, defolt AÇIQ): Kernel Loop L1; planlaşdırıcı yalnız `demo` açar sözlü şablon; handler-lər yalnız `memory.recall`/`memory.consolidate`/`report.progress` (dar, modelsiz, şəbəkəsiz);
  hər addım ayrıca sahib təsdiqi (mövcud step-up) tələb edir; STOP bayrağı üstündür. Real model/API/ödəniş/kod icrası YOXDUR.
Bütün SQLite obyektləri BİR serverin ipində yaşayır; digər iplər `call(fn)` ilə iş göndərir (iplər arası sqlite istifadəsi yoxdur)."""
from __future__ import annotations

import argparse
import dataclasses
import queue
import sys
import threading
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.spec import AgentSpec  # noqa: E402
from api.bootstrap import Services, build_app  # noqa: E402
from api.core_api import CoreApiApp  # noqa: E402
from api.server import create_server  # noqa: E402
from core import FileStop, KernelLoop, build_store  # noqa: E402
from kernel import Origin, PlanStepSpec  # noqa: E402
from memory import EpisodeRecorder, MemoryStore  # noqa: E402
from reasoning import PlanTemplate, TemplatePlanner  # noqa: E402
from runtime.brokered_core import BrokeredApprovals, BrokeredStepExecutor, StepHandlerRegistry  # noqa: E402
from runtime.core_handlers import register_safe_handlers  # noqa: E402
from safety.broker import EffectBroker  # noqa: E402
from safety.dbconn import connect  # noqa: E402

BIND_HOST = "127.0.0.1"            # SABİT: konfiqurasiya ilə dəyişdirilə bilməz
DEFAULT_PORT = 8765
MAX_TICKS_PER_IDLE = 5


class BindError(RuntimeError):
    pass


@dataclasses.dataclass
class CoreDev:
    services: Services
    app: CoreApiApp
    faces: Any
    stop_path: Path
    loop: KernelLoop | None
    memory: MemoryStore | None


def _demo_steps() -> tuple[PlanStepSpec, ...]:
    return (PlanStepSpec("recall", "Yaddaşdan əlaqəli qeydləri tap", "Əlaqəli qeydlərin sayı", ("nonempty",), (), "memory.recall"),
            PlanStepSpec("report", "Tərəqqi hesabatı yaz", "Hesabat yaddaşa yazılır", ("nonempty",), ("recall",), "report.progress"))


def build_core_dev(data_dir: str | Path, *, owner_id: str = "owner", demo_loop: bool = True) -> CoreDev:
    d = Path(data_dir)
    svc = build_app(d, owner_id=owner_id)                                         # mövcud yığın: mock LLM, real heç nə yoxdur
    faces = build_store(connect(str(d / "core.db")))
    base = svc.app
    app = CoreApiApp(core_owner=faces.owner, autonomy="L1" if demo_loop else "L0", auth=base.auth, chat=base.chat, events=base.events, stop_path=base.stop_path, gateway=base.gateway,
                     reader=base.reader, memory=base.memory, company=base.company, proactive=base.proactive, jobs=base.jobs, voice=base.voice, owner_id=owner_id,
                     audit_path=base.audit_path, llm_is_mock=base.llm_is_mock, agent_runner_connected=base.agent_runner_connected, https=base.https, ai=base.ai,
                     voice_registry=base.voice_registry)
    loop, memory = None, None
    if demo_loop:
        memory = MemoryStore(connect(str(d / "memory_v2.db")))
        registry = StepHandlerRegistry()
        register_safe_handlers(registry, goals=faces.engine, memory=memory)
        registry.seal()
        spec = AgentSpec(name="naymera_core", purpose="Kernel Loop demo (dar handler-lər)", tools=[], permissions=["read_files", "write_files"], inputs=["x"], outputs=["y"], cost_limit=0.0)
        broker = EffectBroker(d / "core_audit.jsonl", base.stop_path, ledger=svc.ledger)
        planner = TemplatePlanner([PlanTemplate("demo", ("demo",), _demo_steps())])
        loop = KernelLoop(faces.engine, autonomy="L1", stop=FileStop(base.stop_path), planner=planner, approvals=BrokeredApprovals(svc.approvals.client, spec, registry),
                          executor=BrokeredStepExecutor(broker, spec, registry, svc.approvals.client), observer=EpisodeRecorder(memory), lease_s=120)
    return CoreDev(dataclasses.replace(svc, app=app), app, faces, base.stop_path, loop, memory)


class CoreDevServer:
    """Serveri ayrıca ipdə işlədir (testlər/jvm kontraktı üçün). Bütün obyektlər server ipində yaranır və yalnız orada istifadə olunur; `call(fn)` işi həmin ipə göndərir."""

    def __init__(self, data_dir: str | Path, *, port: int = 0, owner_id: str = "owner", demo_loop: bool = True, autorun_loop: bool = True) -> None:
        self._dir, self._port, self._owner, self._demo, self._autorun = Path(data_dir), port, owner_id, demo_loop, autorun_loop      # autorun_loop=False: döngü qurulur, yalnız `call` ilə addım-addım işləyir (deterministik testlər)
        self._q: queue.Queue[tuple[Callable[[CoreDev], Any], Future]] = queue.Queue()
        self._ready, self._stopping, self._error = threading.Event(), threading.Event(), None
        self._thread: threading.Thread | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://{BIND_HOST}:{self.port}"

    def start(self, timeout: float = 15.0) -> int:
        if self._thread is not None:
            raise RuntimeError("server artıq başladılıb")
        self._thread = threading.Thread(target=self._run, name="core-dev-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("server vaxtında başlamadı")
        if self._error is not None:
            raise self._error
        return self.port

    def _run(self) -> None:
        httpd = dev = None
        try:
            dev = build_core_dev(self._dir, owner_id=self._owner, demo_loop=self._demo)
            httpd = create_server(dev.services, BIND_HOST, self._port, request_timeout=5)
            host, port = httpd.server_address[:2]
            if host != BIND_HOST:                                                      # ikinci yoxlama: yalnız 127.0.0.1
                raise BindError(f"gözlənilməz bağlanma ünvanı: {host}")
            self.port, httpd.timeout = port, 0.05
            self._ready.set()
            while not self._stopping.is_set():
                httpd.handle_request()                                                 # ≤0.05s gözləyir
                self._drain(dev)
                if self._autorun and dev.loop is not None and not self._stopping.is_set():
                    try:
                        dev.loop.run(MAX_TICKS_PER_IDLE)
                    except Exception:                                                  # noqa: BLE001  döngü xətası serveri dayandırmır (jurnala yazılır)
                        pass
            self._drain(dev)
        except BaseException as exc:                                                   # noqa: BLE001
            self._error = exc
            self._ready.set()
        finally:
            if httpd is not None:
                httpd.server_close()
            self._stopped = True

    def _drain(self, dev: CoreDev) -> None:
        while True:
            try:
                fn, fut = self._q.get_nowait()
            except queue.Empty:
                return
            try:
                fut.set_result(fn(dev))
            except BaseException as exc:                                               # noqa: BLE001
                fut.set_exception(exc)

    def call(self, fn: Callable[[CoreDev], Any], timeout: float = 15.0) -> Any:
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError("server işləmir")
        fut: Future = Future()
        self._q.put((fn, fut))
        return fut.result(timeout)

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def __enter__(self) -> "CoreDevServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/serve_core_dev.py", description="CORE inkişaf serveri: YALNIZ 127.0.0.1 (host seçimi yoxdur).")
    ap.add_argument("--data-dir", default="data/core_dev")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-demo-loop", action="store_true", help="Kernel Loop demo-sunu söndür (yalnız API)")
    a = ap.parse_args(argv)
    if not 0 <= a.port <= 65535:
        ap.error("port 0..65535 olmalıdır")
    srv = CoreDevServer(a.data_dir, port=a.port, demo_loop=not a.no_demo_loop)
    srv.start()
    code = srv.call(lambda dev: dev.services.auth.create_pairing_code())
    print(f"NAYMERA CORE dev server: {srv.url}  (yalnız loopback)\nCütləşmə kodu (bir-dəfəlik): {code}\nTelefon/emulyator üçün: adb reverse tcp:{srv.port} tcp:{srv.port}\nDayandırmaq: Ctrl+C")
    try:
        while srv._thread is not None and srv._thread.is_alive():
            srv._thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

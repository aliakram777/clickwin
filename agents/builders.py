"""Məhsul istehsalı bünövrəsi: Website Builder, App Builder, APK Builder və Video Studio üçün genişləndirilə bilən agent interfeysləri.

Bu mərhələdə HEÇ BİR builder qurulmayıb (`not_configured`): real alət (fayl yazma, test, build, render), nəşr, imzalama və deployment YOXDUR. `plan()` yalnız
deterministik SİMULYASİYA planıdır (yan təsirsiz); `build()` həmişə `NotConfigured` atır. Plan üç mərhələyə bölünür: `planning` (yalnız `read_files`, agentlərlə
mətn nəticələri), `build` (alət və `write_files`/`run_tests` tələb edir; hələ yoxdur) və `release` (nəşr/imzalama/deployment: HƏMİŞƏ sahib təsdiqi, Gate/Broker üzərindən).
`to_team_plan` yalnız planlaşdırma mərhələsini çoxagentli plana çevirir. Gələcək real alətlər `ToolRuntime` ilə qoşulacaq (burada import olunmur).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from agents.memory import detect_secrets
from agents.team import MAX_GOAL, Stage, TeamPlan


class NotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class BuildStep:
    stage_id: str
    role_id: str
    title: str
    instruction: str
    phase: str                       # planning | build | release
    depends_on: tuple[str, ...] = ()
    permission: str = "read_files"
    needs_approval: bool = False


@dataclass(frozen=True)
class ApprovalGate:
    action: str
    permission: str
    reason: str
    owner_only: bool = True


@dataclass(frozen=True)
class BuildPlan:
    builder_id: str
    brief: str
    steps: tuple[BuildStep, ...]
    approvals: tuple[ApprovalGate, ...]
    artifacts: tuple[str, ...]
    simulation: bool = True
    notes: tuple[str, ...] = ()

    @property
    def deferred(self) -> tuple[BuildStep, ...]:
        return tuple(s for s in self.steps if s.phase != "planning")


@dataclass(frozen=True)
class BuilderStatus:
    builder_id: str
    title: str
    state: str                       # həmişə not_configured (bu mərhələdə)
    reason: str
    required_tools: tuple[str, ...]
    tools_state: dict = field(default_factory=dict)
    can_publish: bool = False


def _brief(brief: object) -> str:
    if not isinstance(brief, str) or not brief.strip():
        raise ValueError("təsvir boş ola bilməz")
    text = " ".join(brief.split())
    if len(text) > MAX_GOAL:
        raise ValueError(f"təsvir çox uzundur (maks. {MAX_GOAL})")
    if detect_secrets(text):
        raise ValueError("təsvir sirr bənzər məzmun ehtiva edir")
    return text


class ProductBuilder(Protocol):
    builder_id: str

    def status(self) -> BuilderStatus: ...
    def plan(self, brief: str) -> BuildPlan: ...
    def build(self, plan: BuildPlan) -> None: ...
    def to_team_plan(self, plan: BuildPlan) -> TeamPlan: ...


class _Builder:
    builder_id = ""
    title = ""
    reason = ""
    tools: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    planning: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = ()
    build_steps: tuple[tuple[str, str, str, str, tuple[str, ...], str], ...] = ()
    gates: tuple[ApprovalGate, ...] = ()
    release: tuple[tuple[str, str, str, str, tuple[str, ...], str], ...] = ()

    def status(self) -> BuilderStatus:
        return BuilderStatus(self.builder_id, self.title, "not_configured", self.reason, self.tools, {t: "not_configured" for t in self.tools}, False)

    def plan(self, brief: str) -> BuildPlan:
        text = _brief(brief)
        steps = [BuildStep(i, r, t, ins, "planning", d) for i, r, t, ins, d in self.planning]
        steps += [BuildStep(i, r, t, ins, "build", d, perm) for i, r, t, ins, d, perm in self.build_steps]
        steps += [BuildStep(i, r, t, ins, "release", d, perm, True) for i, r, t, ins, d, perm in self.release]
        return BuildPlan(self.builder_id, text, tuple(steps), self.gates, self.artifacts, True, self.notes)

    def build(self, plan: BuildPlan) -> None:
        raise NotConfigured(f"{self.title}: NOT CONFIGURED — {self.reason}")

    def to_team_plan(self, plan: BuildPlan) -> TeamPlan:
        planning = [s for s in plan.steps if s.phase == "planning"]
        return TeamPlan.build(plan.brief, [Stage(s.stage_id, s.role_id, s.title, s.instruction, s.depends_on) for s in planning], "rules")


class WebsiteBuilder(_Builder):
    builder_id, title = "website", "Website Builder"
    reason = "fayl yazma, test və statik hosting alətləri qoşulmayıb; saytın qurulması və yayımı mümkün deyil"
    tools = ("fs.write", "tests.run", "site.preview", "site.deploy")
    artifacts = ("site-plan.md", "wireframes.md", "test-plan.md")
    planning = (("plan", "project_manager", "Layihə planı", "Məqsəd, auditoriya və səhifə siyahısını müəyyənləşdir.", ()),
                ("research", "researcher", "Rəqib və tələb araşdırması", "Mənbəli qısa araşdırma hazırla.", ("plan",)),
                ("design", "web_designer", "Dizayn və struktur", "Səhifə strukturu, naviqasiya və əlçatanlıq ilə maket təsviri hazırla.", ("plan",)),
                ("code", "programmer", "Reallaşdırma planı", "Komponentlər və kod strukturu təklif et (qaralama).", ("research", "design")),
                ("test", "test_engineer", "Test planı", "Test halları və qəbul meyarları hazırla.", ("code",)))
    build_steps = (("impl", "programmer", "Saytın yazılması", "Faylları yarat (alətlər qoşulduqdan sonra).", ("test",), "write_files"),
                   ("verify", "test_engineer", "Testlərin icrası", "Testləri işlət (alətlər qoşulduqdan sonra).", ("impl",), "run_tests"))
    release = (("publish", "web_designer", "Saytın yayımı", "Yayım yalnız sahib təsdiqi ilə.", ("verify",), "publish"),)
    gates = (ApprovalGate("publish", "publish", "sayt ictimaiyyətə açılır"), ApprovalGate("deploy", "external_account", "hosting hesabı/deployment"))
    notes = ("Bu plan SİMULYASİYADIR: alət qoşulmadığı üçün heç nə yaradılmır və yayımlanmır.",)


class AppBuilder(_Builder):
    builder_id, title = "app", "App Builder"
    reason = "layihə quruluşu, build və test alətləri qoşulmayıb; tətbiqin qurulması mümkün deyil"
    tools = ("fs.write", "tests.run", "build.runner")
    artifacts = ("app-plan.md", "architecture.md", "test-plan.md")
    planning = (("plan", "project_manager", "Layihə planı", "Funksiyalar, platformalar və mərhələləri müəyyənləşdir.", ()),
                ("research", "researcher", "Oxşar tətbiqlərin araşdırması", "Mənbəli qısa araşdırma hazırla.", ("plan",)),
                ("arch", "programmer", "Arxitektura təklifi", "Modullar, məlumat axını və təhlükəsizlik tələbləri.", ("plan",)),
                ("test", "test_engineer", "Test planı", "Test halları və qəbul meyarları.", ("research", "arch")))
    build_steps = (("impl", "programmer", "Kodun yazılması", "Kodu yarat (alətlər qoşulduqdan sonra).", ("test",), "write_files"),
                   ("verify", "test_engineer", "Testlərin icrası", "Testləri işlət (alətlər qoşulduqdan sonra).", ("impl",), "run_tests"))
    release = (("distribute", "project_manager", "Paylaşım", "Paylaşım yalnız sahib təsdiqi ilə.", ("verify",), "publish"),)
    gates = (ApprovalGate("distribution", "publish", "tətbiq paylaşılır"),)
    notes = ("Bu plan SİMULYASİYADIR: build alətləri qoşulmayıb.",)


class ApkBuilder(_Builder):
    builder_id, title = "apk", "APK Builder"
    reason = "Android SDK və Gradle/AGP asılılıqları (dl.google.com) bu mühitdə yoxdur; APK build edilə bilməz (bax: android/BUILD_STATUS.md)"
    tools = ("android.sdk", "gradle.build", "apk.sign")
    artifacts = ("apk-plan.md", "build-checklist.md")
    planning = (("plan", "project_manager", "APK planı", "Tələblər, SDK versiyaları və yoxlama siyahısı.", ()),
                ("app", "android_developer", "Tətbiq strukturu", "Ekranlar, şəbəkə qatı və təhlükəsizlik (Kotlin/Compose).", ("plan",)),
                ("test", "test_engineer", "Test planı", "Cihaz və emulyator sınaq planı.", ("app",)))
    build_steps = (("build", "android_developer", "Debug build", "Debug APK build (Android SDK qoşulduqdan sonra).", ("test",), "run_tests"),)
    release = (("sign", "project_manager", "İmzalama", "İmzalama açarı YALNIZ sahibdədir; avtomatlaşdırılmır.", ("build",), "external_account"),
               ("distribute", "project_manager", "Yayım", "Mağaza/paylaşım yalnız sahib təsdiqi ilə.", ("sign",), "publish"))
    gates = (ApprovalGate("apk_signing", "external_account", "imzalama açarı (keystore) yalnız sahibə məxsusdur"), ApprovalGate("distribution", "publish", "APK paylaşılır/yayımlanır"))
    notes = ("Bu plan SİMULYASİYADIR. Keystore və mağaza hesabları sahibin qərarıdır; repoda/agentdə açar saxlanmır.", "Android SDK olmadan APK build mümkün deyil.")


class VideoStudio(_Builder):
    builder_id, title = "video", "Video Studio"
    reason = "video render və media alətləri qoşulmayıb; yalnız ssenari/storyboard planı mümkündür"
    tools = ("video.render", "media.store")
    artifacts = ("script.md", "storyboard.md", "production-plan.md")
    planning = (("plan", "project_manager", "İstehsal planı", "Məqsəd, müddət və format.", ()),
                ("research", "researcher", "Mövzu araşdırması", "Mənbəli qısa araşdırma hazırla.", ("plan",)),
                ("script", "video_creator", "Ssenari və storyboard", "Qısa ssenari və storyboard (yalnız mətn).", ("research",)))
    build_steps = (("render", "video_creator", "Video render", "Video yarat (alətlər qoşulduqdan sonra).", ("script",), "write_files"),)
    release = (("publish", "project_manager", "Paylaşım", "Paylaşım yalnız sahib təsdiqi ilə.", ("render",), "publish"),)
    gates = (ApprovalGate("distribution", "publish", "video paylaşılır"),)
    notes = ("Bu plan SİMULYASİYADIR: video istehsalı alətləri qoşulmayıb.",)


BUILDERS: dict[str, type[_Builder]] = {"website": WebsiteBuilder, "app": AppBuilder, "apk": ApkBuilder, "video": VideoStudio}


class BuilderRegistry:
    def __init__(self) -> None:
        self._b = {bid: cls() for bid, cls in BUILDERS.items()}

    def all(self) -> list[_Builder]:
        return list(self._b.values())

    def get(self, builder_id: str) -> _Builder | None:
        return self._b.get(builder_id)

    def statuses(self) -> list[BuilderStatus]:
        return [b.status() for b in self._b.values()]

"""Workspace: agentlərin fayl alətləri üçün ayrılmış qovluq.

DİQQƏT: bu, TƏTBİQ SƏVİYYƏLİ yol sərhədidir, OS sandbox deyil. Qorunan: `..`/mütləq yol/NUL/backslash,
symlink və hardlink-lər (qadağandır), sirrə bənzər adlar, fayl/workspace ölçüsü. Məlum məhdudiyyət: yoxlama ilə
açma arasında başqa PROSES qovluğu dəyişə bilər (TOCTOU); workspace-ə yalnız bu alətlər yazmalıdır. Real
izolyasiya OS sandbox adapteri tələb edir (safety/tools.py).
"""
from __future__ import annotations

import fnmatch
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

from safety.tools import ToolContext, ToolDenied, ToolError, ToolSpec, tool_handler

DEFAULT_DENY_NAMES = (".env", ".env.*", "*.pem", "*.key", "*.p12", "id_rsa*", "id_ed25519*", ".git", ".ssh",
                      ".aws", ".netrc")
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DRIVE = re.compile(r"^[A-Za-z]:")


class WorkspaceError(ValueError):
    pass


class PathDenied(WorkspaceError):
    pass


class NotFound(WorkspaceError):
    pass


class FileTooLarge(WorkspaceError):
    pass


class QuotaExceeded(WorkspaceError):
    pass


def _positive(name: str, v: Any) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValueError(f"{name} müsbət tam ədəd olmalıdır")


class Workspace:
    def __init__(self, root: str | Path, max_file_bytes: int = 1_048_576,
                 max_workspace_bytes: int = 16 * 1024 * 1024, max_entries: int = 1000,
                 deny_names: tuple[str, ...] = DEFAULT_DENY_NAMES) -> None:
        for n, v in (("max_file_bytes", max_file_bytes), ("max_workspace_bytes", max_workspace_bytes),
                     ("max_entries", max_entries)):
            _positive(n, v)
        p = Path(root)
        if not p.exists() or not p.is_dir():
            raise WorkspaceError(f"workspace qovluğu mövcud deyil: {p}")
        self.root = p.resolve(strict=True)
        self.max_file_bytes = max_file_bytes
        self.max_workspace_bytes = max_workspace_bytes
        self.max_entries = max_entries
        self.deny_names = tuple(n.casefold() for n in deny_names)

    # --- yol ---
    def resolve(self, rel: Any, *, allow_root: bool = False) -> Path:
        if not isinstance(rel, str) or len(rel) > 1024:
            raise PathDenied("yol sətir olmalı və 1024 simvoldan az olmalıdır")
        if "\x00" in rel or "\\" in rel or rel.startswith(("/", "~")) or _DRIVE.match(rel):
            raise PathDenied("yol qaydaya uyğun deyil (mütləq/backslash/NUL/~)")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if ".." in parts:
            raise PathDenied("'..' qadağandır")
        if not parts:
            if allow_root and rel in ("", "."):
                return self.root
            raise PathDenied("boş yol")
        for p in parts:
            if any(fnmatch.fnmatchcase(p.casefold(), pat) for pat in self.deny_names):
                raise PathDenied(f"qadağan olunmuş ad: {p}")
        cur = self.root
        for i, p in enumerate(parts):
            cur = cur / p
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                if i < len(parts) - 1:
                    raise PathDenied("ata qovluq mövcud deyil") from None
                break
            if stat.S_ISLNK(st.st_mode):
                raise PathDenied("symlink qadağandır")
            if i < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
                raise PathDenied("ara komponent qovluq deyil")
        final = self.root.joinpath(*parts)
        real = os.path.realpath(final)
        root = str(self.root)
        if real != root and not real.startswith(root + os.sep):
            raise PathDenied("yol workspace-dən çıxır")
        return final

    # --- əməliyyatlar ---
    def read_text(self, rel: Any) -> str:
        path = self.resolve(rel)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | _O_CLOEXEC)
        except FileNotFoundError:
            raise NotFound("fayl tapılmadı") from None
        except OSError as exc:
            raise PathDenied(f"fayl açıla bilmir: {exc.strerror}") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise WorkspaceError("adi fayl deyil")
            if st.st_nlink > 1:
                raise PathDenied("hardlink qadağandır")
            if st.st_size > self.max_file_bytes:
                raise FileTooLarge(f"fayl limiti aşır: {st.st_size} > {self.max_file_bytes}")
            data = b""
            while len(data) < st.st_size:
                chunk = os.read(fd, st.st_size - len(data))
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(fd)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError("fayl UTF-8 mətn deyil") from None

    def write_text(self, rel: Any, content: Any, overwrite: bool = False) -> int:
        if not isinstance(content, str):
            raise WorkspaceError("məzmun mətn olmalıdır")
        data = content.encode("utf-8")
        if len(data) > self.max_file_bytes:
            raise FileTooLarge(f"fayl limiti aşır: {len(data)} > {self.max_file_bytes}")
        path = self.resolve(rel)
        existing = 0
        exists = False
        try:
            st = os.lstat(path)
            exists = True
            if not stat.S_ISREG(st.st_mode):
                raise WorkspaceError("adi fayl deyil")
            if st.st_nlink > 1:
                raise PathDenied("hardlink qadağandır")
            existing = st.st_size
        except FileNotFoundError:
            pass
        if exists and not overwrite:
            raise PathDenied("fayl artıq mövcuddur (overwrite=false)")
        if self.used_bytes() - existing + len(data) > self.max_workspace_bytes:
            raise QuotaExceeded("workspace kvotası aşılır")
        flags = os.O_WRONLY | os.O_NOFOLLOW | _O_CLOEXEC | (0 if exists else os.O_CREAT | os.O_EXCL)
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise PathDenied(f"fayl açıla bilmir: {exc.strerror}") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                raise PathDenied("hədəf adi, tək-linkli fayl deyil")
            os.ftruncate(fd, 0)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        return len(data)

    def list_dir(self, rel: Any = ".") -> list[str]:
        path = self.resolve(rel, allow_root=True)
        if not path.is_dir():
            raise WorkspaceError("qovluq deyil")
        names: list[str] = []
        with os.scandir(path) as it:
            for e in it:
                if e.is_symlink() or any(fnmatch.fnmatchcase(e.name.casefold(), p) for p in self.deny_names):
                    continue
                names.append(e.name + ("/" if e.is_dir(follow_symlinks=False) else ""))
                if len(names) > self.max_entries:
                    raise WorkspaceError(f"element sayı limiti aşır: {self.max_entries}")
        return sorted(names)

    def used_bytes(self) -> int:
        total = 0
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            for f in filenames:
                try:
                    st = os.lstat(os.path.join(dirpath, f))
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    total += st.st_size
        return total


# --- alətlər ---

def file_tools(ws: Workspace) -> list[ToolSpec]:
    """Workspace-ə bağlı fayl alətləri (in-process, etibarlı kod). Yalnız broker daxilində işləyir."""

    def wrap(ctx: ToolContext, fn: Any, *a: Any, **k: Any) -> Any:
        try:
            return fn(*a, **k)
        except WorkspaceError as exc:
            raise ToolError(str(exc), cost=ctx.cost) from None

    def pre(key: str, allow_root: bool = False) -> Any:
        def check(args: Mapping[str, Any]) -> None:
            try:
                ws.resolve(args.get(key, "." if allow_root else None), allow_root=allow_root)
            except PathDenied as exc:
                raise ToolDenied(str(exc)) from None

        return check

    @tool_handler("fs.read")
    def read(ctx: ToolContext, path: str) -> str:
        return wrap(ctx, ws.read_text, path)

    @tool_handler("fs.write")
    def write(ctx: ToolContext, path: str, content: str, overwrite: bool = False) -> dict:
        n = wrap(ctx, ws.write_text, path, content, overwrite)
        return {"path": path, "bytes": n}

    @tool_handler("fs.list")
    def ls(ctx: ToolContext, path: str = ".") -> list:
        return wrap(ctx, ws.list_dir, path)

    return [
        ToolSpec("fs.read", "fs_read", "read_files", {"path": str}, ("path",), read,
                 precheck=pre("path"), description="Workspace-də mətn faylını oxu"),
        ToolSpec("fs.write", "fs_write", "write_files", {"path": str, "content": str, "overwrite": bool},
                 ("path", "content"), write, precheck=pre("path"),
                 description="Workspace-də mətn faylı yaz (defolt: yalnız yeni fayl)"),
        ToolSpec("fs.list", "fs_read", "read_files", {"path": str}, (), ls,
                 precheck=pre("path", allow_root=True), description="Workspace qovluğunun siyahısı"),
    ]

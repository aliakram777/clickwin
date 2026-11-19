"""Ehtiyat nüsxə və bərpa (Mərhələ 18.8): SQLite faylları üçün (sqlite3 backup API, oxu-yalnız mənbə). Manifest hər faylın sha256 + integrity_check nəticəsi ilə. Bərpa əvvəlcə yoxlayır, sonra
YALNIZ hədəf qovluqda (ad slug yoxlaması ilə) yazır; mövcud faylı yalnız `overwrite=True` ilə əvəz edir. Qovluq 0700, fayllar 0600.
HƏDD: nüsxələr ŞİFRƏLƏNMİR (DB-lər həssas məlumat saxlaya bilər: nüsxəni sahibin təhlükəsiz yerində saxlayın); xarici saxlama/yükləmə YOXDUR; bərpa zamanı tətbiq dayandırılmalıdır (kilid yoxlanmır)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

_NAME = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,47}$")
_DIR = re.compile(r"^backup-\d{8}T\d{6}Z(-\d+)?$")
MANIFEST = "manifest.json"


class BackupError(Exception):
    pass


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _integrity(path: Path) -> bool:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def backup(db_paths: Mapping[str, str | Path], dest_dir: str | Path, *, keep: int = 5, clock: Callable[[], datetime] | None = None) -> Path:
    for name in db_paths:
        if not _NAME.match(name):
            raise BackupError(f"yanlış ad: {name!r}")
    now = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    root = Path(dest_dir)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    stamp, n = now.strftime("backup-%Y%m%dT%H%M%SZ"), 0
    target = root / stamp
    while target.exists():
        n += 1
        target = root / f"{stamp}-{n}"
    target.mkdir(mode=0o700)
    files: dict[str, dict] = {}
    for name, src in sorted(db_paths.items()):
        s, d = Path(src), target / f"{name}.db"
        if not s.is_file():
            raise BackupError(f"mənbə yoxdur: {name}")
        con = sqlite3.connect(f"file:{s}?mode=ro", uri=True)
        out = sqlite3.connect(d)
        try:
            con.backup(out)
        finally:
            out.close()
            con.close()
        os.chmod(d, 0o600)
        files[name] = {"sha256": _sha(d), "size": d.stat().st_size, "integrity_ok": _integrity(d)}
    (target / MANIFEST).write_text(json.dumps({"version": 1, "created": now.isoformat(), "files": files}, sort_keys=True), encoding="utf-8")
    os.chmod(target / MANIFEST, 0o600)
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and _DIR.match(p.name) and (p / MANIFEST).is_file())
    for old in dirs[:-max(1, int(keep))]:
        shutil.rmtree(old)
    return target


def verify_backup(path: str | Path) -> tuple[bool, list[str]]:
    p, problems = Path(path), []
    try:
        man = json.loads((p / MANIFEST).read_text(encoding="utf-8"))
        files = man["files"]
    except Exception:                                            # noqa: BLE001
        return False, ["manifest oxunmadı"]
    for name, meta in files.items():
        f = p / f"{name}.db"
        if not _NAME.match(name) or not f.is_file():
            problems.append(f"{name}: fayl yoxdur/yanlış ad")
        elif _sha(f) != meta.get("sha256"):
            problems.append(f"{name}: hash uyğun deyil")
        elif not _integrity(f):
            problems.append(f"{name}: integrity_check uğursuz")
    return not problems, problems


def restore(backup_path: str | Path, target_dir: str | Path, *, overwrite: bool = False) -> list[str]:
    ok, problems = verify_backup(backup_path)
    if not ok:
        raise BackupError("nüsxə yoxlamadan keçmədi: " + "; ".join(problems))
    man = json.loads((Path(backup_path) / MANIFEST).read_text(encoding="utf-8"))
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    names = sorted(man["files"])
    exists = [n for n in names if (target / f"{n}.db").exists()]
    if exists and not overwrite:
        raise BackupError("hədəfdə mövcud fayllar var (overwrite=True tələb olunur): " + ", ".join(exists))
    for n in names:
        tmp = target / f".restore-{n}.tmp"
        shutil.copyfile(Path(backup_path) / f"{n}.db", tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target / f"{n}.db")
    return names

"""Workspace: tətbiq səviyyəli yol sərhədi (OS sandbox DEYİL). Testlər yalnız pytest-in tmp qovluğunda işləyir."""
import os

import pytest

from safety.workspace import (
    FileTooLarge, NotFound, PathDenied, QuotaExceeded, Workspace, WorkspaceError,
)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root, max_file_bytes=100, max_workspace_bytes=250)


def test_write_read_list_roundtrip(ws):
    assert ws.write_text("a.txt", "salam") == 5
    assert ws.read_text("a.txt") == "salam"
    ws.write_text("b.txt", "x")
    assert ws.list_dir(".") == ["a.txt", "b.txt"]
    assert ws.list_dir("") == ["a.txt", "b.txt"]


def test_nested_paths_need_existing_parent(ws, tmp_path):
    (tmp_path / "ws" / "sub").mkdir()
    ws.write_text("sub/c.txt", "ok")
    assert ws.read_text("sub/c.txt") == "ok"
    assert ws.list_dir(".") == ["sub/"]
    with pytest.raises(PathDenied):
        ws.write_text("yoxdur/c.txt", "x")  # avtomatik mkdir yoxdur


def test_create_only_by_default_overwrite_explicit(ws):
    ws.write_text("a.txt", "bir")
    with pytest.raises(PathDenied):
        ws.write_text("a.txt", "iki")
    assert ws.read_text("a.txt") == "bir"
    ws.write_text("a.txt", "iki", overwrite=True)
    assert ws.read_text("a.txt") == "iki"


@pytest.mark.parametrize("bad", [
    "../x.txt", "a/../../x.txt", "..", "/etc/passwd", "a\\b.txt", "x\x00y", "C:/x.txt",
    "~/x", "a/../b/../../c", "", "x" * 2000,
])
def test_traversal_and_malformed_paths_denied(ws, tmp_path, bad):
    with pytest.raises(PathDenied):
        ws.write_text(bad, "zərər")
    with pytest.raises(PathDenied):
        ws.read_text(bad)
    assert not (tmp_path / "x.txt").exists() and not (tmp_path / "c").exists()


def test_absolute_path_inside_root_is_still_denied(ws, tmp_path):
    ws.write_text("a.txt", "x")
    with pytest.raises(PathDenied):
        ws.read_text(str(tmp_path / "ws" / "a.txt"))


def test_non_string_path_denied(ws):
    for bad in (None, 5, b"a.txt", ["a"]):
        with pytest.raises(PathDenied):
            ws.read_text(bad)


def test_symlinks_are_denied(ws, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("gizli")
    root = tmp_path / "ws"
    os.symlink(outside, root / "link.txt")
    os.symlink(tmp_path, root / "dirlink")
    (root / "real.txt").write_text("içəri")
    os.symlink(root / "real.txt", root / "inner_link.txt")  # içəriyə olsa da qadağan
    for p in ("link.txt", "dirlink/outside.txt", "inner_link.txt"):
        with pytest.raises(PathDenied):
            ws.read_text(p)
    with pytest.raises(PathDenied):
        ws.write_text("link.txt", "yaz", overwrite=True)
    assert outside.read_text() == "gizli"
    assert ws.list_dir(".") == ["real.txt"]  # symlink-lər siyahıda görünmür


def test_hardlinks_to_outside_files_are_denied(ws, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("gizli")
    os.link(outside, tmp_path / "ws" / "h.txt")
    with pytest.raises(PathDenied):
        ws.read_text("h.txt")
    with pytest.raises(PathDenied):
        ws.write_text("h.txt", "yaz", overwrite=True)
    assert outside.read_text() == "gizli"  # kəsilməyib, dəyişməyib


@pytest.mark.parametrize("name", [".env", ".env.local", "k.pem", "server.key", "id_rsa", ".git/config", ".ssh/id"])
def test_secret_like_names_denied(ws, tmp_path, name):
    (tmp_path / "ws" / ".git").mkdir(exist_ok=True)
    (tmp_path / "ws" / ".ssh").mkdir(exist_ok=True)
    with pytest.raises(PathDenied):
        ws.write_text(name, "x")
    with pytest.raises(PathDenied):
        ws.read_text(name)


def test_secret_like_files_hidden_from_listing(ws, tmp_path):
    (tmp_path / "ws" / ".env").write_text("A=1")
    ws.write_text("ok.txt", "x")
    assert ws.list_dir(".") == ["ok.txt"]


def test_size_limits(ws, tmp_path):
    with pytest.raises(FileTooLarge):
        ws.write_text("big.txt", "x" * 101)
    (tmp_path / "ws" / "huge.txt").write_text("x" * 500)  # kənardan düşüb
    with pytest.raises(FileTooLarge):
        ws.read_text("huge.txt")


def test_workspace_quota(ws):
    for i in range(2):
        ws.write_text(f"f{i}.txt", "x" * 100)
    with pytest.raises(QuotaExceeded):
        ws.write_text("f2.txt", "x" * 100)  # 300 > 250
    ws.write_text("f0.txt", "x" * 50, overwrite=True)  # üzərinə yazmaq yer boşaldır
    assert ws.used_bytes() == 150


def test_non_utf8_and_missing(ws, tmp_path):
    (tmp_path / "ws" / "bin.dat").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(WorkspaceError):
        ws.read_text("bin.dat")
    with pytest.raises(NotFound):
        ws.read_text("yoxdur.txt")


def test_directory_is_not_a_file(ws, tmp_path):
    (tmp_path / "ws" / "d").mkdir()
    with pytest.raises(WorkspaceError):
        ws.read_text("d")
    with pytest.raises(WorkspaceError):
        ws.write_text("d", "x", overwrite=True)


def test_list_entry_cap(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    small = Workspace(root, max_entries=3)
    for i in range(4):
        (root / f"{i}.txt").write_text("x")
    with pytest.raises(WorkspaceError):
        small.list_dir(".")


def test_root_validation(tmp_path):
    with pytest.raises(WorkspaceError):
        Workspace(tmp_path / "yoxdur")
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(WorkspaceError):
        Workspace(f)
    for bad in ({"max_file_bytes": 0}, {"max_workspace_bytes": -1}, {"max_entries": 0}):
        with pytest.raises(ValueError):
            Workspace(tmp_path, **bad)

"""Unit tests for the proxy: no pyrefly, logic only."""

from __future__ import annotations

import io
import os
import time
from pathlib import Path

import pyrefly_lsp_proxy as proxy
import pytest
from conftest import git, init_repo

NOT_ROOT = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read despite chmod(0)")


def test_message_roundtrip() -> None:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "x", "params": {"text": "Grüße 🙂"}}
    stream = io.BytesIO(proxy.encode(msg) + proxy.encode({"jsonrpc": "2.0", "method": "y"}))
    assert proxy.read_message(stream) == msg
    assert proxy.read_message(stream) == {"jsonrpc": "2.0", "method": "y"}
    assert proxy.read_message(stream) is None


def test_apply_full_change() -> None:
    assert proxy.apply_changes("old", [{"text": "new"}]) == "new"


def test_apply_incremental_change_counts_utf16() -> None:
    # 🙂 is two UTF-16 units: the position after it is 2, not 1.
    text = "a🙂b\nsécond\n"
    change = {"range": {"start": {"line": 0, "character": 3}, "end": {"line": 1, "character": 6}}, "text": "X"}
    assert proxy.apply_changes(text, [change]) == "a🙂X\n"


def test_settings_defaults_and_placeholders() -> None:
    s = proxy.Settings.from_env(
        {
            "PYREFLY_LSP_TYPE_CHECKING_MODE": "${user_config.type_checking_mode}",
            "PYREFLY_LSP_DIAGNOSTICS": "",
        }
    )
    assert s.type_checking_mode == "auto"
    assert s.diagnostics is True
    assert s.sync_from_disk is True
    assert s.complete_outgoing_calls is True
    assert s.indexing_mode == "lazy-blocking"
    assert s.pyrefly is None


def test_settings_values() -> None:
    s = proxy.Settings.from_env(
        {
            "PYREFLY_LSP_TYPE_CHECKING_MODE": "strict",
            "PYREFLY_LSP_DIAGNOSTICS": "false",
            "PYREFLY_LSP_SYNC_FROM_DISK": "false",
            "PYREFLY_LSP_COMPLETE_OUTGOING_CALLS": "false",
            "PYREFLY_LSP_INDEXING_MODE": "lazy-non-blocking-background",
            "PYREFLY_LSP_PYREFLY": "/opt/pyrefly",
        }
    )
    assert (s.type_checking_mode, s.diagnostics, s.sync_from_disk, s.complete_outgoing_calls) == (
        "strict",
        False,
        False,
        False,
    )
    assert s.indexing_mode == "lazy-non-blocking-background"
    assert s.pyrefly == "/opt/pyrefly"


def test_settings_reject_unknown_mode() -> None:
    assert proxy.Settings.from_env({"PYREFLY_LSP_TYPE_CHECKING_MODE": "loose"}).type_checking_mode == "auto"


def _repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    main = init_repo(tmp_path / "main")
    (main / "a.py").write_text("x = 1\n")
    git(main, "add", "a.py")
    git(main, "commit", "-qm", "init")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", str(wt))
    return main, wt


def _fake_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_main_checkout_of_worktree(tmp_path: Path) -> None:
    main, wt = _repo_with_worktree(tmp_path)
    assert proxy.main_checkout(wt) == main.resolve()
    assert proxy.main_checkout(main) is None
    assert proxy.main_checkout(tmp_path / "not-a-repo") is None


def test_interpreter_for_worktree_uses_main_venv(tmp_path: Path) -> None:
    main, wt = _repo_with_worktree(tmp_path)
    python = _fake_executable(main / ".venv" / "bin" / "python")
    assert proxy.interpreter_for(wt, {}) == str(python)
    # The project itself has a .venv or an active environment: pyrefly finds it, nothing to override.
    assert proxy.interpreter_for(main, {}) is None
    assert proxy.interpreter_for(wt, {"VIRTUAL_ENV": "/somewhere"}) is None
    _fake_executable(wt / ".venv" / "bin" / "python")
    assert proxy.interpreter_for(wt, {}) is None


def _binary(command: list[str] | None) -> str:
    assert command is not None
    return command[0]


def test_pyrefly_command_prefers_project_then_main_then_path(tmp_path: Path) -> None:
    main, wt = _repo_with_worktree(tmp_path)
    settings = proxy.Settings.from_env({})
    on_path = lambda _name: "/usr/bin/pyrefly"  # noqa: E731
    nowhere = lambda _name: None  # noqa: E731
    assert proxy.pyrefly_command(wt, settings, on_path) == [
        "/usr/bin/pyrefly",
        "lsp",
        "--indexing-mode",
        "lazy-blocking",
    ]
    assert proxy.pyrefly_command(wt, settings, nowhere) is None
    from_main = _fake_executable(main / ".venv" / "bin" / "pyrefly")
    assert _binary(proxy.pyrefly_command(wt, settings, on_path)) == str(from_main)
    own = _fake_executable(wt / ".venv" / "bin" / "pyrefly")
    assert _binary(proxy.pyrefly_command(wt, settings, on_path)) == str(own)
    explicit = proxy.Settings.from_env({"PYREFLY_LSP_PYREFLY": "/opt/pyrefly"})
    assert _binary(proxy.pyrefly_command(wt, explicit, on_path)) == "/opt/pyrefly"


def test_init_options_merge() -> None:
    auto = proxy.Settings.from_env({})
    assert proxy.init_options({"keep": 1}, auto, None) == {"keep": 1}
    strict = proxy.Settings.from_env({"PYREFLY_LSP_TYPE_CHECKING_MODE": "strict"})
    merged = proxy.init_options({"pyrefly": {"other": True}}, strict, "/main/.venv/bin/python")
    assert merged == {"pyrefly": {"other": True, "typeCheckingMode": "strict"}, "pythonPath": "/main/.venv/bin/python"}
    assert proxy.init_options(None, auto, None) == {}


def _touch(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    # File system timestamps can be coarse: shift them by hand so the change is visible.
    t = time.time() + 5
    os.utime(path, (t, t))


def test_disk_watcher_in_git_repo(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "r")
    (root / ".gitignore").write_text("ignored/\n")
    _touch(root / "a.py", "a = 1\n")
    _touch(root / "pkg" / "b.py", "b = 1\n")
    _touch(root / "ignored" / "c.py", "c = 1\n")
    watcher = proxy.DiskWatcher(root)
    assert watcher.changes() == []
    _touch(root / "a.py", "a = 2\n")
    _touch(root / "pkg" / "new.py", "n = 1\n")
    (root / "pkg" / "b.py").unlink()
    _touch(root / "ignored" / "c.py", "c = 2\n")
    _touch(root / "notes.txt", "not code\n")
    changes = {(p.relative_to(root).as_posix(), kind) for p, kind in watcher.changes()}
    assert changes == {("a.py", 2), ("pkg/new.py", 1), ("pkg/b.py", 3)}
    assert watcher.changes() == []


def test_disk_watcher_without_git_skips_venv(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    _touch(root / "a.py", "a = 1\n")
    _touch(root / ".venv" / "lib" / "x.py", "x = 1\n")
    watcher = proxy.DiskWatcher(root)
    _touch(root / ".venv" / "lib" / "x.py", "x = 2\n")
    _touch(root / "pyproject.toml", "[tool.pyrefly]\n")
    assert [(p.name, kind) for p, kind in watcher.changes()] == [("pyproject.toml", 1)]


SOURCE = """import os


class Box:
    def __init__(self, n: int) -> None:
        self.n = n

    def size(self) -> int:
        return len(str(self.n))


def helper() -> int:
    return 1


async def ahelper() -> int:
    return 1


@staticmethod
def decorated() -> int:
    return helper()


async def caller() -> int:
    x = helper()
    y = await ahelper()
    z = helper() + int(os.getenv("N", "0"))
    box = Box(x)

    def nested() -> int:
        return ahelper_never_counted()

    return x + y + z + box.size() + len([nested])
"""


def _names(sites: list[proxy.CallSite]) -> list[str]:
    return sorted(SOURCE.split("\n")[s.line][s.character : s.end_character] for s in sites)


def test_call_sites_cover_await_binop_attribute_and_skip_nested_defs() -> None:
    line = next(i for i, text in enumerate(SOURCE.split("\n")) if text.startswith("async def caller"))
    assert _names(proxy.call_sites(SOURCE, line)) == [
        "Box",
        "ahelper",
        "getenv",
        "helper",
        "helper",
        "int",
        "len",
        "size",
    ]


def test_call_sites_of_decorated_function_and_unknown_line() -> None:
    line = next(i for i, text in enumerate(SOURCE.split("\n")) if text.startswith("def decorated"))
    assert _names(proxy.call_sites(SOURCE, line)) == ["helper"]
    assert proxy.call_sites(SOURCE, 0) == []
    assert proxy.call_sites("def broken(:\n", 0) == []


def _at(name_line: str) -> tuple[int, int]:
    """(line, column of the name) for the SOURCE line that starts with name_line after indentation."""
    for i, text in enumerate(SOURCE.split("\n")):
        if text.strip().startswith(name_line):
            word = name_line.split()[-1].split("(")[0]
            return i, text.index(word, text.index(name_line.split()[0]))
    raise ValueError(name_line)


def test_describe_target_function_method_and_constructor() -> None:
    line, col = _at("def helper")
    item = proxy.describe_target(SOURCE, line, col)
    assert item is not None
    assert (item["name"], item["kind"]) == ("helper", 12)
    assert item["selectionRange"]["start"] == {"line": line, "character": 4}
    assert item["range"]["end"]["line"] == line + 1

    method = proxy.describe_target(SOURCE, *_at("def size"))
    assert method is not None
    assert (method["name"], method["kind"]) == ("size", 6)

    ctor = proxy.describe_target(SOURCE, *_at("def __init__"))
    assert ctor is not None
    # The constructor stays a method: the hierarchy can be walked further from its item.
    assert (ctor["name"], ctor["kind"]) == ("__init__", 6)
    cls = proxy.describe_target(SOURCE, *_at("class Box"))
    assert cls is not None
    assert (cls["name"], cls["kind"]) == ("Box", 5)

    deco = proxy.describe_target(SOURCE, *_at("def decorated"))
    assert deco is not None
    assert deco["name"] == "decorated"
    assert deco["range"]["start"]["line"] == _at("def decorated")[0] - 1  # including the decorator

    assert proxy.describe_target(SOURCE, 0, 0) is None


def test_describe_target_rejects_positions_off_the_name() -> None:
    text = "def make(cls, n):\n    return cls(n)\n"
    # The definition of a callable parameter points at the def line; it is not the function make itself.
    assert proxy.describe_target(text, 0, text.index("cls")) is None
    assert proxy.describe_target(text, 0, 0) is None
    item = proxy.describe_target(text, 0, 4)
    assert item is not None
    assert item["name"] == "make"


def _call(name: str, uri: str, line: int, from_line: int, from_char: int = 4) -> dict:
    rng = {"start": {"line": line, "character": 0}, "end": {"line": line, "character": 3}}
    from_rng = {
        "start": {"line": from_line, "character": from_char},
        "end": {"line": from_line, "character": from_char + 3},
    }
    return {"to": {"name": name, "kind": 12, "uri": uri, "range": rng, "selectionRange": rng}, "fromRanges": [from_rng]}


def test_merge_outgoing_dedupes_by_target_and_range() -> None:
    server = [_call("helper", "file:///m.py", 10, 20)]
    ours = [
        _call("helper", "file:///m.py", 10, 20),
        _call("helper", "file:///m.py", 10, 21),
        _call("other", "file:///o.py", 1, 22),
    ]
    merged = proxy.merge_outgoing(server, ours)
    by_name = {c["to"]["name"]: c for c in merged}
    assert set(by_name) == {"helper", "other"}
    assert sorted(r["start"]["line"] for r in by_name["helper"]["fromRanges"]) == [20, 21]
    assert proxy.merge_outgoing(None, []) == []


def test_merge_outgoing_keeps_every_target_of_one_call_site() -> None:
    # x: A | B: x.f() leads to both methods; the server found one, completion finds both.
    server = [_call("f", "file:///m.py", 10, 30)]
    ours = [_call("f", "file:///m.py", 10, 30), _call("f", "file:///m.py", 15, 30)]
    merged = proxy.merge_outgoing(server, ours)
    assert sorted(c["to"]["selectionRange"]["start"]["line"] for c in merged) == [10, 15]
    assert all(len(c["fromRanges"]) == 1 for c in merged)


def test_describe_target_keeps_dunder_call_a_method() -> None:
    text = "class Handler:\n    def __call__(self) -> int:\n        return 1\n"
    item = proxy.describe_target(text, 1, 8)
    assert item is not None
    assert (item["name"], item["kind"]) == ("__call__", 6)


def _rng(line: int, start: int, end: int) -> dict:
    return {"start": {"line": line, "character": start}, "end": {"line": line, "character": end}}


def _call_to(name: str, kind: int, line: int, *ranges: dict) -> dict:
    target = _rng(line, 4, 4 + len(name))
    return {
        "to": {"name": name, "kind": kind, "uri": "file:///m.py", "range": target, "selectionRange": target},
        "fromRanges": list(ranges),
    }


def test_merge_outgoing_keeps_the_server_constructor_over_our_class() -> None:
    # Box(1): the server gives __init__ (the hierarchy can be walked further from it), completion gives class Box.
    server = [_call_to("__init__", 6, 1, _rng(20, 11, 17))]
    ours = [_call_to("Box", 5, 0, _rng(20, 11, 14))]
    assert [c["to"]["name"] for c in proxy.merge_outgoing(server, ours)] == ["__init__"]


def test_merge_outgoing_keeps_a_nested_call_inside_the_server_range() -> None:
    # helper(helper()): the server gives the range of the whole outer call, completion gives both names.
    server = [_call_to("helper", 12, 5, _rng(20, 11, 27))]
    ours = [_call_to("helper", 12, 5, _rng(20, 11, 17), _rng(20, 18, 24))]
    merged = proxy.merge_outgoing(server, ours)
    assert len(merged) == 1
    assert len(merged[0]["fromRanges"]) == 2


def test_unopened_changes_compare_paths_not_uri_strings(tmp_path: Path) -> None:
    opened = tmp_path / "my+proj" / "a.py"
    other = tmp_path / "my+proj" / "b.py"
    node_style_uri = "file://" + str(opened)  # Node does not encode "+", Python does
    assert node_style_uri != opened.as_uri()
    changes = proxy.unopened_changes([(opened, 2), (other, 1)], [node_style_uri])
    assert [c["type"] for c in changes] == [1]
    assert proxy.uri_to_path(changes[0]["uri"]) == other


def test_settings_expand_user_in_pyrefly_path() -> None:
    s = proxy.Settings.from_env({"PYREFLY_LSP_PYREFLY": "~/.local/bin/pyrefly"})
    assert s.pyrefly == str(Path.home() / ".local" / "bin" / "pyrefly")


def test_worktree_subdirectory_uses_matching_main_subdirectory(tmp_path: Path) -> None:
    main, wt = _repo_with_worktree(tmp_path)
    (wt / "backend").mkdir()
    python = _fake_executable(main / "backend" / ".venv" / "bin" / "python")
    binary = _fake_executable(main / "backend" / ".venv" / "bin" / "pyrefly")
    assert proxy.interpreter_for(wt / "backend", {}) == str(python)
    settings = proxy.Settings.from_env({})
    assert _binary(proxy.pyrefly_command(wt / "backend", settings, lambda _n: None)) == str(binary)


def test_worktree_subdirectory_prefers_the_worktree_own_venv(tmp_path: Path) -> None:
    main, wt = _repo_with_worktree(tmp_path)
    (wt / "backend").mkdir()
    _fake_executable(main / ".venv" / "bin" / "python")
    _fake_executable(main / ".venv" / "bin" / "pyrefly")
    own_python = _fake_executable(wt / ".venv" / "bin" / "python")
    own_binary = _fake_executable(wt / ".venv" / "bin" / "pyrefly")
    assert proxy.interpreter_for(wt / "backend", {}) == str(own_python)
    settings = proxy.Settings.from_env({})
    assert _binary(proxy.pyrefly_command(wt / "backend", settings, lambda _n: None)) == str(own_binary)


def test_disk_watcher_tracks_config_files_migrated_by_auto_mode(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "cfg")
    _touch(root / "a.py", "a = 1\n")
    watcher = proxy.DiskWatcher(root)
    for name in ("mypy.ini", "setup.cfg", "pyrightconfig.json"):
        _touch(root / name, "[mypy]\n")
    assert sorted(p.name for p, _kind in watcher.changes()) == ["mypy.ini", "pyrightconfig.json", "setup.cfg"]


def test_disk_watcher_keeps_its_snapshot_when_git_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = init_repo(tmp_path / "flaky")
    (root / ".gitignore").write_text("gen/\n")
    _touch(root / "a.py", "a = 1\n")
    _touch(root / "gen" / "g.py", "g = 1\n")
    watcher = proxy.DiskWatcher(root, max_entries=3)
    real_git = proxy._git
    monkeypatch.setattr(proxy, "_git", lambda *a, **k: None)
    assert watcher.changes() == []
    assert watcher.enabled is True
    monkeypatch.setattr(proxy, "_git", real_git)
    _touch(root / "a.py", "a = 2\n")
    assert [(p.name, kind) for p, kind in watcher.changes()] == [("a.py", 2)]


def test_disk_watcher_outside_git_skips_virtualenvs_without_a_dot(tmp_path: Path) -> None:
    root = tmp_path / "plainvenv"
    _touch(root / "app.py", "x = 1\n")
    (root / "venv").mkdir()
    (root / "venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    for i in range(40):
        _touch(root / "venv" / "lib" / f"m{i}.py", "y = 1\n")
    watcher = proxy.DiskWatcher(root, max_entries=20)
    assert watcher.enabled is True
    assert watcher.snapshot is not None
    assert all("venv" not in p.parts for p in watcher.snapshot)


def test_encode_survives_lone_surrogates() -> None:
    data = proxy.encode({"text": "\ud83d broken"})
    assert proxy.read_message(io.BytesIO(data)) == {"text": "\ud83d broken"}


def test_disk_watcher_outside_git_switches_off_on_a_large_tree(tmp_path: Path) -> None:
    root = tmp_path / "big"
    for i in range(30):
        _touch(root / f"d{i}" / "m.py", "x = 1\n")
    watcher = proxy.DiskWatcher(root, max_entries=10)
    assert watcher.enabled is False
    _touch(root / "d0" / "new.py", "y = 1\n")
    assert watcher.changes() == []
    small = proxy.DiskWatcher(tmp_path / "big" / "d1", max_entries=10)
    assert small.enabled is True


# --- proxy with a fake server -------------------------------------------------------------


class _Recorder(io.RawIOBase):
    """The fake server's stdin: collects the messages sent to it."""

    def __init__(self) -> None:
        self.buffer = b""

    def writable(self) -> bool:
        return True

    def write(self, data: bytes | bytearray | memoryview) -> int:  # type: ignore[override]
        self.buffer += bytes(data)
        return len(data)

    def messages(self) -> list[dict]:
        stream = io.BytesIO(self.buffer)
        out = []
        while (msg := proxy.read_message(stream)) is not None:
            out.append(msg)
        return out


class _FakeServer:
    def __init__(self) -> None:
        self.stdin = _Recorder()

    def poll(self) -> None:
        return None


def _proxy(root: Path) -> tuple[proxy.Proxy, _FakeServer]:
    p = proxy.Proxy(proxy.Settings.from_env({}), {}, io.BytesIO(), io.BytesIO())
    server = _FakeServer()
    p.server = server  # type: ignore[assignment]
    p.watcher = proxy.DiskWatcher(root)
    return p, server


def _open(p: proxy.Proxy, path: Path, text: str) -> str:
    uri = path.as_uri()
    p.from_client(
        {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": text}},
        }
    )
    return uri


def test_did_open_always_reaches_server_before_a_disk_resync(tmp_path: Path) -> None:
    import threading

    root = tmp_path / "race"
    root.mkdir()
    for i in range(200):
        path = root / f"f{i}.py"
        path.write_text("old\n")
        p, server = _proxy(root)
        path.write_text("new text on disk\n")
        opener = threading.Thread(target=_open, args=(p, path, "old\n"))
        opener.start()
        p.sync()
        opener.join()
        p.sync()
        methods = [
            m["method"]
            for m in server.stdin.messages()
            if m["params"].get("textDocument", {}).get("uri") == path.as_uri()
        ]
        assert methods[0] == "textDocument/didOpen", methods


def test_incremental_change_after_resync_takes_the_text_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "c.py"
    path.write_text("a = 1\n")
    p, server = _proxy(tmp_path)
    uri = _open(p, path, "a = 1\n")
    _touch(path, "a = 1\nb = 2\n")  # outside Claude
    p.sync()
    # Claude edits its own (old) buffer with a fragment and writes the result to disk.
    _touch(path, "z = 0\na = 1\n")
    change = {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}, "text": "z = 0\n"}
    p.from_client(
        {
            "jsonrpc": "2.0",
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [change]},
        }
    )
    last = [m for m in server.stdin.messages() if m.get("method") == "textDocument/didChange"][-1]
    assert last["params"]["contentChanges"] == [{"text": "z = 0\na = 1\n"}]
    assert p.docs[uri].text == "z = 0\na = 1\n"


@NOT_ROOT
def test_unreadable_file_is_retried_on_the_next_sync(tmp_path: Path) -> None:
    path = tmp_path / "c.py"
    path.write_text("a = 1\n")
    p, server = _proxy(tmp_path)
    _open(p, path, "a = 1\n")
    _touch(path, "a = 2\n")
    path.chmod(0)  # temporarily unreadable; permissions come back, content and mtime stay the same
    try:
        p.sync()
    finally:
        path.chmod(0o644)
    p.sync()
    sent = [
        m["params"]["contentChanges"][0]["text"]
        for m in server.stdin.messages()
        if m.get("method") == "textDocument/didChange"
    ]
    assert sent == ["a = 2\n"]


def test_sync_failure_on_request_path_does_not_escape(tmp_path: Path) -> None:
    p, server = _proxy(tmp_path)

    def boom() -> list:
        raise ValueError("sync failure")

    p.watcher.changes = boom  # type: ignore[method-assign, union-attr]
    p.from_client({"jsonrpc": "2.0", "id": 7, "method": "textDocument/hover", "params": {}})
    assert [m.get("id") for m in server.stdin.messages()] == [7]


def test_definition_timeouts_do_not_leak_pending_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "m.py"
    text = "def f() -> int:\n    return g() + h()\n"
    path.write_text(text)
    p, _server = _proxy(tmp_path)
    _open(p, path, text)
    monkeypatch.setattr(proxy, "DEFINITION_TIMEOUT", 0.05)
    item = {
        "uri": path.as_uri(),
        "selectionRange": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 5}},
    }
    assert p.compute_outgoing(item) == []
    assert p.pending == {}


def test_definition_index_is_cached_and_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p, _server = _proxy(tmp_path)
    monkeypatch.setattr(proxy, "INDEX_CACHE_SIZE", 3)
    files = []
    for i in range(5):
        path = tmp_path / f"t{i}.py"
        path.write_text(f"def f{i}() -> int:\n    return {i}\n")
        files.append(path.as_uri())
    first = p.definition_index(files[0])
    assert p.definition_index(files[0]) is first
    for uri in files[1:]:
        p.definition_index(uri)
    assert len(p.indexes) == 3
    assert p.describe(files[4], 0, 4)["name"] == "f4"  # type: ignore[index]


def test_did_open_with_text_differing_from_disk_is_resynced(tmp_path: Path) -> None:
    # Claude wrote `x=2`, then a formatter rewrote the file: the client's text is older than the disk.
    path = tmp_path / "f.py"
    path.write_text("x = 2\n")
    p, server = _proxy(tmp_path)
    _open(p, path, "x=2\n")
    p.sync()
    sent = [
        m["params"]["contentChanges"][0]["text"]
        for m in server.stdin.messages()
        if m.get("method") == "textDocument/didChange"
    ]
    assert sent == ["x = 2\n"]


def test_did_change_with_text_differing_from_disk_is_resynced(tmp_path: Path) -> None:
    path = tmp_path / "f.py"
    path.write_text("x = 1\n")
    p, server = _proxy(tmp_path)
    uri = _open(p, path, "x = 1\n")
    _touch(path, "x = 2\n")  # the formatter after Claude's write
    change = {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": "x=2\n"}]}
    p.from_client({"jsonrpc": "2.0", "method": "textDocument/didChange", "params": change})
    p.sync()
    sent = [
        m["params"]["contentChanges"][0]["text"]
        for m in server.stdin.messages()
        if m.get("method") == "textDocument/didChange"
    ]
    assert sent[-1] == "x = 2\n"


def test_deleted_open_document_is_reported_as_deleted(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "del")
    path = root / "c.py"
    path.write_text("def c_one() -> int:\n    return 1\n")
    p, server = _proxy(root)
    _open(p, path, path.read_text())
    path.unlink()
    p.sync()
    msgs = server.stdin.messages()
    assert any(m.get("method") == "textDocument/didClose" for m in msgs)
    watched = [c for m in msgs if m.get("method") == "workspace/didChangeWatchedFiles" for c in m["params"]["changes"]]
    assert watched == [{"uri": path.as_uri(), "type": 3}]


@NOT_ROOT
def test_unreadable_disk_with_incremental_change_does_not_corrupt_the_document(tmp_path: Path) -> None:
    path = tmp_path / "c.py"
    path.write_text("a = 1\n")
    p, server = _proxy(tmp_path)
    uri = _open(p, path, "a = 1\n")
    _touch(path, "a = 1\nb = 2\n")
    p.sync()  # the disk text is substituted
    before = len(server.stdin.messages())
    path.chmod(0)
    try:
        change = {
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
            "text": "z = 0\n",
        }
        p.from_client(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didChange",
                "params": {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [change]},
            }
        )
        assert len(server.stdin.messages()) == before  # the corrupted text did not reach the server
        assert p.docs[uri].text == "a = 1\nb = 2\n"
    finally:
        path.chmod(0o644)
    _touch(path, "z = 0\na = 1\nb = 2\n")
    p.sync()
    last = [m for m in server.stdin.messages() if m.get("method") == "textDocument/didChange"][-1]
    assert last["params"]["contentChanges"] == [{"text": "z = 0\na = 1\nb = 2\n"}]


def test_index_of_an_open_document_follows_its_version(tmp_path: Path) -> None:
    path = tmp_path / "f.py"
    path.write_text("def a() -> None:\n    pass\n")
    p, _server = _proxy(tmp_path)
    uri = _open(p, path, path.read_text())
    assert p.describe(uri, 0, 4)["name"] == "a"  # type: ignore[index]
    # Claude's buffer moved ahead while the disk is still old: the index must match the buffer.
    change = {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": "def b() -> None:\n    pass\n"}]}
    p.from_client({"jsonrpc": "2.0", "method": "textDocument/didChange", "params": change})
    assert p.describe(uri, 0, 4)["name"] == "b"  # type: ignore[index]


def test_scan_runs_outside_the_document_lock_and_is_skipped_when_fresh(tmp_path: Path) -> None:
    p, _server = _proxy(tmp_path)
    calls = []

    class _Watcher:
        enabled = True
        cost = 0.5  # a costly repository
        last_scan = time.monotonic()

        def changes(self) -> list:
            calls.append(p.doc_lock.locked())
            return []

    p.watcher = _Watcher()  # type: ignore[assignment]
    p.from_client({"jsonrpc": "2.0", "id": 1, "method": "textDocument/hover", "params": {}})
    assert calls == []  # the background just scanned: not repeated on the request path
    p.sync()
    assert calls == [False]  # the background scan runs without the document lock


@NOT_ROOT
def test_unreadable_directory_outside_git_does_not_break_the_watcher(tmp_path: Path) -> None:
    root = tmp_path / "plain_locked"
    _touch(root / "a.py", "a = 1\n")
    locked = root / "data" / "locked"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        watcher = proxy.DiskWatcher(root)
        assert watcher.enabled is True
        assert watcher.snapshot is not None
        assert (root / "a.py") in watcher.snapshot
    finally:
        locked.chmod(0o755)


@NOT_ROOT
def test_unreadable_venv_falls_back_to_path(tmp_path: Path) -> None:
    root = tmp_path / "lockedvenv"
    (root / ".venv").mkdir(parents=True)
    (root / ".venv").chmod(0)
    try:
        command = proxy.pyrefly_command(root, proxy.Settings.from_env({}), lambda _n: "/usr/bin/pyrefly")
        assert _binary(command) == "/usr/bin/pyrefly"
        assert proxy.interpreter_for(root, {}) is None
    finally:
        (root / ".venv").chmod(0o755)


def test_poll_keeps_syncing_open_documents_when_the_watcher_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    path = tmp_path / "c.py"
    path.write_text("a = 1\n")
    p, server = _proxy(tmp_path)
    p.watcher = None
    _open(p, path, "a = 1\n")

    def broken(*_a: object, **_k: object) -> None:
        raise RuntimeError("watching failed to start")

    monkeypatch.setattr(proxy, "DiskWatcher", broken)
    monkeypatch.setattr(proxy, "POLL_INTERVAL", 0.05)
    thread = threading.Thread(target=p.poll_disk, args=(tmp_path,), daemon=True)
    thread.start()
    _touch(path, "a = 2\n")
    deadline = time.time() + 5
    while time.time() < deadline and not any(
        m.get("method") == "textDocument/didChange" for m in server.stdin.messages()
    ):
        time.sleep(0.05)
    p.stopped.set()
    thread.join(timeout=2)
    assert any(m.get("method") == "textDocument/didChange" for m in server.stdin.messages())


def test_reopened_document_gets_a_fresh_index(tmp_path: Path) -> None:
    path = tmp_path / "m.py"
    path.write_text("def old() -> None:\n    pass\n")
    p, _server = _proxy(tmp_path)
    uri = _open(p, path, path.read_text())
    assert p.describe(uri, 0, 4)["name"] == "old"  # type: ignore[index]
    p.from_client({"jsonrpc": "2.0", "method": "textDocument/didClose", "params": {"textDocument": {"uri": uri}}})
    path.write_text("def new() -> None:\n    pass\n")
    _open(p, path, path.read_text())
    assert p.describe(uri, 0, 4)["name"] == "new"  # type: ignore[index]


def test_files_with_a_bom_are_parsed() -> None:
    text = "\ufeffdef f() -> int:\n    return g()\n"
    # The BOM is the document's first character: first-line positions shift by one UTF-16 unit.
    assert [(s.line, s.character) for s in proxy.call_sites(text, 0)] == [(1, 11)]
    item = proxy.describe_target(text, 0, 5)
    assert item is not None
    assert item["name"] == "f"
    assert item["selectionRange"]["start"] == {"line": 0, "character": 5}


def test_project_ignored_by_an_enclosing_repo_is_watched(tmp_path: Path) -> None:
    outer = init_repo(tmp_path / "outer")
    (outer / ".gitignore").write_text("proj/\n")
    root = outer / "proj"
    _touch(root / "a.py", "a = 1\n")
    watcher = proxy.DiskWatcher(root)
    assert watcher.snapshot is not None
    assert (root / "a.py") in watcher.snapshot
    _touch(root / "a.py", "a = 2\n")
    assert [(p.name, kind) for p, kind in watcher.changes()] == [("a.py", 2)]


def test_merge_outgoing_keeps_a_class_nested_in_a_constructor_call() -> None:
    # Outer(Inner()): Outer's constructor range also covers Inner(); only Outer must be dropped.
    server = [_call_to("__init__", 6, 1, _rng(20, 11, 27))]
    ours = [_call_to("Outer", 5, 0, _rng(20, 11, 16)), _call_to("Inner", 5, 3, _rng(20, 17, 22))]
    assert sorted(c["to"]["name"] for c in proxy.merge_outgoing(server, ours)) == ["Inner", "__init__"]


def test_request_after_a_background_send_waits_for_the_recheck(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "bg")
    opened = root / "m.py"
    opened.write_text("x = 1\n")
    p, _server = _proxy(root)
    _open(p, opened, "x = 1\n")
    _touch(root / "b.py", "y = 1\n")
    assert p.sync() is not None  # the background found and sent it
    settled = []
    p.settle = settled.append  # type: ignore[method-assign]
    p.from_client({"jsonrpc": "2.0", "id": 3, "method": "textDocument/hover", "params": {}})
    assert len(settled) == 1
    p.from_client({"jsonrpc": "2.0", "id": 4, "method": "textDocument/hover", "params": {}})
    assert len(settled) == 1  # the second request no longer waits


def test_a_broken_document_does_not_lose_found_changes(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "nul")
    p, server = _proxy(root)
    p.from_client(
        {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {"uri": "file:///tmp/bad%00name.py", "languageId": "python", "version": 1, "text": "x"}
            },
        }
    )
    _touch(root / "b.py", "y = 1\n")
    p.sync()
    watched = [
        c["uri"]
        for m in server.stdin.messages()
        if m.get("method") == "workspace/didChangeWatchedFiles"
        for c in m["params"]["changes"]
    ]
    assert watched == [(root / "b.py").as_uri()]


def test_indexing_limit_is_passed_to_pyrefly(tmp_path: Path) -> None:
    settings = proxy.Settings.from_env({"PYREFLY_LSP_INDEXING_LIMIT": "10000"})
    command = proxy.pyrefly_command(tmp_path, settings, lambda _n: "/usr/bin/pyrefly")
    assert command is not None
    assert command[-2:] == ["--workspace-indexing-limit", "10000"]
    assert proxy.Settings.from_env({"PYREFLY_LSP_INDEXING_LIMIT": "10000.0"}).indexing_limit == 10000
    assert proxy.Settings.from_env({"PYREFLY_LSP_INDEXING_LIMIT": "0"}).indexing_limit is None
    default = proxy.pyrefly_command(tmp_path, proxy.Settings.from_env({}), lambda _n: "/usr/bin/pyrefly")
    assert default is not None
    assert "--workspace-indexing-limit" not in default


def test_disabled_sync_does_not_read_the_disk_on_open_and_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f.py"
    path.write_text("x = 1\n")
    p = proxy.Proxy(proxy.Settings.from_env({"PYREFLY_LSP_SYNC_FROM_DISK": "false"}), {}, io.BytesIO(), io.BytesIO())
    p.server = _FakeServer()  # type: ignore[assignment]

    def no_reads(*_a: object, **_k: object) -> None:
        raise AssertionError("the disk must not be read")

    monkeypatch.setattr(proxy, "_read_text", no_reads)
    uri = _open(p, path, "x = 1\n")
    change = {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": "x = 2\n"}]}
    p.from_client({"jsonrpc": "2.0", "method": "textDocument/didChange", "params": change})


def test_cached_index_does_not_read_the_target_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "t.py"
    path.write_text("def f() -> int:\n    return 1\n")
    p, _server = _proxy(tmp_path)
    assert p.describe(path.as_uri(), 0, 4)["name"] == "f"  # type: ignore[index]
    reads: list[Path] = []
    real = Path.read_bytes

    def counting(self: Path) -> bytes:
        reads.append(self)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    assert p.describe(path.as_uri(), 0, 4)["name"] == "f"  # type: ignore[index]
    assert path not in reads


def test_unparsable_file_is_logged_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "new_syntax.py"
    text = "def f() -> int:\n    return g(\n"
    path.write_text(text)
    p, _server = _proxy(tmp_path)
    _open(p, path, text)
    item = {
        "uri": path.as_uri(),
        "selectionRange": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 5}},
    }
    assert p.compute_outgoing(item) == []
    assert p.compute_outgoing(item) == []
    assert capsys.readouterr().err.count("could not parse") == 1


@pytest.mark.skipif(not __import__("shutil").which("python3.9"), reason="no python3.9")
def test_proxy_module_imports_on_python39() -> None:
    import subprocess

    from conftest import PROXY

    done = subprocess.run(
        ["python3.9", "-c", f"import runpy; runpy.run_path({str(PROXY)!r}, run_name='check')"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    ("value", "expected"), [("true", True), ("1", True), ("False", False), ("0", False), ("off", False)]
)
def test_bool_parsing(value: str, expected: bool) -> None:
    assert proxy.Settings.from_env({"PYREFLY_LSP_DIAGNOSTICS": value}).diagnostics is expected

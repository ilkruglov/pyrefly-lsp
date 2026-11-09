"""End-to-end tests: real pyrefly behind the proxy, and a client that behaves like Claude Code."""

from __future__ import annotations

import json
import subprocess
import sys
import sysconfig
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import PROXY, LspClient, git, init_repo, needs_pyrefly, position, target_files

pytestmark = needs_pyrefly

M_PY = """from b import old_func


def helper() -> int:
    return old_func()


async def ahelper() -> int:
    return 1


async def acaller() -> int:
    x = helper()
    y = await ahelper()
    return x + y


def scaller() -> int:
    return helper() + helper()
"""


def _outgoing(client: LspClient, uri: str, text: str, fn: str) -> list[str]:
    items = client.request(
        "textDocument/prepareCallHierarchy", {"textDocument": {"uri": uri}, "position": position(text, f"def {fn}", 4)}
    )
    calls = client.request("callHierarchy/outgoingCalls", {"item": items[0]})
    return sorted(f"{c['to']['name']}×{len(c['fromRanges'])}" for c in calls)


def test_outgoing_calls_include_await_binop_and_imports(
    make_project: Callable[..., Path], client_factory: Callable[..., LspClient]
) -> None:
    root = make_project({"m.py": M_PY, "b.py": "def old_func() -> int:\n    return 1\n"})
    client = client_factory(root)
    uri = client.open("m.py")
    assert _outgoing(client, uri, M_PY, "acaller") == ["ahelper×1", "helper×1"]
    assert _outgoing(client, uri, M_PY, "scaller") == ["helper×2"]
    assert _outgoing(client, uri, M_PY, "helper") == ["old_func×1"]


def test_outgoing_calls_left_to_server_when_disabled(
    make_project: Callable[..., Path], client_factory: Callable[..., LspClient]
) -> None:
    root = make_project({"m.py": M_PY, "b.py": "def old_func() -> int:\n    return 1\n"})
    client = client_factory(root, env={"PYREFLY_LSP_COMPLETE_OUTGOING_CALLS": "false"})
    uri = client.open("m.py")
    # Without completion the reply is only what pyrefly found itself (1.4.1 loses `return a() + b()`,
    # facebook/pyrefly#5486); the proxy adds nothing extra.
    assert set(_outgoing(client, uri, M_PY, "scaller")) <= {"helper×2", "helper×1"}


CALLS_PY = """class Box:
    def __init__(self, n: int) -> None:
        self.n = n

    @classmethod
    def make(cls, n: int) -> "Box":
        return cls(n) or cls(0)


class A:
    def f(self) -> int:
        return 1


class B:
    def f(self) -> int:
        return 2


def build() -> Box:
    return Box(1)


def run(callback) -> int:
    return callback() + callback()


def use(x: A | B) -> int:
    return x.f() + 1
"""


def test_outgoing_calls_do_not_report_called_parameters_as_recursion(
    make_project: Callable[..., Path], client_factory: Callable[..., LspClient]
) -> None:
    root = make_project({"calls.py": CALLS_PY})
    client = client_factory(root)
    uri = client.open("calls.py")
    assert "make×2" not in _outgoing(client, uri, CALLS_PY, "make")
    assert _outgoing(client, uri, CALLS_PY, "run") == []


def test_outgoing_calls_report_a_constructor_once(
    make_project: Callable[..., Path], client_factory: Callable[..., LspClient]
) -> None:
    root = make_project({"calls.py": CALLS_PY})
    client = client_factory(root)
    uri = client.open("calls.py")
    calls = _outgoing(client, uri, CALLS_PY, "build")
    assert len(calls) == 1, calls


def test_outgoing_calls_cover_every_definition_of_a_union_method(
    make_project: Callable[..., Path], client_factory: Callable[..., LspClient]
) -> None:
    root = make_project({"calls.py": CALLS_PY})
    client = client_factory(root)
    uri = client.open("calls.py")
    items = client.request(
        "textDocument/prepareCallHierarchy",
        {"textDocument": {"uri": uri}, "position": position(CALLS_PY, "def use", 4)},
    )
    calls = client.request("callHierarchy/outgoingCalls", {"item": items[0]})
    lines = sorted(c["to"]["selectionRange"]["start"]["line"] for c in calls if c["to"]["name"] == "f")
    assert lines == [10, 15]


TRAVERSE_PY = """def helper() -> int:
    return 1


class Box:
    def __init__(self) -> None:
        self.v = helper()


def caller() -> Box:
    return Box()


def nested() -> int:
    return helper(helper()) if False else 0
"""


def test_outgoing_calls_through_a_constructor_can_be_followed(
    make_project: Callable[..., Path], client_factory: Callable[..., LspClient]
) -> None:
    root = make_project({"t.py": TRAVERSE_PY.replace("helper(helper())", "max(helper(), helper())")})
    client = client_factory(root)
    uri = client.open("t.py")
    text = (root / "t.py").read_text()
    items = client.request(
        "textDocument/prepareCallHierarchy", {"textDocument": {"uri": uri}, "position": position(text, "def caller", 4)}
    )
    calls = client.request("callHierarchy/outgoingCalls", {"item": items[0]})
    assert len(calls) == 1
    deeper = client.request("callHierarchy/outgoingCalls", {"item": calls[0]["to"]})
    assert "helper" in [c["to"]["name"] for c in deeper]


def test_outgoing_calls_keep_a_nested_call(
    make_project: Callable[..., Path], client_factory: Callable[..., LspClient]
) -> None:
    text = "def helper(x: int = 0) -> int:\n    return x\n\n\ndef nested() -> int:\n    return helper(helper())\n"
    root = make_project({"n.py": text})
    client = client_factory(root)
    uri = client.open("n.py")
    assert _outgoing(client, uri, text, "nested") == ["helper×2"]


def test_deleted_open_module_is_gone_for_the_server(
    make_project: Callable[..., Path], client_factory: Callable[..., LspClient]
) -> None:
    text = "from c import c_one\n\nX = c_one()\n"
    root = make_project({"m.py": text, "c.py": "def c_one() -> int:\n    return 1\n"})
    client = client_factory(root)
    uri = client.open("m.py")
    client.open("c.py")
    client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
    (root / "c.py").unlink()
    time.sleep(2.5)  # background sync
    definition = client.request(
        "textDocument/definition", {"textDocument": {"uri": uri}, "position": position(text, "X = c_one", 4)}
    )
    assert "c.py" not in target_files(definition)


def test_references_complete_on_first_request(
    make_project: Callable[..., Path], client_factory: Callable[..., LspClient]
) -> None:
    files = {"lib.py": "class Street:\n    pass\n"}
    for i in range(120):
        files[f"pkg/use_{i}.py"] = "from lib import Street\n\n\ndef f(s: Street) -> Street:\n    return s\n"
    root = make_project(files)
    client = client_factory(root)
    uri = client.open("lib.py")
    params = {
        "textDocument": {"uri": uri},
        "position": {"line": 0, "character": 6},
        "context": {"includeDeclaration": True},
    }
    first = client.request("textDocument/references", params)
    assert len({r["uri"] for r in first}) == 121
    time.sleep(2)
    assert len(client.request("textDocument/references", params)) == len(first)


def test_unopened_file_changed_on_disk_is_seen(
    make_project: Callable[..., Path], client_factory: Callable[..., LspClient]
) -> None:
    root = make_project(
        {"m.py": "from b import old_func\n\nX = old_func()\n", "b.py": "def old_func() -> int:\n    return 1\n"}
    )
    client = client_factory(root)
    client.open("m.py")
    client.request("textDocument/documentSymbol", {"textDocument": {"uri": (root / "m.py").as_uri()}})
    # The file is appended outside Claude (git pull, another agent, a formatter); Claude Code doesn't tell the server.
    with open(root / "b.py", "a") as fh:
        fh.write("\n\ndef new_func() -> int:\n    return 2\n")
    text = "from b import old_func, new_func\n\nX = old_func()\nY = new_func()\n"
    mark = time.time()
    uri = client.edit("m.py", text)
    definition = client.request(
        "textDocument/definition", {"textDocument": {"uri": uri}, "position": position(text, "Y = new_func", 4)}
    )
    assert target_files(definition) == ["b.py"]
    time.sleep(1)
    assert not any("new_func" in d["message"] for d in client.diagnostics_for(uri, since=mark))


def test_disk_change_reaches_server_before_the_next_request(
    make_project: Callable[..., Path], client_factory: Callable[..., LspClient]
) -> None:
    root = make_project(
        {"m.py": "from b import old_func\n\nX = old_func()\n", "b.py": "def old_func() -> int:\n    return 1\n"}
    )
    client = client_factory(root)
    uri = client.open("m.py")
    client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
    with open(root / "b.py", "a") as fh:
        fh.write("\n\ndef new_func() -> int:\n    return 2\n")
    # As in real use: seconds pass between the disk edit and Claude's next call.
    time.sleep(2.5)
    text = "from b import old_func, new_func\n\nX = old_func()\nY = new_func()\n"
    mark = time.time()
    uri = client.edit("m.py", text)
    time.sleep(1)
    assert not any("new_func" in d["message"] for d in client.diagnostics_for(uri, since=mark))
    definition = client.request(
        "textDocument/definition", {"textDocument": {"uri": uri}, "position": position(text, "Y = new_func", 4)}
    )
    assert target_files(definition) == ["b.py"]


def test_open_file_changed_on_disk_is_seen(
    make_project: Callable[..., Path], client_factory: Callable[..., LspClient]
) -> None:
    root = make_project({"c.py": "def c_one() -> int:\n    return 1\n"})
    client = client_factory(root)
    uri = client.open("c.py")
    before = client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
    assert [s["name"] for s in before] == ["c_one"]
    with open(root / "c.py", "a") as fh:
        fh.write("\n\ndef c_two() -> int:\n    return 2\n")
    after = client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
    assert [s["name"] for s in after] == ["c_one", "c_two"]
    # Then Claude edits the same file: the server's document versions must not break.
    uri = client.edit("c.py", "def c_one() -> int:\n    return 1\n\n\ndef c_three() -> int:\n    return 3\n")
    final = client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
    assert [s["name"] for s in final] == ["c_one", "c_three"]


def test_open_file_deleted_then_recreated(
    make_project: Callable[..., Path], client_factory: Callable[..., LspClient]
) -> None:
    root = make_project({"c.py": "def c_one() -> int:\n    return 1\n", "m.py": "x = 1\n"})
    client = client_factory(root)
    uri = client.open("c.py")
    client.open("m.py")
    (root / "c.py").unlink()
    client.request("textDocument/documentSymbol", {"textDocument": {"uri": (root / "m.py").as_uri()}})
    uri = client.edit("c.py", "def c_back() -> int:\n    return 1\n")
    symbols = client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
    assert [s["name"] for s in symbols] == ["c_back"]


def test_diagnostics_reported_by_default_and_dropped_when_disabled(
    make_project: Callable[..., Path], client_factory: Callable[..., LspClient]
) -> None:
    bad = 'X: int = "not an int"\n'
    on = make_project({"m.py": "X: int = 1\n"}, name="on")
    client = client_factory(on)
    client.open("m.py")
    mark = time.time()
    uri = client.edit("m.py", bad)
    deadline = time.time() + 20
    while not client.diagnostics_for(uri, since=mark) and time.time() < deadline:
        time.sleep(0.1)
    assert any("not assignable" in d["message"] for d in client.diagnostics_for(uri, since=mark))

    off = make_project({"m.py": "X: int = 1\n"}, name="off")
    quiet = client_factory(off, env={"PYREFLY_LSP_DIAGNOSTICS": "false"})
    quiet.open("m.py")
    mark = time.time()
    quiet.edit("m.py", bad)
    time.sleep(3)
    assert quiet.published(since=mark) == 0
    # Navigation still works.
    assert quiet.request("textDocument/documentSymbol", {"textDocument": {"uri": (off / "m.py").as_uri()}})


def test_worktree_without_venv_uses_main_checkout_venv(
    tmp_path: Path, client_factory: Callable[..., LspClient]
) -> None:
    main = init_repo(tmp_path / "main")
    (main / "pyproject.toml").write_text('[tool.pyrefly]\nproject-includes = ["."]\n')
    (main / "app.py").write_text("import fakelib\n\nVALUE = fakelib.ANSWER\n")
    (main / ".gitignore").write_text(".venv/\n")
    git(main, "add", ".")
    git(main, "commit", "-qm", "init")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(main / ".venv")], check=True)
    purelib = Path(sysconfig.get_path("purelib", vars={"base": str(main / ".venv")}))
    purelib.mkdir(parents=True, exist_ok=True)
    (purelib / "fakelib.py").write_text("ANSWER: int = 42\n")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", str(wt))
    assert not (wt / ".venv").exists()

    client = client_factory(wt)
    uri = client.open("app.py")
    text = (wt / "app.py").read_text()
    definition = client.request(
        "textDocument/definition", {"textDocument": {"uri": uri}, "position": position(text, "fakelib.ANSWER", 8)}
    )
    assert target_files(definition) == ["fakelib.py"]


def test_missing_pyrefly_reports_clear_error(make_project: Callable[..., Path]) -> None:
    root = make_project({"m.py": "x = 1\n"})
    client = LspClient(root, env={"PYREFLY_LSP_PYREFLY": str(root / "no-such-pyrefly")})
    with pytest.raises(RuntimeError, match="pyrefly"):
        client.initialize()
    client.proc.wait(timeout=10)
    assert client.proc.returncode != 0
    assert "pyrefly" in client.stderr()


def test_proxy_exits_when_client_goes_away(make_project: Callable[..., Path]) -> None:
    root = make_project({"m.py": "x = 1\n"})
    client = LspClient(root)
    client.initialize()
    assert client.proc.stdin is not None
    client.proc.stdin.close()
    client.proc.wait(timeout=15)
    # A normal end of the session is not a crash: exit code 0.
    assert client.proc.returncode == 0


def test_proxy_is_isolated_from_the_project_pythonpath(make_project: Callable[..., Path]) -> None:
    # The user started Claude with PYTHONPATH=. and the project has a json.py.
    root = make_project({"json.py": "raise SystemExit('shadowed json')\n", "m.py": "x = 1\n"})
    lsp = json.loads((Path(__file__).resolve().parents[1] / "plugins" / "pyrefly-lsp" / ".lsp.json").read_text())
    args = [a.replace("${CLAUDE_PLUGIN_ROOT}", str(PROXY.parents[1])) for a in lsp["pyrefly"]["args"]]
    client = LspClient(root, env={"PYTHONPATH": str(root)}, command=[sys.executable, *args])
    try:
        assert "capabilities" in client.initialize()
    finally:
        client.close()

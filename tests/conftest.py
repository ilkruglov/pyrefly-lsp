"""Общее для тестов: путь к прослойке, LSP-клиент как у Claude Code, временные проекты."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "pyrefly-lsp" / "scripts"
PROXY = SCRIPTS / "pyrefly_lsp_proxy.py"
sys.path.insert(0, str(SCRIPTS))

# Возможности клиента, которые Claude Code 2.1.319 объявляет в initialize (сняты из живого лога):
# слежения за файлами и workspace/configuration нет — ровно те условия, ради которых прослойка.
CLAUDE_CODE_CAPABILITIES: dict[str, Any] = {
    "workspace": {
        "configuration": False,
        "workspaceFolders": False,
        "didChangeWatchedFiles": {"dynamicRegistration": False},
    },
    "textDocument": {
        "synchronization": {
            "dynamicRegistration": False,
            "willSave": False,
            "willSaveWaitUntil": False,
            "didSave": True,
        },
        "publishDiagnostics": {"relatedInformation": True, "tagSupport": {"valueSet": [1, 2]}, "versionSupport": False},
        "hover": {"dynamicRegistration": False, "contentFormat": ["markdown", "plaintext"]},
        "definition": {"dynamicRegistration": False, "linkSupport": True},
        "references": {"dynamicRegistration": False},
        "documentSymbol": {"dynamicRegistration": False, "hierarchicalDocumentSymbolSupport": True},
        "callHierarchy": {"dynamicRegistration": False},
    },
    "general": {"positionEncodings": ["utf-16"]},
}

needs_pyrefly = pytest.mark.skipif(shutil.which("pyrefly") is None, reason="pyrefly не установлен")


class LspClient:
    """Клиент LSP, который ведёт себя как Claude Code: шлёт полный текст в didChange, не следит за диском."""

    def __init__(self, root: Path, env: dict[str, str] | None = None, command: list[str] | None = None) -> None:
        self.root = root
        full_env = {**os.environ, **(env or {})}
        self.proc = subprocess.Popen(
            command or [sys.executable, str(PROXY)],
            cwd=root,
            env=full_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.responses: dict[int, dict[str, Any]] = {}
        self.notifications: list[tuple[float, dict[str, Any]]] = []
        self.versions: dict[str, int] = {}
        self._next = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _send(self, msg: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        body = json.dumps(msg).encode()
        with self._lock:
            self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            self.proc.stdin.flush()

    def _read(self) -> None:
        out = self.proc.stdout
        assert out is not None
        while True:
            head = b""
            while not head.endswith(b"\r\n\r\n"):
                ch = out.read(1)
                if not ch:
                    return
                head += ch
            length = next(
                int(line.split(b":")[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length")
            )
            msg = json.loads(out.read(length))
            if "method" in msg and "id" in msg:
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
            elif "id" in msg:
                self.responses[msg["id"]] = msg
            else:
                self.notifications.append((time.time(), msg))

    def request(self, method: str, params: dict[str, Any], timeout: float = 60) -> Any:
        self._next += 1
        rid = self._next
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while rid not in self.responses:
            if time.time() > deadline:
                raise TimeoutError(method)
            if self.proc.poll() is not None and rid not in self.responses:
                raise RuntimeError(f"прослойка завершилась с кодом {self.proc.returncode}: {self.stderr()}")
            time.sleep(0.01)
        msg = self.responses.pop(rid)
        if "error" in msg:
            raise RuntimeError(msg["error"])
        return msg.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def initialize(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        uri = self.root.as_uri()
        result = self.request(
            "initialize",
            {
                "processId": None,
                "clientInfo": {"name": "Claude Code", "version": "2.1.319"},
                "rootUri": uri,
                "rootPath": str(self.root),
                "workspaceFolders": [{"uri": uri, "name": self.root.name}],
                "initializationOptions": options or {},
                "capabilities": CLAUDE_CODE_CAPABILITIES,
            },
            timeout=120,
        )
        self.notify("initialized", {})
        return result

    def open(self, rel: str) -> str:
        path = self.root / rel
        uri = path.as_uri()
        self.versions[uri] = 1
        self.notify(
            "textDocument/didOpen",
            {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": path.read_text()}},
        )
        return uri

    def edit(self, rel: str, text: str) -> str:
        """Как Edit в Claude Code: пишет файл на диск, затем шлёт didChange с полным текстом и didSave."""
        path = self.root / rel
        path.write_text(text)
        uri = path.as_uri()
        self.versions[uri] += 1
        self.notify(
            "textDocument/didChange",
            {"textDocument": {"uri": uri, "version": self.versions[uri]}, "contentChanges": [{"text": text}]},
        )
        self.notify("textDocument/didSave", {"textDocument": {"uri": uri}})
        return uri

    def diagnostics_for(self, uri: str, since: float = 0) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t, msg in self.notifications:
            if t >= since and msg.get("method") == "textDocument/publishDiagnostics" and msg["params"]["uri"] == uri:
                out = msg["params"]["diagnostics"]
        return out

    def published(self, since: float = 0) -> int:
        return sum(
            1 for t, m in self.notifications if t >= since and m.get("method") == "textDocument/publishDiagnostics"
        )

    def stderr(self) -> str:
        if self.proc.stderr is None:
            return ""
        try:
            return self.proc.stderr.read().decode(errors="replace")[-2000:]
        except (OSError, ValueError):
            return ""

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.request("shutdown", {}, timeout=10)
                self.notify("exit", {})
                self.proc.wait(timeout=10)
            except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
                self.proc.kill()


def position(text: str, needle: str, offset: int = 0) -> dict[str, int]:
    for i, line in enumerate(text.split("\n")):
        j = line.find(needle)
        if j >= 0:
            return {"line": i, "character": j + offset}
    raise ValueError(needle)


def target_files(result: Any) -> list[str]:
    """Имена файлов из ответа definition: Location, Location[] или LocationLink[]."""
    if isinstance(result, dict):
        result = [result]
    return [Path((r.get("uri") or r.get("targetUri", "")).removeprefix("file://")).name for r in result or []]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "t")
    git(path, "config", "user.email", "t@t")
    return path


@pytest.fixture
def make_project(tmp_path: Path) -> Callable[..., Path]:
    """Проект с [tool.pyrefly]: make_project({"m.py": "..."}) → корень."""

    def build(files: dict[str, str], name: str = "proj") -> Path:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "pyproject.toml").write_text(
            '[project]\nname = "proj"\nversion = "0"\n\n[tool.pyrefly]\nproject-includes = ["."]\n'
        )
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        return root

    return build


@pytest.fixture
def client_factory() -> Iterator[Callable[..., LspClient]]:
    clients: list[LspClient] = []

    def start(root: Path, env: dict[str, str] | None = None, options: dict[str, Any] | None = None) -> LspClient:
        c = LspClient(root, env)
        clients.append(c)
        c.initialize(options)
        return c

    yield start
    for c in clients:
        c.close()

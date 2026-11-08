#!/usr/bin/env python3
"""Прослойка между Claude Code и `pyrefly lsp`.

Claude Code объявляет серверу, что не следит за файлами (`didChangeWatchedFiles` нет), шлёт
`didChange` только на собственные правки и не передаёт настроек. Отсюда то, что прослойка
добавляет к голому серверу:

- запуск pyrefly из `.venv` проекта (или основной копии для git worktree), иначе из PATH,
  с индексацией `lazy-blocking`: поиск ссылок полный с первого запроса;
- синхронизация с диском: сервер узнаёт о файлах, изменённых мимо Claude (git, форматтер,
  другой агент), а в открытые документы попадает текст с диска;
- worktree без своего `.venv`: интерпретатор основной копии (`pythonPath`);
- режим проверки для проектов без конфига pyrefly и отключение ошибок — из настроек плагина;
- полные исходящие вызовы: pyrefly теряет вызовы внутри `await` и выражений
  (facebook/pyrefly#5486), прослойка достраивает их по `ast` и переходу к определению.

Только стандартная библиотека, Python 3.9+. Протокол — в stdout, журнал — в stderr.

Блокировки: doc_lock упорядочивает всё, что меняет документы на сервере (open/change/close
и сверку с диском), и под ним можно писать серверу. Поток ответов сервера doc_lock не берёт
никогда — иначе запись в переполненный stdin сервера и непрочитанный stdout заперли бы друг друга.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Union

if TYPE_CHECKING:
    FunctionNode = Union[ast.FunctionDef, ast.AsyncFunctionDef]

TYPE_CHECKING_MODES = ("auto", "off", "basic", "legacy", "default", "strict", "all")
INDEXING_MODES = ("lazy-blocking", "lazy-non-blocking-background", "none")
WATCHED_SUFFIXES = (".py", ".pyi")
# Конфиги, которые pyrefly читает сам или переносит в режиме auto.
WATCHED_NAMES = ("pyproject.toml", "pyrefly.toml", "mypy.ini", ".mypy.ini", "setup.cfg", "pyrightconfig.json")
SKIP_DIRS = {"node_modules", "__pycache__", "build", "dist", "target", "site-packages"}
# Вне git обход дерева раз в секунду дорог: на большом дереве слежение за неоткрытыми файлами выключается.
MAX_WALK_ENTRIES = 20_000
MAX_GIT_FILES = 50_000
# Сверка с диском в фоне: сервер узнаёт о правке мимо Claude раньше, чем придёт запрос.
POLL_INTERVAL = 1.0
# Скан дороже этого на пути запроса не повторяем, если фон сканировал меньше POLL_INTERVAL назад.
CHEAP_SCAN = 0.05
# Зависимые файлы pyrefly перепроверяет в фоне и после этого заново публикует ошибки открытых
# файлов. Если правка найдена в момент запроса, ждём первую публикацию, затем тишину.
FIRST_PUBLICATION_TIMEOUT = 1.5
SETTLE_QUIET = 0.15
SETTLE_TIMEOUT = 3.0
OUTGOING_TIMEOUT = 60.0
DEFINITION_TIMEOUT = 20.0
INDEX_CACHE_SIZE = 64
FILE_CREATED, FILE_CHANGED, FILE_DELETED = 1, 2, 3
PROXY_ID_PREFIX = "pyrefly-lsp-proxy-"
KIND_CLASS, KIND_METHOD, KIND_FUNCTION = 5, 6, 12
# Вызов класса pyrefly отдаёт как вызов его __init__/__new__ (__call__ — вызов экземпляра, не класса).
CONSTRUCTORS = ("__init__", "__new__")
FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
DEF_NAME = re.compile(r"(?:async\s+def|def|class)\s+([^\W\d]\w*)")
LINE_BREAK = re.compile(r"\r\n|\r|\n")


def log(message: str) -> None:
    print(f"pyrefly-lsp: {message}", file=sys.stderr, flush=True)


# --- протокол -----------------------------------------------------------------------------


def encode(msg: Mapping[str, Any]) -> bytes:
    try:
        body = json.dumps(msg, ensure_ascii=False).encode()
    except UnicodeEncodeError:
        # Одиночный суррогат в строке: в UTF-8 его не записать, а как \\u-escape — можно.
        body = json.dumps(msg, ensure_ascii=True).encode()
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def read_message(stream: IO[bytes]) -> dict[str, Any] | None:
    length: int | None = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if length is None:
                continue
            break
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    body = stream.read(length)
    if len(body) < length:
        return None
    return json.loads(body)


def split_lines(text: str) -> list[str]:
    r"""Строки так, как их считают LSP и ast: разделители только \n, \r\n и \r."""
    return LINE_BREAK.split(text)


def _offset(text: str, line: int, character: int) -> int:
    """Индекс в строке Python для позиции LSP (строка, столбец в единицах UTF-16)."""
    starts = [0, *(m.end() for m in LINE_BREAK.finditer(text))]
    if line >= len(starts):
        return len(text)
    start = starts[line]
    brk = LINE_BREAK.search(text, start)
    content = text[start : brk.start() if brk else len(text)]
    units = idx = 0
    while idx < len(content) and units < character:
        units += 2 if ord(content[idx]) > 0xFFFF else 1
        idx += 1
    return start + idx


def apply_changes(text: str, changes: list[dict[str, Any]]) -> str:
    for change in changes:
        rng = change.get("range")
        if rng is None:
            text = change["text"]
            continue
        a = _offset(text, rng["start"]["line"], rng["start"]["character"])
        b = _offset(text, rng["end"]["line"], rng["end"]["character"])
        text = text[:a] + change["text"] + text[b:]
    return text


def _utf16_len(s: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in s)


def _utf16_col(line_text: str, byte_offset: int) -> int:
    """Столбец UTF-16 по смещению в байтах UTF-8 (так считает `ast`)."""
    return _utf16_len(line_text.encode()[:byte_offset].decode(errors="ignore"))


def uri_to_path(uri: str) -> Path | None:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file":
        return None
    return Path(urllib.request.url2pathname(parsed.path))


# --- настройки и окружение ----------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    type_checking_mode: str = "auto"
    diagnostics: bool = True
    sync_from_disk: bool = True
    complete_outgoing_calls: bool = True
    indexing_mode: str = "lazy-blocking"
    pyrefly: str | None = None
    # Предел файлов проекта, которые индексирует pyrefly (у самого pyrefly по умолчанию 2000).
    indexing_limit: int | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        def raw(name: str) -> str:
            value = (env.get(name) or "").strip()
            # Неподставленная ${user_config.X} — пользователь ничего не задал.
            return "" if value.startswith("${") else value

        def flag(name: str, default: bool) -> bool:
            value = raw(name).lower()
            if not value:
                return default
            if value in ("1", "true", "yes", "on"):
                return True
            if value in ("0", "false", "no", "off"):
                return False
            log(f"{name}={value!r} не понял, оставляю {default}")
            return default

        mode = raw("PYREFLY_LSP_TYPE_CHECKING_MODE") or "auto"
        if mode not in TYPE_CHECKING_MODES:
            log(f"режим проверки {mode!r} неизвестен, беру auto")
            mode = "auto"
        indexing = raw("PYREFLY_LSP_INDEXING_MODE") or "lazy-blocking"
        if indexing not in INDEXING_MODES:
            log(f"режим индексации {indexing!r} неизвестен, беру lazy-blocking")
            indexing = "lazy-blocking"
        pyrefly = raw("PYREFLY_LSP_PYREFLY")
        limit_text = raw("PYREFLY_LSP_INDEXING_LIMIT")
        try:
            limit = int(float(limit_text)) if limit_text else 0  # число из настроек может прийти как «10000.0»
        except ValueError:
            log(f"предел индексации {limit_text!r} не понял, оставляю предел pyrefly")
            limit = 0
        return cls(
            type_checking_mode=mode,
            diagnostics=flag("PYREFLY_LSP_DIAGNOSTICS", True),
            sync_from_disk=flag("PYREFLY_LSP_SYNC_FROM_DISK", True),
            complete_outgoing_calls=flag("PYREFLY_LSP_COMPLETE_OUTGOING_CALLS", True),
            indexing_mode=indexing,
            pyrefly=os.path.expanduser(pyrefly) if pyrefly else None,
            indexing_limit=limit if limit > 0 else None,
        )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_lines(root: Path, *args: str) -> list[str] | None:
    done = _git(root, *args)
    if done is None or done.returncode != 0:
        return None
    return done.stdout.decode(errors="replace").splitlines()


def main_checkout(root: Path) -> Path | None:
    """Основная копия репозитория, если root — дополнительный git worktree."""
    lines = _git_lines(root, "rev-parse", "--path-format=absolute", "--git-common-dir", "--git-dir")
    if not lines or len(lines) < 2:
        return None
    common, own = Path(lines[0]).resolve(), Path(lines[1]).resolve()
    if common == own or common.name != ".git":
        return None
    return common.parent


@dataclass(frozen=True)
class VenvSearch:
    """Где искать .venv: сам проект и корень его worktree, затем основная копия (тот же подкаталог, корень)."""

    own: tuple[Path, ...]
    main: tuple[Path, ...]


def venv_search(root: Path) -> VenvSearch:
    own = [root]
    rel: Path | None = None
    top = _git_lines(root, "rev-parse", "--show-toplevel")
    if top:
        top_path = Path(top[0])
        try:
            rel = root.resolve().relative_to(top_path.resolve())
        except ValueError:
            rel = None
        if rel is not None and rel.parts:
            own.append(top_path)
    main_paths: list[Path] = []
    main = main_checkout(root)
    if main is not None:
        if rel is not None and rel.parts:
            main_paths.append(main / rel)
        main_paths.append(main)
    return VenvSearch(tuple(own), tuple(main_paths))


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:  # нет прав зайти в каталог — считаем, что файла нет
        return False


def _venv_file(base: Path, *names: str) -> Path | None:
    for name in names:
        path = base / ".venv" / name
        if _exists(path):
            return path
    return None


def interpreter_for(root: Path, env: Mapping[str, str], search: VenvSearch | None = None) -> str | None:
    """pythonPath, если у самого проекта нет .venv: корень worktree, затем основная копия."""
    if env.get("VIRTUAL_ENV") or _venv_file(root, "bin/python", "Scripts/python.exe"):
        return None
    search = search or venv_search(root)
    for base in (*search.own[1:], *search.main):
        found = _venv_file(base, "bin/python", "Scripts/python.exe")
        if found:
            return str(found)
    return None


def pyrefly_command(
    root: Path,
    settings: Settings,
    which: Callable[[str], str | None] = shutil.which,
    search: VenvSearch | None = None,
) -> list[str] | None:
    binary = settings.pyrefly
    if not binary:
        search = search or venv_search(root)
        for base in (*search.own, *search.main):
            found = _venv_file(base, "bin/pyrefly", "Scripts/pyrefly.exe")
            if found:
                binary = str(found)
                break
    binary = binary or which("pyrefly")
    if not binary:
        return None
    command = [binary, "lsp", "--indexing-mode", settings.indexing_mode]
    if settings.indexing_limit:
        command += ["--workspace-indexing-limit", str(settings.indexing_limit)]
    return command


def init_options(client: Mapping[str, Any] | None, settings: Settings, python_path: str | None) -> dict[str, Any]:
    options = dict(client or {})
    if settings.type_checking_mode != "auto":
        pyrefly = dict(options.get("pyrefly") or {})
        pyrefly["typeCheckingMode"] = settings.type_checking_mode
        options["pyrefly"] = pyrefly
    if python_path:
        options["pythonPath"] = python_path
    return options


# --- слежение за диском -------------------------------------------------------------------


class _TooLarge(Exception):
    pass


class _GitFailed(Exception):
    pass


class DiskWatcher:
    """Снимок (mtime, размер) файлов Python и конфигов проекта; changes() — что изменилось с прошлого раза.

    В git список даёт `git ls-files` (миллисекунды). Вне git — обход каталогов без виртуальных
    окружений; если в дереве больше max_entries записей, слежение выключается целиком: усечённый
    обход давал бы ложные «удаления» и грузил бы диск каждую секунду. Сбой git не переводит в обход —
    тот видит игнорируемые файлы; снимок просто остаётся прежним до следующего удачного прохода.
    """

    def __init__(self, root: Path, max_entries: int = MAX_WALK_ENTRIES) -> None:
        self.root = root
        self.max_entries = max_entries
        inside = _git(root, "rev-parse", "--is-inside-work-tree")
        self.use_git = inside is not None and inside.returncode == 0 and inside.stdout.strip() == b"true"
        if self.use_git:
            # Проект лежит в игнорируемом каталоге другого репозитория (dotfiles в ~, .tmp/):
            # ls-files его не покажет — следим обходом каталогов.
            ignored = _git(root, "check-ignore", "-q", ".")
            self.use_git = not (ignored is not None and ignored.returncode == 0)
        self.enabled = True
        self.lock = threading.Lock()
        self.cost = 0.0
        self.last_scan = 0.0
        self.snapshot: dict[Path, tuple[int, int]] | None = None
        try:
            self.snapshot = self._timed_scan()
        except _TooLarge:
            self._switch_off()
        except _GitFailed:
            self.snapshot = None  # базу снимем при первом удачном проходе

    def _switch_off(self) -> None:
        self.enabled = False
        self.snapshot = {}
        if self.use_git:
            log(
                f"в {self.root} больше {MAX_GIT_FILES} файлов Python под git: слежение за неоткрытыми файлами выключено"
            )
        else:
            log(f"в {self.root} больше {self.max_entries} записей вне git: слежение за неоткрытыми файлами выключено")

    def _list(self) -> list[Path]:
        if not self.use_git:
            return self._walk()
        patterns = ["*.py", "*.pyi", *(f"*{name}" for name in WATCHED_NAMES)]
        done = _git(self.root, "ls-files", "-z", "-co", "--exclude-standard", "--", *patterns)
        if done is None or done.returncode != 0:
            raise _GitFailed
        names = [n for n in done.stdout.decode(errors="replace").split("\0") if n]
        if len(names) > MAX_GIT_FILES:
            raise _TooLarge
        return [self.root / n for n in names if n.endswith(WATCHED_SUFFIXES) or Path(n).name in WATCHED_NAMES]

    def _walk(self) -> list[Path]:
        found: list[Path] = []
        entries = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [
                d
                for d in dirnames
                if not d.startswith(".") and d not in SKIP_DIRS and not _exists(Path(dirpath, d) / "pyvenv.cfg")
            ]
            entries += len(dirnames) + len(filenames)
            if entries > self.max_entries:
                raise _TooLarge
            found.extend(Path(dirpath) / f for f in filenames if f.endswith(WATCHED_SUFFIXES) or f in WATCHED_NAMES)
        return found

    def _timed_scan(self) -> dict[Path, tuple[int, int]]:
        started = time.monotonic()
        snapshot: dict[Path, tuple[int, int]] = {}
        for path in self._list():
            try:
                st = path.stat()
            except OSError:
                continue
            snapshot[path] = (st.st_mtime_ns, st.st_size)
        self.last_scan = time.monotonic()
        self.cost = self.last_scan - started
        return snapshot

    def changes(self) -> list[tuple[Path, int]]:
        with self.lock:
            if not self.enabled:
                return []
            try:
                new = self._timed_scan()
            except _TooLarge:
                self._switch_off()
                return []
            except _GitFailed:
                return []
            old = self.snapshot
            self.snapshot = new
        if old is None:
            return []
        out = [(p, FILE_CREATED if p not in old else FILE_CHANGED) for p, sig in new.items() if old.get(p) != sig]
        out.extend((p, FILE_DELETED) for p in old if p not in new)
        return out


def unopened_changes(changes: Iterable[tuple[Path, int]], open_uris: Iterable[str]) -> list[dict[str, Any]]:
    """Изменения для didChangeWatchedFiles без открытых документов. Сравниваются пути: Node и
    Python по-разному кодируют в URI символы вроде «+» и «@»."""
    opened = {uri_to_path(uri) for uri in open_uris}
    return [{"uri": path.as_uri(), "type": kind} for path, kind in changes if path not in opened]


# --- исходящие вызовы ---------------------------------------------------------------------


@dataclass(frozen=True)
class CallSite:
    line: int
    character: int
    end_line: int
    end_character: int

    def as_range(self) -> dict[str, dict[str, int]]:
        return {
            "start": {"line": self.line, "character": self.character},
            "end": {"line": self.end_line, "character": self.end_character},
        }


def _first_line(node: FunctionNode | ast.ClassDef) -> int:
    return min([d.lineno for d in node.decorator_list] + [node.lineno]) - 1


def _function_at(tree: ast.AST, line: int) -> FunctionNode | None:
    for node in ast.walk(tree):
        if isinstance(node, FUNCTION_NODES) and _first_line(node) <= line <= node.lineno - 1:
            return node
    return None


def _calls(node: ast.AST) -> Iterator[ast.Call]:
    """Вызовы, которые выполняются вместе с телом: тела вложенных функций и лямбд не в счёт."""
    if isinstance(node, ast.Call):
        yield node
    if isinstance(node, (*FUNCTION_NODES, ast.Lambda)):
        children: list[ast.AST] = [*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]
        if not isinstance(node, ast.Lambda):
            children.extend(node.decorator_list)
    else:
        children = list(ast.iter_child_nodes(node))
    for child in children:
        yield from _calls(child)


@dataclass(frozen=True)
class Parsed:
    """Разобранный текст. BOM `ast` не принимает: разбираем без него, а столбцы первой строки
    сдвигаем на shift — в документе Claude Code BOM остаётся первым символом."""

    tree: ast.Module
    lines: list[str]
    shift: int

    def col(self, row: int, byte_offset: int) -> int:
        return _utf16_col(self.lines[row], byte_offset) + (self.shift if row == 0 else 0)


def parse_source(text: str) -> Parsed | None:
    shift = 1 if text.startswith("\ufeff") else 0
    body = text[shift:]
    try:
        tree = ast.parse(body)
    except (SyntaxError, ValueError):
        return None
    return Parsed(tree, split_lines(body), shift)


def call_sites(text: str, line: int, parsed: Parsed | None = None) -> list[CallSite]:
    """Места вызовов внутри функции, имя которой стоит на строке line (0-based)."""
    parsed = parsed or parse_source(text)
    if parsed is None:
        return []
    fn = _function_at(parsed.tree, line)
    if fn is None:
        return []
    sites: list[CallSite] = []
    for stmt in fn.body:
        for call in _calls(stmt):
            func = call.func
            if isinstance(func, ast.Name):
                row, start = func.lineno - 1, func.col_offset
            elif isinstance(func, ast.Attribute) and func.end_lineno is not None and func.end_col_offset is not None:
                row, start = func.end_lineno - 1, func.end_col_offset - len(func.attr.encode())
            else:
                continue
            name = func.id if isinstance(func, ast.Name) else func.attr
            col = parsed.col(row, start)
            sites.append(CallSite(row, col, row, col + _utf16_len(name)))
    return sites


def _span(parsed: Parsed, node: ast.AST, first: int) -> dict[str, dict[str, int]]:
    end_line = (getattr(node, "end_lineno", None) or first + 1) - 1
    end_offset = getattr(node, "end_col_offset", None) or 0
    end_col = parsed.col(end_line, end_offset) if end_line < len(parsed.lines) else 0
    return {"start": {"line": first, "character": 0}, "end": {"line": end_line, "character": end_col}}


def _name_range(parsed: Parsed, node: FunctionNode | ast.ClassDef) -> dict[str, dict[str, int]]:
    row = node.lineno - 1
    text = parsed.lines[row] if row < len(parsed.lines) else ""
    match = DEF_NAME.search(text)
    col = _utf16_len(text[: match.start(1)]) if match else 0
    col += parsed.shift if row == 0 else 0
    return {"start": {"line": row, "character": col}, "end": {"line": row, "character": col + _utf16_len(node.name)}}


class DefinitionIndex:
    """Определения функций и классов файла по позиции их имени: файл разбирается один раз."""

    def __init__(self, text: str) -> None:
        self.by_line: dict[int, list[tuple[int, int, dict[str, Any]]]] = {}
        parsed = parse_source(text)
        if parsed is None:
            return
        for parent in ast.walk(parsed.tree):
            for child in ast.iter_child_nodes(parent):
                if isinstance(child, (*FUNCTION_NODES, ast.ClassDef)):
                    self._add(parsed, parent, child)

    def _add(self, parsed: Parsed, parent: ast.AST, child: FunctionNode | ast.ClassDef) -> None:
        if isinstance(child, ast.ClassDef):
            kind = KIND_CLASS
        else:
            kind = KIND_METHOD if isinstance(parent, ast.ClassDef) else KIND_FUNCTION
        name = _name_range(parsed, child)
        item = {
            "name": child.name,
            "kind": kind,
            "range": _span(parsed, child, _first_line(child)),
            "selectionRange": name,
        }
        start, end = name["start"]["character"], name["end"]["character"]
        self.by_line.setdefault(name["start"]["line"], []).append((start, end, item))

    def describe(self, line: int, character: int) -> dict[str, Any] | None:
        """CallHierarchyItem (без uri), только если позиция стоит на имени def или class."""
        for start, end, item in self.by_line.get(line, []):
            if start <= character <= end:
                return item
        return None


def describe_target(text: str, line: int, character: int) -> dict[str, Any] | None:
    return DefinitionIndex(text).describe(line, character)


def _overlaps(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    def key(pos: Mapping[str, int]) -> tuple[int, int]:
        return pos["line"], pos["character"]

    return key(a["start"]) <= key(b["end"]) and key(b["start"]) <= key(a["end"])


def _start(rng: Mapping[str, Any]) -> tuple[int, int]:
    return rng["start"]["line"], rng["start"]["character"]


def _key(call: Mapping[str, Any]) -> tuple[str, int]:
    to = call["to"]
    return to["uri"], to["selectionRange"]["start"]["line"]


def merge_outgoing(server: list[dict[str, Any]] | None, ours: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ответ сервера плюс вызовы, которых он не нашёл.

    - Одна цель — одна запись (ключ: uri и строка имени цели).
    - Диапазон сервера может накрывать весь внешний вызов `helper(helper())`: ему соответствует
      самое раннее из наших мест внутри него, остальные — отдельные вызовы.
    - Вызов класса сервер отдаёт как __init__/__new__; наш вариант того же места — класс — отбрасываем,
      чтобы по элементу метода иерархию можно было обходить дальше.
    """
    server = server or []
    constructor_ranges = [r for c in server if c["to"].get("name") in CONSTRUCTORS for r in c.get("fromRanges", [])]
    class_sites = [
        (i, j, rng)
        for i, call in enumerate(ours)
        if call["to"].get("kind") == KIND_CLASS
        for j, rng in enumerate(call.get("fromRanges", []))
    ]
    dropped: set[tuple[int, int]] = set()
    for known in constructor_ranges:  # Outer(Inner()): конструктору Outer соответствует только Outer
        candidates = [(i, j, r) for i, j, r in class_sites if (i, j) not in dropped and _overlaps(r, known)]
        if candidates:
            i, j, _r = min(candidates, key=lambda c: _start(c[2]))
            dropped.add((i, j))
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    for call in server:
        entry = merged.setdefault(_key(call), {"to": call["to"], "fromRanges": []})
        entry["fromRanges"].extend(r for r in call.get("fromRanges", []) if r not in entry["fromRanges"])
    ours_by_key: dict[tuple[str, int], tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for i, call in enumerate(ours):
        ranges = [r for j, r in enumerate(call.get("fromRanges", [])) if (i, j) not in dropped]
        if ranges:
            ours_by_key.setdefault(_key(call), (call["to"], []))[1].extend(ranges)
    for key, (to, ranges) in ours_by_key.items():
        server_ranges = merged[key]["fromRanges"] if key in merged else []
        matched: set[int] = set()
        for known in server_ranges:
            candidates = [i for i, r in enumerate(ranges) if i not in matched and _overlaps(r, known)]
            if candidates:
                matched.add(min(candidates, key=lambda i: _start(ranges[i])))
        extra: list[dict[str, Any]] = []
        for i, rng in enumerate(ranges):
            if i not in matched and all(_start(rng) != _start(r) for r in extra):
                extra.append(rng)
        if extra:
            merged.setdefault(key, {"to": to, "fromRanges": []})["fromRanges"].extend(extra)
    return list(merged.values())


# --- прослойка ----------------------------------------------------------------------------


@dataclass
class Doc:
    text: str
    version: int
    disk: tuple[int, int] | None = None
    on_server: bool = True
    # Текст подставлен с диска: инкрементная правка клиента считалась от его старого буфера.
    resynced: bool = False
    # Номер открытия: после didClose и нового didOpen версия снова начинается с 1.
    generation: int = 0


@dataclass
class Waiter:
    event: threading.Event = field(default_factory=threading.Event)
    msg: dict[str, Any] | None = None


def _disk_signature(path: Path | None) -> tuple[int, int] | None:
    if path is None:
        return None
    try:
        st = path.stat()
    except (OSError, ValueError):  # ValueError — нулевой байт в пути
        return None
    return st.st_mtime_ns, st.st_size


def _read_text(path: Path | None, lenient: bool = False) -> str | None:
    """Текст файла. Строго UTF-8 — для сверки документов (иначе None, попробуем позже);
    lenient — с заменой битых байтов, для разбора определений."""
    if path is None:
        return None
    try:
        return path.read_bytes().decode("utf-8", errors="replace" if lenient else "strict")
    except (OSError, ValueError):
        return None


def _root_from(params: Mapping[str, Any], env: Mapping[str, str]) -> Path:
    for uri in (params.get("rootUri"), *(f.get("uri") for f in params.get("workspaceFolders") or [])):
        path = uri_to_path(uri) if uri else None
        if path:
            return path
    return Path(params.get("rootPath") or env.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()


class Proxy:
    def __init__(self, settings: Settings, env: Mapping[str, str], client_in: IO[bytes], client_out: IO[bytes]) -> None:
        self.settings = settings
        self.env = env
        self.client_in = client_in
        self.client_out = client_out
        self.out_lock = threading.Lock()
        self.server_lock = threading.Lock()
        self.doc_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.index_lock = threading.Lock()
        self.published = threading.Condition()
        self.publications = 0
        self.server: subprocess.Popen[bytes] | None = None
        self.watcher: DiskWatcher | None = None
        self.docs: dict[str, Doc] = {}
        self.pending: dict[str, Waiter] = {}
        self.indexes: OrderedDict[tuple[str, Any], DefinitionIndex] = OrderedDict()
        self.unparsable: set[str] = set()
        self.counter = 0
        self.generations = 0
        self.exit_code: int | None = None
        self.stopped = threading.Event()
        # Сверки (фон и запрос) идут по одной: скан и отправка его итогов — одно целое.
        self.sync_lock = threading.Lock()
        # Фон отправил изменения, а их перепроверку ещё никто не дождался: ждёт ближайший запрос.
        self.unsettled: int | None = None

    # ввод-вывод
    def to_client(self, msg: Mapping[str, Any]) -> None:
        with self.out_lock:
            self.client_out.write(encode(msg))
            self.client_out.flush()

    def to_server(self, msg: Mapping[str, Any]) -> None:
        server = self.server
        if server is None or server.stdin is None:
            return
        data = encode(msg)
        with self.server_lock:
            try:
                server.stdin.write(data)
                server.stdin.flush()
            except (OSError, ValueError):  # сервер завершился или канал закрыт
                pass

    def notify_server(self, method: str, params: Mapping[str, Any]) -> None:
        self.to_server({"jsonrpc": "2.0", "method": method, "params": params})

    def ask_server(self, method: str, params: Mapping[str, Any]) -> tuple[str, Waiter]:
        with self.pending_lock:
            self.counter += 1
            rid = f"{PROXY_ID_PREFIX}{self.counter}"
            waiter = Waiter()
            self.pending[rid] = waiter
        self.to_server({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return rid, waiter

    def wait_reply(self, rid: str, waiter: Waiter, timeout: float) -> dict[str, Any] | None:
        if waiter.event.wait(max(0.0, timeout)):
            return waiter.msg
        with self.pending_lock:
            self.pending.pop(rid, None)
        return None

    # главный цикл
    def run(self) -> int:
        while self.exit_code is None:
            msg = read_message(self.client_in)
            if msg is None:
                break
            self.from_client(msg)
        self.stopped.set()
        if self.exit_code is not None:
            return self.exit_code
        self.stop_server()
        return 0

    def stop_server(self) -> None:
        server = self.server
        if server is None or server.poll() is not None:
            return
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()

    def from_client(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method == "initialize" and self.server is None:
            self.start(msg)
            return
        if self.server is None:
            if "id" in msg and method:
                error = {"code": -32002, "message": "сервер ещё не инициализирован"}
                self.to_client({"jsonrpc": "2.0", "id": msg["id"], "error": error})
            return
        if method == "textDocument/didOpen":
            self.did_open(msg)
        elif method == "textDocument/didChange":
            self.did_change(msg)
        elif method == "textDocument/didClose":
            self.did_close(msg)
        elif method and "id" in msg:
            if method != "shutdown":
                self.sync(from_request=True)
                with self.published:
                    seen, self.unsettled = self.unsettled, None
                if seen is not None:
                    self.settle(seen)
            if method == "callHierarchy/outgoingCalls" and self.settings.complete_outgoing_calls:
                threading.Thread(target=self.outgoing_calls, args=(msg,), daemon=True).start()
                return
            self.to_server(msg)
        else:
            self.to_server(msg)

    def start(self, msg: dict[str, Any]) -> None:
        params = dict(msg.get("params") or {})
        root = _root_from(params, self.env)
        search = venv_search(root)
        command = pyrefly_command(root, self.settings, search=search)
        problem = "pyrefly не найден: поставьте его (uv tool install pyrefly) или задайте путь в настройках плагина"
        if command is not None:
            try:
                self.server = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            except OSError as exc:
                problem = f"не удалось запустить pyrefly ({command[0]}): {exc}"
        if self.server is None or command is None:
            log(problem)
            self.to_client({"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": problem}})
            self.exit_code = 1
            return
        python_path = interpreter_for(root, self.env, search=search)
        if python_path:
            log(f"у проекта нет своего .venv, беру интерпретатор {python_path}")
        params["initializationOptions"] = init_options(params.get("initializationOptions"), self.settings, python_path)
        threading.Thread(target=self.from_server, daemon=True).start()
        self.to_server({**msg, "params": params})
        if self.settings.sync_from_disk:
            # Первый снимок вне git может занять секунды — строим его в фоне, не задерживая initialize.
            threading.Thread(target=self.poll_disk, args=(root,), daemon=True).start()
        log(f"запущен {' '.join(command)} в {root}")

    # документы
    def _disk_matches(self, path: Path | None, text: str) -> tuple[int, int] | None:
        """Подпись файла, если на диске ровно этот текст; иначе None — следующая сверка подставит диск."""
        if not self.settings.sync_from_disk:
            return None  # сверка выключена — диск не читаем
        sig = _disk_signature(path)
        return sig if sig is not None and _read_text(path) == text else None

    def did_open(self, msg: dict[str, Any]) -> None:
        doc = msg["params"]["textDocument"]
        text = doc.get("text", "")
        with self.doc_lock:
            self.generations += 1
            disk = self._disk_matches(uri_to_path(doc["uri"]), text)
            self.docs[doc["uri"]] = Doc(text, int(doc.get("version", 1)), disk, generation=self.generations)
            self.to_server(msg)

    def did_change(self, msg: dict[str, Any]) -> None:
        params = msg["params"]
        uri = params["textDocument"]["uri"]
        changes = params.get("contentChanges", [])
        with self.doc_lock:
            doc = self.docs.get(uri)
            if doc is None:
                self.to_server(msg)
                return
            path = uri_to_path(uri)
            incremental = any(c.get("range") is not None for c in changes)
            if doc.resynced and incremental:
                # Claude Code сначала пишет файл, потом шлёт правку от своего старого буфера, а у нас
                # уже текст с диска: правку поверх него не применить — берём итог с диска.
                disk_text = _read_text(path)
                if disk_text is None:
                    doc.disk = None  # прочитать нельзя: серверу ничего, следующая сверка подставит диск
                    return
                doc.text = disk_text
                changes = [{"text": disk_text}]
            else:
                doc.text = apply_changes(doc.text, changes)
            doc.resynced = False
            doc.version += 1
            doc.disk = self._disk_matches(path, doc.text)
            if not doc.on_server:
                doc.on_server = True
                item = {"uri": uri, "languageId": "python", "version": doc.version, "text": doc.text}
                self.notify_server("textDocument/didOpen", {"textDocument": item})
                return
            document = {**params["textDocument"], "version": doc.version}
            self.to_server({**msg, "params": {**params, "textDocument": document, "contentChanges": changes}})

    def did_close(self, msg: dict[str, Any]) -> None:
        with self.doc_lock:
            doc = self.docs.pop(msg["params"]["textDocument"]["uri"], None)
            if doc is None or doc.on_server:
                self.to_server(msg)

    # сверка с диском
    def poll_disk(self, root: Path) -> None:
        try:
            self.watcher = DiskWatcher(root)
        except Exception as exc:  # noqa: BLE001 — без слежения за неоткрытыми файлами открытые всё равно сверяем
            log(f"слежение за файлами не поднялось: {exc!r}")
        while not self.stopped.wait(max(POLL_INTERVAL, 10 * (self.watcher.cost if self.watcher else 0.0))):
            self.sync()

    def sync(self, from_request: bool = False) -> int | None:
        """Сверить с диском. Если серверу что-то отправлено — число публикаций до отправки, иначе None.

        Скан файлов идёт вне doc_lock. На пути запроса дорогой скан не повторяем, если фон только что
        его сделал: правку, найденную фоном, сервер уже получил.
        """
        if not self.settings.sync_from_disk:
            return None
        with self.sync_lock:
            try:
                return self._sync(from_request)
            except Exception as exc:  # noqa: BLE001 — сверка не должна ронять прослойку ни в фоне, ни на запросе
                log(f"сверка с диском не удалась: {exc!r}")
                return None

    def _sync(self, from_request: bool) -> int | None:
        with self.published:
            seen = self.publications
        changes: list[tuple[Path, int]] = []
        watcher = self.watcher
        if watcher is not None:
            fresh = time.monotonic() - watcher.last_scan < POLL_INTERVAL
            if not (from_request and watcher.cost > CHEAP_SCAN and fresh):
                changes = watcher.changes()
        with self.doc_lock:
            sent = self._sync_documents()
            open_on_server = [uri for uri, doc in self.docs.items() if doc.on_server]
            watched = unopened_changes(changes, open_on_server)
            if watched:
                sent = True
                self.notify_server("workspace/didChangeWatchedFiles", {"changes": watched})
        if not sent:
            return None
        with self.published:
            self.unsettled = seen if self.unsettled is None else min(self.unsettled, seen)
        return seen

    def _sync_documents(self) -> bool:
        """Открытые документы — к тексту на диске. Удалённый закрываем на сервере: тогда событие
        об удалении его файла дойдёт до сервера вместе с остальными (см. unopened_changes)."""
        sent = False
        for uri, doc in self.docs.items():
            sent = self._sync_document_safely(uri, doc) or sent
        return sent

    def _sync_document_safely(self, uri: str, doc: Doc) -> bool:
        try:
            return self._sync_document(uri, doc)
        except Exception as exc:  # noqa: BLE001 — один странный документ не должен сорвать сверку остальных
            log(f"не сверил {uri}: {exc!r}")
            return False

    def _sync_document(self, uri: str, doc: Doc) -> bool:
        """Один открытый документ — к тексту на диске. True, если серверу что-то отправлено."""
        path = uri_to_path(uri)
        sig = _disk_signature(path)
        if sig is not None and sig == doc.disk and doc.on_server:
            return False
        if sig is None:
            doc.disk = None
            if not doc.on_server:
                return False
            doc.on_server = False
            self.notify_server("textDocument/didClose", {"textDocument": {"uri": uri}})
            return True
        text = _read_text(path)
        if text is None:
            return False  # прочитать не вышло — подпись не запоминаем, попробуем на следующем проходе
        doc.disk = sig
        if doc.on_server and text == doc.text:
            return False
        doc.text = text
        doc.version += 1
        doc.resynced = True
        if doc.on_server:
            change = {"textDocument": {"uri": uri, "version": doc.version}, "contentChanges": [{"text": text}]}
            self.notify_server("textDocument/didChange", change)
        else:
            doc.on_server = True
            item = {"uri": uri, "languageId": "python", "version": doc.version, "text": text}
            self.notify_server("textDocument/didOpen", {"textDocument": item})
        return True

    def settle(self, seen: int) -> None:
        """Дождаться, пока сервер перепроверит изменения: первая публикация ошибок после seen, затем тишина."""
        with self.doc_lock:
            if not any(doc.on_server for doc in self.docs.values()):
                return  # публиковать ошибки серверу не для чего — ждать нечего
        deadline = time.monotonic() + SETTLE_TIMEOUT
        with self.published:
            if not self.published.wait_for(lambda: self.publications > seen, timeout=FIRST_PUBLICATION_TIMEOUT):
                return
            while time.monotonic() < deadline:
                last = self.publications
                if not self.published.wait_for(lambda last=last: self.publications > last, timeout=SETTLE_QUIET):
                    return

    # ответы сервера
    def from_server(self) -> None:
        server = self.server
        assert server is not None
        assert server.stdout is not None
        try:
            while True:
                msg = read_message(server.stdout)
                if msg is None:
                    break
                self._handle_server_message(msg)
        except Exception as exc:  # noqa: BLE001 — без этого поток молча умрёт, а Claude будет ждать ответов вечно
            log(f"поток ответов pyrefly упал: {exc!r}")
            if not self.stopped.is_set():
                self.stop_server()
                os._exit(1)
        code = server.wait()
        with self.out_lock:
            self.client_out.flush()
        if self.stopped.is_set():
            os._exit(0)  # сервер остановили мы: сессия закончилась штатно
        if code:
            log(f"pyrefly завершился с кодом {code}")
        os._exit(code if code and code > 0 else (1 if code else 0))

    def _handle_server_message(self, msg: dict[str, Any]) -> None:
        rid = msg.get("id")
        if "method" not in msg and isinstance(rid, str) and rid.startswith(PROXY_ID_PREFIX):
            with self.pending_lock:
                waiter = self.pending.pop(rid, None)
            if waiter is not None:
                waiter.msg = msg
                waiter.event.set()
            return
        if msg.get("method") == "textDocument/publishDiagnostics":
            with self.published:
                self.publications += 1
                self.published.notify_all()
            if not self.settings.diagnostics:
                return
        self.to_client(msg)

    # исходящие вызовы
    def outgoing_calls(self, msg: dict[str, Any]) -> None:
        rid, waiter = self.ask_server(msg["method"], msg.get("params") or {})
        reply = self.wait_reply(rid, waiter, OUTGOING_TIMEOUT)
        if reply is None:
            reply = {"error": {"code": -32603, "message": "pyrefly не ответил на outgoingCalls"}}
        if "error" in reply:
            self.to_client({"jsonrpc": "2.0", "id": msg["id"], "error": reply["error"]})
            return
        result = reply.get("result")
        try:
            ours = self.compute_outgoing((msg.get("params") or {}).get("item") or {})
            result = merge_outgoing(result if isinstance(result, list) else [], ours)
        except Exception as exc:  # noqa: BLE001 — достройка необязательна: при сбое отдаём ответ сервера как есть
            log(f"не смог достроить исходящие вызовы: {exc!r}")
        self.to_client({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    def _version(self, uri: str) -> tuple[Any, str | None]:
        """Версия текста без чтения файла: документ — по номеру открытия и версии, файл — по подписи.
        Второе значение — текст открытого документа (у файла читаем только при промахе кэша)."""
        with self.doc_lock:
            doc = self.docs.get(uri)
            if doc is not None:
                return ("document", doc.generation, doc.version), doc.text
        return ("disk", _disk_signature(uri_to_path(uri))), None

    def text_for(self, uri: str) -> str | None:
        _version, text = self._version(uri)
        return text if text is not None else _read_text(uri_to_path(uri), lenient=True)

    def definition_index(self, uri: str) -> DefinitionIndex:
        version, text = self._version(uri)
        key = (uri, version)
        with self.index_lock:
            index = self.indexes.get(key)
            if index is not None:
                self.indexes.move_to_end(key)
                return index
        if text is None:
            text = _read_text(uri_to_path(uri), lenient=True)
        index = DefinitionIndex(text or "")
        with self.index_lock:
            self.indexes[key] = index
            while len(self.indexes) > INDEX_CACHE_SIZE:
                self.indexes.popitem(last=False)
        return index

    def describe(self, uri: str, line: int, character: int) -> dict[str, Any] | None:
        return self.definition_index(uri).describe(line, character)

    def compute_outgoing(self, item: Mapping[str, Any]) -> list[dict[str, Any]]:
        uri = item.get("uri")
        rng = item.get("selectionRange") or item.get("range")
        if not uri or not rng:
            return []
        text = self.text_for(uri)
        if text is None:
            return []
        parsed = parse_source(text)
        if parsed is None:
            if uri not in self.unparsable:
                self.unparsable.add(uri)
                try:
                    ast.parse(text.lstrip("\ufeff"))
                    reason = "не разбирается"
                except (SyntaxError, ValueError) as exc:
                    reason = f"{getattr(exc, 'msg', exc)}, строка {getattr(exc, 'lineno', '?')}"
                log(
                    f"не разобрал {uri} на Python {sys.version.split()[0]} ({reason}): исходящие вызовы для него — "
                    "только ответ pyrefly. Если это новый синтаксис, прослойке нужен python3 не старше проекта"
                )
            return []
        asked = []
        for site in call_sites(text, rng["start"]["line"], parsed):
            position = {"line": site.line, "character": site.character}
            params = {"textDocument": {"uri": uri}, "position": position}
            asked.append((site, *self.ask_server("textDocument/definition", params)))
        deadline = time.monotonic() + DEFINITION_TIMEOUT
        calls: list[dict[str, Any]] = []
        missed = 0
        for site, rid, waiter in asked:
            reply = self.wait_reply(rid, waiter, deadline - time.monotonic())
            if reply is None:
                missed += 1
                continue
            result = reply.get("result")
            targets = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
            for target in targets:  # у x: A | B определений несколько — каждое своя цель
                target_uri = target.get("targetUri") or target.get("uri")
                target_range = target.get("targetSelectionRange") or target.get("range")
                if not target_uri or not target_range:
                    continue
                start = target_range["start"]
                described = self.describe(target_uri, start["line"], start["character"])
                if described is None:
                    continue  # параметр, переменная, импорт — не определение функции или класса
                calls.append({"to": {"uri": target_uri, **described}, "fromRanges": [site.as_range()]})
        if missed:
            log(f"pyrefly не успел найти определения для {missed} вызовов из {len(asked)}")
        return calls


def main() -> int:
    if sys.version_info < (3, 9):  # noqa: UP036 — понятное сообщение вместо падения на старом python3
        log(f"нужен Python 3.9 или новее, запущен {sys.version.split()[0]} ({sys.executable})")
        return 1
    settings = Settings.from_env(os.environ)
    return Proxy(settings, os.environ, sys.stdin.buffer, sys.stdout.buffer).run()


if __name__ == "__main__":
    sys.exit(main())

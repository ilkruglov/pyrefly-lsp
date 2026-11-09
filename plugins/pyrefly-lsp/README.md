# pyrefly-lsp

A Claude Code plugin that runs [pyrefly](https://pyrefly.org) as the Python language server. Claude gets code
navigation and type errors from the same engine as `pyrefly check`.

This is a community plugin. It is not affiliated with or endorsed by Meta or the pyrefly team.

The LSP client in Claude Code does not watch files, tells the server only about its own edits and passes no
settings. So after something edits files outside Claude (git, a formatter, another agent), a server connected
directly keeps working with stale code for the rest of the session. This plugin puts a small proxy, a Python
script with no dependencies, between Claude Code and `pyrefly lsp`. The proxy keeps the server in sync with the
disk and fills in what pyrefly returns incompletely.

## Comparison

What Claude gets from different Python language servers under the same conditions, the way Claude Code works
with a server. Ready-made plugins exist for pyright (the official `pyright-lsp`) and ty (`astral` by Astral);
the other servers would need a plugin of your own.

| | pyrefly-lsp | pyright-lsp | astral (ty) | basedpyright | zuban | jedi, pylsp |
|---|---|---|---|---|---|---|
| Type errors | pyrefly, same as `pyrefly check` | pyright | ty (beta) | basedpyright | zuban | no type checking |
| First find-references after opening a file¹ | 718 in 57 files | 14 in 1 file | 718 in 57 files | 14 in 1 file | 490 in 57 files | 240 in 22 files |
| Outgoing calls, including `await f()` and `f() + g()` | complete | complete | complete | complete | no | no |
| Implementations: via inheritance / of a Protocol method | yes / no | no / no | yes / no | yes / yes | no / no | no / no |
| Libraries from the project's `.venv` without configuration | yes | no | yes | yes | yes | no |
| Function added outside Claude to an unopened file | seen | not seen, false error | not seen, false error | not seen, false error | seen | seen |
| Open file changed outside Claude | new text | old text | old text | old text | old text | old text |
| git worktree without its own `.venv` | libraries from the main checkout's `.venv` | no libraries | no libraries | no libraries | no libraries | no libraries |

¹ pyright and basedpyright find all 719 references once their index is built: a repeated request 15 s later is
complete. For the others a repeated request returns the same number.

What follows from this:

- ty navigates almost as well, but it does not see edits made outside Claude or worktrees without a `.venv`,
  and it reports type errors by its own rules, not by those of `pyrefly check`.
- Only basedpyright finds implementations of Protocol methods.
- No server re-reads an open file changed outside Claude by itself: by the protocol, a server trusts the
  client's text for an open file. Only the proxy covers this.

How it was measured (November 9, 2026). A test client sends the server the same messages as Claude Code
2.1.319: its client capabilities were taken from a Claude Code log, with no file watching and no settings; an
edit is written to the file, and the server gets `didChange` with the full text. References: a class in a
project of 505 Python files. Calls, implementations and edits outside Claude: a three-file project. Libraries:
go to `FastAPI` in a project with a `.venv` and to `torch` in a git worktree without its own `.venv`. Versions:
pyrefly 1.4.1, pyright 1.1.414, ty 0.0.99, basedpyright 1.40.2, zuban 0.10.2, jedi-language-server 0.47.0,
python-lsp-server 1.15.0.

## How the proxy works

- **Indexing**: `pyrefly lsp --indexing-mode lazy-blocking`. The first request waits for the index, so the
  answer is complete.
- **Outgoing calls**: pyrefly loses calls inside `await` and expressions
  ([facebook/pyrefly#5486](https://github.com/facebook/pyrefly/issues/5486)). The proxy parses the function
  body with `ast`, asks pyrefly for the definition of every call and merges the result with the server's
  reply.
- **Disk sync**: in the background once a second (less often if walking the tree is slow: at most once per
  10 walk durations) and before every request from Claude. Changed unopened files go to the server as
  `workspace/didChangeWatchedFiles`; open documents get the text from disk. The proxy watches `.py`, `.pyi`
  and the configs pyrefly reads: `pyproject.toml`, `pyrefly.toml`, `mypy.ini`, `.mypy.ini`, `setup.cfg`,
  `pyrightconfig.json`. The file list comes from `git ls-files` (about 5 ms for 600 files); outside git, from
  a directory walk that skips virtual environments.
- **Worktrees**: if the project has no `.venv` of its own and no virtual environment is active
  (`VIRTUAL_ENV`), the server gets `pythonPath` from the worktree root (when the project is its
  subdirectory), then from the main checkout: the same subdirectory (monorepo), then the root. The proxy looks
  for the `pyrefly` binary in the same places, starting with the project's own `.venv`.
- **Large trees**: the proxy watches unopened files only if `git ls-files` lists at most 50,000 `.py`, `.pyi`
  and config files (tracked ones plus untracked ones that are not ignored) or, outside git, the tree has at
  most 20,000 entries. On a larger tree only open documents are synced, and the log says so.

## What the plugin runs and reads

- **Runs** the proxy: `python3 -I <plugin folder>/scripts/pyrefly_lsp_proxy.py`. Its source is in this folder;
  it uses only the Python standard library.
- **Starts** `pyrefly lsp`: from the "Path to pyrefly" setting, else from the project's `.venv` (for a
  worktree, the main checkout's), else from PATH. The plugin does not install pyrefly.
- **Runs git**, read-only, in the project directory: `git rev-parse` (is the project in a work tree, and where
  is the main checkout of a worktree), `git check-ignore` and `git ls-files` (which files to watch).
- **Reads** sizes and modification times of the watched files; the text of open documents and of any file
  that pyrefly's definitions point to (project files, libraries, standard-library stubs); whether a `.venv`
  has `bin/python` and `bin/pyrefly` (`Scripts/python.exe` and `Scripts/pyrefly.exe` on Windows).
- **Reads environment variables**: the `PYREFLY_LSP_*` values that Claude Code fills in from the plugin
  settings, `VIRTUAL_ENV` and `CLAUDE_PROJECT_DIR`.
- **Does not** write files or make network requests. The proxy logs to the server's stderr
  (`claude --debug`).

## Requirements

- Claude Code 2.1.271 or later (choice lists in plugin settings).
- `python3` 3.9+ on PATH: it runs the proxy. On an older one the proxy says so in the log.
- `pyrefly` in the project's `.venv` (for example, as a dev dependency) or on PATH:
  `uv tool install pyrefly`. Tested with 1.4.1.

## Install

```bash
claude plugin marketplace add ilkruglov/pyrefly-lsp
claude plugin install pyrefly-lsp@pyrefly-lsp
```

To enable the server only in some projects, disable it globally and enable it per project:

```bash
claude plugin disable pyrefly-lsp@pyrefly-lsp -s user
cd ~/my-project && claude plugin enable pyrefly-lsp@pyrefly-lsp -s local
```

Disable `pyright-lsp` in projects that use this plugin: only one server handles the `.py` extension, the one
registered first.

### Update

```bash
claude plugin marketplace update pyrefly-lsp
claude plugin update pyrefly-lsp@pyrefly-lsp
```

The new version loads in the next Claude Code session. What changed: see the
[changelog](https://github.com/ilkruglov/pyrefly-lsp/blob/main/CHANGELOG.md).

## Settings

`/config` or `/plugin configure pyrefly-lsp@pyrefly-lsp`:

| Setting | Default | What it does |
|---|---|---|
| Type-checking mode without a pyrefly config | `auto` | For files without `pyrefly.toml` or `[tool.pyrefly]`. `auto` migrates mypy/pyright settings, otherwise uses the `basic` preset; `default`, `strict` and `all` are stricter; `off` reports no errors. The project config always wins |
| Type errors after edits | on | Turn off if a linter hook already reports them: navigation keeps working |
| Sync with disk | on | Turn off only for troubleshooting |
| Complete outgoing calls | on | Turn off only for troubleshooting |
| Path to pyrefly | empty | By default, the project's `.venv` (for a worktree, the main checkout's), then PATH |
| Indexing limit | 0 | How many project files pyrefly indexes; 0 keeps its default (2000). Higher gives more complete find-references on large projects but uses more memory: about 0.5 GB for 600 files |

## Project configuration

pyrefly finds the project's `.venv` itself. Import roots, the preset and exclusions go into `[tool.pyrefly]` in
`pyproject.toml` (or into `pyrefly.toml`):

```toml
[tool.pyrefly]
project-includes = ["src", "tests"]
search-path = ["src"]
python-version = "3.12"
preset = "default"
```

## Limitations

- Implementations of a Protocol method are not found: pyrefly finds implementations through inheritance only.
- pyrefly indexes at most 2000 project files (the "Indexing limit" setting): on a larger project,
  find-references in the remaining files is incomplete.
- Claude Code attaches type errors to the next tool call, not to the edit itself.
- pyrefly rechecks dependent files in the background. If an edit made outside Claude is found right before a
  request, the proxy waits until the server stops publishing errors (up to 3 s).
- Claude Code never closes documents, so the proxy syncs them with the disk for the whole session.
- For open documents the disk is the source of truth: `didChange` text that is not on disk is replaced by the
  disk text at the next sync. Claude Code edits the files themselves, so this does not affect it; a client
  with unsaved edits, such as an editor, would not work this way.
- Outgoing-call completion parses code with `ast` of the `python3` that runs the proxy. Syntax newer than its
  version (for example, `def f[T]` with python3 3.11) is not parsed: for such a file only pyrefly's answer is
  returned, and the log says so.
- Tested on Linux with Python 3.12. Python 3.9 and Windows were not tested by running.

## License

Apache-2.0, see [LICENSE](https://github.com/ilkruglov/pyrefly-lsp/blob/main/LICENSE).

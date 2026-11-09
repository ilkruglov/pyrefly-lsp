# Changelog

## 1.0.1 — 2026-11-09

- Documentation, plugin settings, log messages and code comments are in English.
- The full README now lives in the plugin folder, next to the manifest; the repository README is a short
  overview.
- The README lists what the plugin runs and reads, and states the watched configs, the worktree conditions
  and the file limit for watching exactly as the code does.

## 1.0.0 — 2026-11-09

A proxy, `scripts/pyrefly_lsp_proxy.py` (Python, no dependencies), now sits between Claude Code and
`pyrefly lsp`. What changed for Claude compared with 0.1.0, measured under Claude Code 2.1.319 conditions with
pyrefly 1.4.1 (the plugin README says how):

| | 0.1.0 | 1.0.0 |
|---|---|---|
| First find-references after opening a file | 14 in 1 file | 718 in 57 files |
| Outgoing calls `await f()` and `f() + g()` | lost | complete |
| Function added outside Claude to an unopened file | not seen, false error | seen |
| Open file changed outside Claude | old text | new text |
| git worktree without its own `.venv` | no libraries | libraries from the main checkout's `.venv` |

- Find-references is complete from the first request: `lazy-blocking` indexing.
- Outgoing calls are complete: the proxy adds the calls pyrefly loses inside `await` and expressions
  (facebook/pyrefly#5486).
- Disk sync: files changed outside Claude are visible to the server from the next request; open documents
  get the text from disk. Configs that pyrefly migrates in auto mode are watched too: `mypy.ini`,
  `.mypy.ini`, `setup.cfg`, `pyrightconfig.json`.
- A git worktree without its own `.venv` uses the main checkout's interpreter and `pyrefly`.
- `pyrefly` is looked up in the project's `.venv`, then on PATH; without it the proxy returns a clear error
  instead of crashing.
- Plugin settings: type-checking mode for projects without a config, turning errors off, path to pyrefly,
  indexing limit, switches for disk sync and outgoing-call completion.
- The proxy runs as `python3 -I`: project files and `PYTHONPATH` cannot shadow its modules.
- `workspaceFolder` and `startupTimeout` in the server config.
- Tests: unit tests for the proxy and end-to-end tests with real pyrefly.

## 0.1.0 — 2026-11-08

- First version: `pyrefly lsp` directly, modeled on the official `pyright-lsp`.

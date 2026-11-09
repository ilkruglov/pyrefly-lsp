# pyrefly-lsp

A Claude Code plugin that runs [pyrefly](https://pyrefly.org) as the Python language server, with a proxy that
keeps the server in sync with the disk and completes outgoing calls. Claude gets code navigation and type
errors from the same engine as `pyrefly check`.

Documentation, a comparison with other Python language servers, settings and limitations are in the plugin
README: [plugins/pyrefly-lsp/README.md](plugins/pyrefly-lsp/README.md). Changes: [CHANGELOG.md](CHANGELOG.md).

This is a community plugin. It is not affiliated with or endorsed by Meta or the pyrefly team.

## Install

```bash
claude plugin marketplace add ilkruglov/pyrefly-lsp
claude plugin install pyrefly-lsp@pyrefly-lsp
```

You also need `python3` 3.9+ and `pyrefly` (`uv tool install pyrefly`).

## Repository layout

- `plugins/pyrefly-lsp/`: the plugin itself: its manifest, the server config and the proxy
  `scripts/pyrefly_lsp_proxy.py`.
- `tests/`: unit tests for the proxy and end-to-end tests with real pyrefly.
- `.claude-plugin/marketplace.json`: a marketplace with this one plugin.

## Development

```bash
uvx pytest                 # unit and end-to-end tests; the end-to-end ones need pyrefly on PATH
ruff check . && ruff format --check .
pyrefly check
claude plugin validate . && claude plugin validate --strict plugins/pyrefly-lsp
```

To try the plugin in a live session without installing it: `claude --plugin-dir plugins/pyrefly-lsp`. The
proxy logs to the server's stderr (`claude --debug`).

## License

Apache-2.0, see [LICENSE](LICENSE).

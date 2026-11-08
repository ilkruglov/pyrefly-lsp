# pyrefly-lsp

Плагин для Claude Code: языковой сервер Python на [pyrefly](https://pyrefly.org) (`pyrefly lsp`).
Даёт Claude навигацию по коду — переход к определению, поиск ссылок, подсказки типов — и
диагностику тем же движком, что `pyrefly check`.

## Зачем

Официальный плагин `pyright-lsp` запускает только pyright. Если типы в проекте проверяет pyrefly,
языковой сервер должен быть тем же: иначе ошибки, которые Claude видит после правки, расходятся
с проверкой проекта.

Сравнение в Claude Code на двух проектах (FastAPI, 65 и 574 файла):

| | pyright-lsp | pyrefly-lsp |
|---|---|---|
| Переход к определению | есть | есть, то же место |
| Поиск ссылок | 30 в 5 файлах; 34 в 13 | столько же |
| Подсказка типа | есть | есть |
| Переход в библиотеки `.venv` (fastapi) | не нашёл | `site-packages/fastapi/…` |

## Требования

- Claude Code с поддержкой LSP-плагинов.
- `pyrefly` в `PATH` — плагин его не ставит: `uv tool install pyrefly` (проверено с 1.4.1).

## Установка

```bash
claude plugin marketplace add ilkruglov/pyrefly-lsp
claude plugin install pyrefly-lsp@pyrefly-lsp
```

Чтобы включать сервер только в нужных проектах, выключите его глобально и включите в проекте:

```bash
claude plugin disable pyrefly-lsp@pyrefly-lsp -s user
cd ~/my-project && claude plugin enable pyrefly-lsp@pyrefly-lsp -s local
```

В проекте, где включён этот плагин, выключите `pyright-lsp`: два сервера на `.py` вместе не
работают — Claude Code запускает тот, что зарегистрирован первым.

## Настройка проекта

pyrefly сам находит `.venv` проекта. Корни импортов, пресет и исключения задаются в
`[tool.pyrefly]` файла `pyproject.toml` (или в `pyrefly.toml`):

```toml
[tool.pyrefly]
project-includes = ["src", "tests"]
search-path = ["src"]
python-version = "3.12"
preset = "default"
```

Без конфига pyrefly включает пресет `basic` — задавайте пресет явно.

## Ограничения

- Диагностики сервер присылает асинхронно: Claude Code прикрепляет их к одному из следующих
  вызовов инструмента, а не к самой правке. Для гарантированной проверки перед концом работы
  запускайте `pyrefly check`.
- Сервер стартует при первой правке `.py`-файла в сессии; на большом проекте первые запросы
  поиска ссылок могут вернуть неполный ответ, пока идёт индексация.

## Лицензия

Apache-2.0, см. [LICENSE](LICENSE).

# Kosh Kuse — голоса тестеров

Труба, которая превращает разговоры с тестерами в дашборд: загрузка → распознавание
в MacWhisper → разбор в Claude → лента цитат.

- Что строим и зачем — [`docs/TZ.md`](docs/TZ.md)
- Как устроено и почему — [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Этапы и правила работы — [`CLAUDE.md`](CLAUDE.md)

```bash
uv sync --extra dev
.venv/bin/pytest -q
```

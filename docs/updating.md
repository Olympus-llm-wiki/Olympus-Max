# Обновление и упаковка

Выпуск 0.3.0 переносит операционную среду. Источник и версии компонентов указаны
в [TEMPLATE](../TEMPLATE.json) и [toolchain](../config/toolchain.json).
Изменения общего установщика выполняются в конструкторе, в
`packages/portable-environment`, затем `sync-distributions.py` обновляет выбранные
чистые checkout редакций. Этот шаг требует ревью Git diff; настройки владельца
и личный корпус никогда не являются входом сборки.

Перед выпуском проверьте изменения, добавьте только выбранные файлы в Git index:

```bash
python3 scripts/refresh-distribution.py
python3 scripts/verify-distribution.py
python3 scripts/test.py
python3 scripts/package-release.py --output /absolute/Olympus-release.tar.gz
```

Refresh откажется при несовпадении index и рабочих файлов, либо при личных файлах
в index. Архив содержит только manifest и перечисленные в нём файлы. После
распаковки в новой папке повторите verifier и Install.command. Нельзя переносить
`.olympus-local.json`, `.codex`, `.mcp.json`, `.serena`, starter.local.json, venv
или cache другого Mac. Существующую настроенную установку обновляйте в новой
папке: конфликт конфигурации не обходится удалением чужих файлов.

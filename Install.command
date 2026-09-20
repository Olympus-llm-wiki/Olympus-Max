#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
  printf '%s\n' 'Эта поставка предназначена для Mac с Apple Silicon.'; exit 2
fi
export PATH="/opt/homebrew/opt/node@24/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
printf '%s\n' 'Olympus: установка инструментов на этом Mac. Для первого запуска нужен интернет.'
if ! command -v brew >/dev/null; then
  printf '%s\n' 'Запускается официальный установщик Homebrew. Пароль и системные подтверждения вводит владелец.'
  /bin/bash vendor/homebrew-install.sh
fi
if ! command -v uv >/dev/null; then brew install uv; fi
# uv chooses a fresh interpreter; no interpreter or venv is copied from another Mac.
uv run --no-project --python 3.12 python environment.py install "$@"
printf '\n%s\n' 'Инструменты подготовлены. Откройте эту папку в Codex или Claude и подтвердите доверие проекту.'
if [[ -f max.py ]]; then
  printf '%s\n' 'Max: далее выполните runtime prepare, затем runtime start с ключом из менеджера секретов. См. docs/environment.md.'
fi

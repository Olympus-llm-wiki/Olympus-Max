# Olympus Max 0.3.0

**Переносимая рабочая среда для Mac с Apple Silicon.**
Расширенная редакция: инструменты разработки и медиа, актуальное ядро Olympus и отдельный Hindsight в Docker.
В архиве код, 37 навыков и установщик. Свою авторизацию подключаете на новом Mac.

Распакуйте архив и откройте **Install.command**. Из Terminal:

```bash
bash Install.command
```

[Установка, состав и подключение сервисов](docs/environment.md).

```bash
./olympus doctor
./olympus tool openspec --version
./olympus tool pyright --version
./olympus project list
```

Откройте эту папку в Codex или Claude и подтвердите доверие проекту. Serena и
навыки подключаются к текущей папке. Для другого проекта сначала выберите его
через [карту проектов](docs/project-workspaces.md); cwd и MCP — разные привязки.

Команды памяти доступны через `./olympus memory`. Установщик создаёт новый локальный профиль; управление собственным Docker — через `environment.py runtime`. Внешний Hindsight по-прежнему можно подключить через `starter.py setup`; административные команды клиентского профиля остаются закрыты.

Обновление 0.3.0: переносимые devtools вместо абсолютных путей автора, новые
навыки OpenSpec и Olympus-development, три процедуры ECC, актуальный медиаразбор,
опциональный ХОВС, общий интерфейс project/workspace и упаковка по manifest.

```bash
./olympus extras media
./olympus tool reel --help
./olympus extras khvs
./olympus tool khvs doctor
python3 scripts/test.py
python3 scripts/verify-distribution.py
```

[Навыки](docs/skills.md) · [Медиа](docs/media.md) ·
[Обновление и упаковка](docs/updating.md) · [Происхождение](TEMPLATE.json).

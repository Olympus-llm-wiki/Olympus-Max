# Медиа в Olympus Max

Навык [local-media-mining](../.agents/skills/local-media-mining/SKILL.md) извлекает речь и визуальные наблюдения в локальные файлы. Python runner и проверки происходят из конструктора `950da39cb262ac6e9e7674476f499a7dfb857ce7` от 07.09.2026. Инструкции захвата адаптированы к этой редакции.

## Подготовка

Отдельно нужны Python 3.11+, `ffmpeg`/`ffprobe` в PATH и установленный, авторизованный пользователем Antigravity CLI (`agy`). Проверенный пилот конструктора: agy 1.1.27, ffmpeg 8.1.2. Runner сверяет settings и /usage; для подписочного пути требуется false-default useG1Credits. Вход и платёжные параметры настраивает пользователь штатным способом. Файлы авторизации и настройки автора не поставляются. Выбранные источники обрабатываются Google при запуске agy.

Проверь зависимости: `python3 --version`, `ffmpeg -version`, `ffprobe -version`, `agy --version`. Установка необязательных CLI не входит в запуск Olympus. Прежде чем добавлять источник, убедись, что локальное хранилище допускает его размер; полный original можно держать в отдельном корпусе с SHA-256 и ссылкой.

```bash
python3 .agents/skills/local-media-mining/scripts/media.py plan /absolute/video.mp4
python3 .agents/skills/local-media-mining/scripts/media.py run /absolute/video.mp4 --mode analyze --output /absolute/media-corpus/runs
python3 .agents/skills/local-media-mining/scripts/media.py verify /absolute/media-corpus/runs/job-id
```

`plan` и `verify` работают локально; `run` обращается к настроенному agy и использует квоту подписки. Результаты держи вне Git. При partial изучи raw и receipt; `--retry-failed` создаёт новую попытку с сохранением прежней. Текст остаётся черновиком до независимой проверки; попадание таймкодов в диапазон не доказывает их точность. Кадры не проверяют речь; sampled visuals не доказывают полноту монтажа.

Notebook — отдельная выбранная пользователем ветка: [маршрут](../.agents/skills/local-media-mining/references/notebook.md). Она требует собственного установленного коннектора/CLI; продукт их не устанавливает. Готовый текст добавляй через CLI своей редакции, не меняя исторические статусы источников.

В переносе пройдены синтетические media-регрессии. Живые модельные вызовы в этих стартерах повторно не выполнялись; прежняя приёмка runner не удостоверяет настройку другого компьютера.

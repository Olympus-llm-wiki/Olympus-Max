# Карточки содержания по сохранённым разборам

Этот путь создаёт конспекты, структуру ролика и сравнения поверх сохранённых речи,
надписей и событий. Карточки создаёт существующий подписочный Gemini. VidIQ/ClipSonar
служили источниками проектных идей и не участвуют в исполнении.

## Выбор результата

- Только стенограмма: прежнее извлечение или экспорт transcript. Смысловой проход не нужен.
- Конспект урока: профиль lesson, представление notes.
- Структура рилса: профиль reel, представление notes; видимые монтажные события — editing.
  Подробный звук/слои остаются в существующем editing-профиле.
- Сравнение: comparison по выбранным карточкам. Search — буквальный поиск по заголовку
  и тексту пункта, не семантический поиск и не доказательство полноты.

## Вход

Команды находятся в packages/reel-analysis текущего Olympus либо установленном
самостоятельном пакете gemini-reel-analysis. Из скилла разреши его физический путь
через symlink и найди пакет относительно корня проекта. Примеры выполняются из пакета.

Манифест находится вне Git. Path — каталог portable bundle с analysis.json/manifest.json,
отдельный result.json raw-batch либо каталог стандартного media.py job с manifest.json
и receipt.json. Относительные пути разрешаются от манифеста. Editing-map/visual/audio
не являются стандартным media.py входом этой ветки.

~~~json
{
  "schema_version": 1,
  "corpus_id": "course-and-reels",
  "items": [
    {"id": "lesson-01", "title": "Урок 1", "profile": "lesson", "path": "/absolute/media-runs/job"},
    {"id": "reel-01", "title": "Рилс 1", "profile": "reel", "path": "/absolute/reel-report"}
  ]
}
~~~

По умолчанию выбраны все items. Необязательное selected_ids задаёт точные IDs поднабора;
остальные остаются в описи. Запрос полного курса не разрешает молча подменять его
выборкой. При обновлении разбора сохраняй ID материала, меняй его входной snapshot.

~~~bash
uv run --no-sync reel content plan /absolute/corpus.json --output /absolute/content-cards
uv run --no-sync reel content run /absolute/corpus.json --output /absolute/content-cards --max-parts 2
~~~

Plan проверяет источники и записывает снимки без модели. Run выполняется последовательно;
max-parts ограничивает новые попытки, а не объём исходного текста. Большие наборы
делятся по part-bytes (по умолчанию 48000 байт событий на часть). Чрезмерное одиночное
наблюдение вызывает отказ до inference; не обрезай текст для прохождения проверки.
Долгий run запускай в именованном tmux.

Повтор команды переиспользует завершённые части. Подтверждённая ошибка требует
--retry-failed. Неизвестный исход сначала восстанавливается из сохранённого ответа;
если это невозможно, новые вызовы этого output удерживаются даже с retry.
Причины находятся в run-receipt.json и state.json. Новый output не используется
для обхода неизвестной попытки. Квота и авторизация — существующий Antigravity adapter.

## Представления без модели

Для transcript и editing достаточно выполнить plan, затем report; run не требуется.
Notes и comparison используют смысловые пункты, подготовленные через run.

~~~bash
uv run --no-sync reel content report --output /absolute/content-cards --view transcript --target /absolute/new-transcript
uv run --no-sync reel content report --output /absolute/content-cards --view notes --target /absolute/new-notes
uv run --no-sync reel content report --output /absolute/content-cards --view editing --target /absolute/new-editing
uv run --no-sync reel content report --output /absolute/content-cards --view comparison --target /absolute/new-comparison
uv run --no-sync reel content search --output /absolute/content-cards --query "проверяемое понятие"
uv run --no-sync reel content check /absolute/new-notes
~~~

Нужен новый target-каталог. Комплект: report.md, report.json, cards/*.json,
sources/*.md и integrity.json. Внутренние ссылки работают в перенесённой копии.
Markdown-стенограмма сохраняет исходные строки в текстовых блоках. Для последующей
обработки бери точные source.transcript[].text из карточки.

Lesson: concept/step/demonstration/example/caveat/conclusion.
Reel: hook/promise/proof/body/payoff/cta/delivery. Типы являются интерпретацией;
склейка не доказывает новый этап сюжета. Выдуманные IDs отклоняются, но наличие
ссылки не доказывает истинность объяснения.

Для нового вопроса сначала прочитай карточки и найденные основания. Если данных
недостаточно, вернись к указанному фрагменту через прежний inspect/проверку оригинала
и затем перестрой карточку. Не достраивай неизвестный звук по надписям.

## Покрытие и версии

Draft означает обработку всех доступных групп наблюдений, а не всего оригинала:
урок с одной готовой частью из 22 по-прежнему имеет частичный источник.
Речь/времена остаются непроверенными. Перекрытия media.py сохраняются; уточнения
не заменяют исходные наблюдения автоматически.

Check возвращает current/stale/missing/corrupt с конкретными причинами. Изменение
родительского файла, карточки или состава manifest делает старый отчёт неактуальным.
Комплект читается на другой машине, но отсутствие родительских путей не позволяет
подтвердить актуальность. Для перенесённого состояния можно задать --manifest и
--output; исходные parent paths карточек всё равно проверяются.
Current подтверждает целостность и версии, не точность распознавания.

Старые карточки и отчёты сохраняются. Полные тексты, snapshots, raw и расходы — вне Git.
Создание карточки не включает доставку в память; существенный синтез сохраняй по
контракту проекта.

# Навыки рабочей среды

В выпуске 37 навыков: 25 Matt Pocock, 6 OpenSpec, local-media-mining,
olympus-development, khvs и 3 ECC (tdd-workflow, verification-loop, coding-standards).
Происхождение и версии — [TEMPLATE](../TEMPLATE.json); полный состав и hashes —
[DISTRIBUTION](../DISTRIBUTION.json). Upstream Matt сохранён по
[закреплённому manifest](../config/matt-pocock-skills.json).

Codex читает `.agents/skills`; Claude — относительную ссылку `.claude/skills`.
Привязка MCP создаётся установщиком, native trust подтверждает владелец.
Навыки доступны для выбора, их наличие не запускает процессы и не выдаёт доступ
к аккаунтам. Маршрут [инженерной работы](../.agents/skills/olympus-development/SKILL.md)
использует `ops.py` обеих редакций. Для медиа устанавливаются optional dependencies
через `environment.py extras media`, для ХОВС — `extras khvs`.

Лицензии: [Matt MIT](../licenses/matt-pocock-skills-MIT.txt), [ECC MIT](../licenses/ecc-MIT.txt).
Проверка OpenAI Docs от 20.09.2026: [project configuration](https://learn.chatgpt.com/docs/config-file/config-reference).
Дата публикации страницы не указана; MCP загружается после доверия проекту.

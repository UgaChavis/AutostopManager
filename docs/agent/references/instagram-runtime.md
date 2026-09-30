# Instagram через Windsor.ai

Сценарии: [H1 — Windsor.ai](../modules/H1.md), [H2 — Instagram AutoStop](../modules/H2.md), [Instagram skill](../../../.agents/skills/manage-owner-instagram/SKILL.md). Instagram владеет публикациями и комментариями; Manager не сохраняет копию ленты/переписки.

По поручению владельца установи ровно один текущий аккаунт через `get_connectors`. Чтение: `get_fields` → `get_data`. Запись: `list_actions` → текущая схема разрешённого `execute_action` → независимое чтение конкретного результата. ID и media URL берутся из текущего ответа/схемы. Доступные типы могут включать фото, carousel, Reels, stories и создание/ответ/скрытие/восстановление/удаление комментария; discovery не подтверждает успешную запись.

Проверяй доступ отдельно в интерактивном Codex, CLI и существующем wake-чате: `app/list`, `mcpServerStatus/list`, затем read-only `get_connectors`. CLI discovery использует свой cwd/config. Схема связи: [OpenAI App Server](https://developers.openai.com/codex/app-server). Установка Manager не авторизует внешний аккаунт. Тестовая публикация не является технической проверкой чтения.

Direct и Instagram event trigger текущим коннектором не предоставляются. Отсутствие stories допустимо; они ограничены во времени. Реальный период проверяй по timestamp: media tables могут вернуть историю шире заданного окна.

При отсутствии/нескольких аккаунтах сначала разреши цель; permission error проверяй через подключение плагина; пустые данные — поля, таблицу, период и её ограничения. После неизвестного исхода записи читай точную публикацию/комментарий, прежде чем повторять. Коммерческие факты проверяются в [Store](../modules/F2.md); release/runtime — [host-operations.md](host-operations.md).

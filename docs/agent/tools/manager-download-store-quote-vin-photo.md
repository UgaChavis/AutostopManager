# manager.download_store_quote_vin_photo — download_store_quote_vin_photo

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: store; первичная база: AutoStop Store scoped quote VIN attachment preview (Store agent API). Исполнение: network_read.

Вызов: native Manager MCP `download_store_quote_vin_photo`.

Входы: `quote_request_id`, `expected_photo_sha256`.
Defaults: `{}`.
Обязательные facade поля: `quote_request_id`, `expected_photo_sha256`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"expected_photo_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","quote_request_id":"synthetic-quote"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"expected_photo_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","quote_request_id":null}
```

Выход: data.content_base64 и data.content_type=image/jpeg; summary.attachment: sha256 исходного вложения, preview_sha256 и byte_size полученного preview..
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- HTTP-чтение одного точного quote с scoped quote credential; expected_photo_sha256 связывает запрос с текущим исходным вложением.
- Возвращает bounded JPEG preview в Base64 в памяти; инструмент не создаёт локальный файл. Preview SHA-256 вычисляется по возвращённым байтам и может отличаться от SHA исходного фото.
- Сохраняй фото только транзитно в рамках разрешённого кейса; если вызывающая сторона отдельно создала приватный файл, очисти его после использования.
- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/store-api.md).

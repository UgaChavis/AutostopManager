# E7: публичная сверка OEM-кандидатов

Сценарий: [E7](../modules/E7.md). `verify_oem_candidates_web` принимает найденные кандидаты и профиль детали: make/model/year/engine, ось, сторону, позицию. VIN-поля нет; VIN-подобный текст с `/`, `_`, `.`, `-` удаляется из запросов, VIN вместо номера отклоняется.

По умолчанию `live_search=false`, внешних вызовов нет. При включении `WebResearchGatewayV1` выполняет `research_part_public_evidence`. E8 — read-only gateway по четырём каноническим источникам: PartSouq, Amayama, Emex, Exist; не более пяти кандидатов и двух коротких выдержек. E7 проверяет source ID/type/domain по локальному набору. Без authenticated E8 client применяется явно отмеченный ограниченный DuckDuckGo fallback.

Маршруты берутся из [automotive sources](../automotive_sources/automotive_repair_sources_catalog.json) с `include_licensed=False`: paid/login/registration/subscription не выдаются. Ответ содержит OEM, имя, ссылки, условия применимости, противоречия, статус и следующий ручной шаг. Gateway failure возвращает `search_failed` без внутренних ошибок.

Ссылка/snippet оставляет результат кандидатом до доверенной EPC-проверки. Конфликт или неудачная проверка — «не подтверждён». E7 не пишет CRM, не заказывает и не покупает детали. Source: [oem_candidate_web_evidence.py](../../../autostop_manager/oem_candidate_web_evidence.py).

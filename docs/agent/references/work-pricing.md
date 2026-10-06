# Независимые нормочасы и стоимость работ

[E12](../modules/E12.md) получает исходные AUTONORMS rows; [E13](../modules/E13.md) отделяет acquisition от calculation.
normalize_labor_time(rows, unit, source) сохраняет raw workName/workTime/unit, normalized hours,
operation scope/source, included_operations и overlaps_with. Неизвестная единица — unknown, не часы по умолчанию.
collect_work_price_evidence принимает выбранные kinds/sources и обезличенный профиль,
либо explicit aggregates. Публичный research ограничен deadline; internal experience не загружается неявно.
calculate_work_price(labor, policy, hourly_rate, observations, unknown_costs) является pure.
policy.version и basis обязательны; hourly_rate либо сопоставимые public observations являются явной основой.
policy.coefficient/rounding/currency сохраняются. Unknown expense не ноль и оставляет total неизвестным.
Included operations не считаются дважды; unresolved overlap исключается с missing adjustment.
Пример: 2 часа × ставка1000, policy hourly_rate/version=demo-v1 → known_subtotal2000.
Неизвестная диагностика остаётся unknown/excluded; такой расчёт не обещает полный клиентский итог.
estimate_repair_work_cost совместим: defaults auto_research=true/use_internal_experience=true.
auto_research=false сам по себе не запрещает legacy experience read; pure helper не делает его вообще.
Exact signatures/примеры/ошибки — карточки tools; inputSchema — текущая MCP registration.

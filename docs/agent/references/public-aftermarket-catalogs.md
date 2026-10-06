# MANN и DENSO

public_aftermarket_catalog_lookup(provider="mann"|"denso", part_number=...) читает выбранный публичный каталог.
Две provider bindings имеют отдельные stable tool_id, хотя MCP wrapper общий.
MANN product/OE/comparison numbers и DENSO catalog article могут быть кандидатами и OE references.
Ни cross, ни article не становится exact VIN OEM/fitment. Дополнительные условия — [E6](../modules/E6.md).
Empty/parse gap/provider failure не доказывает отсутствие детали.
Сеть только выбранного источника; secrets/private VIN в публичные queries не включаются.
Карточки с параметрами и примерами: [MANN](../tools/aftermarket-mann.md), [DENSO](../tools/aftermarket-denso.md).

# Технические автомобильные источники

[E14](../modules/E14.md) получает существующие NHTSA recalls/TSB metadata и Mercedes/ZF research routes.
[E15](../modules/E15.md) ищет и читает публичные материалы с URL/time/primary lineage.
recommend_automotive_sources — локальный реестр маршрутов, не API/лицензия/подключение базы.
Маршруты и условия доступа — [каталог автомобильных источников](../automotive_sources/automotive_repair_sources_catalog.json);
открытые recalls/TSB/VIN endpoints — [реестр endpoints](../automotive_sources/open_dataset_endpoints.json).
Локальные VIN/OEM маршруты — [реестр источников VIN/OEM](../vin_oem_sources.json).
Это versioned правила выбора; URL и запись реестра не подтверждают текущую доступность или точность базы.
Elcats/Japancats/Ssangyong имеют отдельные канонические source IDs в обоих реестрах.
Полная матрица опубликованных легковых каталогов и операций — [Elcats](elcats.md);
source link и implemented tool не меняют robots запрет, флаг или фактическую готовность.
Готовый профиль можно сопоставить с каталогом без повторного decode; компонентный
каталог Bosch не становится каталогом модификаций или точным OEM EPC автомобиля.
lookup_public_automotive_evidence сохраняет ограниченную область model/year/system.
Model recall не VIN campaign status и не диагноз конкретного автомобиля.
Полный EPC/service procedures/DTC/frame extension требует законно доступной базы и реального adapter contract.
Roadmap карточки не имеют executable invocation. Купить доступ этим рефакторингом не разрешено.
Public materials не подтверждают узкие engine/KPP/options/date условия без первичных evidence.
Для fitment используй [E6](../modules/E6.md), сохраняя unknown/conflict.

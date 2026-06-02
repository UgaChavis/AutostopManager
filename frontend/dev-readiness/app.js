const tools = [
  {
    name: "ripgrep / rg",
    status: "ready",
    detail: "Быстрый поиск по коду и документации с уважением .gitignore.",
    meta: ["apt: ripgrep", "/usr/bin/rg", "14.1.0"],
  },
  {
    name: "fd",
    status: "ready",
    detail: "Быстрый поиск файлов. На Ubuntu установлен fdfind, добавлена ссылка fd.",
    meta: ["apt: fd-find", "/usr/local/bin/fd", "9.0.0"],
  },
  {
    name: "grep / find / jq / git",
    status: "ready",
    detail: "Базовые системные инструменты уже были доступны до установки.",
    meta: ["/usr/bin/grep", "/usr/bin/find", "/usr/bin/jq", "/usr/bin/git"],
  },
  {
    name: "tree",
    status: "ready",
    detail: "Быстрая карта структуры проекта без чтения лишних каталогов.",
    meta: ["apt: tree", "2.1.1"],
  },
  {
    name: "shellcheck",
    status: "ready",
    detail: "Проверка shell-команд и скриптов перед автоматизацией.",
    meta: ["apt: shellcheck", "0.9.0"],
  },
  {
    name: "uv",
    status: "ready",
    detail: "Быстрая установка и синхронизация Python-зависимостей.",
    meta: ["pipx: uv", "0.11.17"],
  },
  {
    name: "ruff",
    status: "ready",
    detail: "Очень быстрый lint/format аудит Python-кода.",
    meta: ["pipx: ruff", "0.15.15"],
  },
  {
    name: "mypy",
    status: "ready",
    detail: "Статическая проверка типов для точечных контрактов и адаптеров.",
    meta: ["apt: mypy", "1.9.0"],
  },
  {
    name: ".venv",
    status: "ready",
    detail: "Проектное окружение содержит mcp, pytest, httpx и pydantic-settings.",
    meta: [".venv/bin/python", "pytest 9.0.3", "mcp ready"],
  },
  {
    name: "Node / npm",
    status: "optional",
    detail: "Не требуется для этого статического фронта. Имеет смысл ставить при переходе к SPA.",
    meta: ["optional", "not installed"],
  },
];

const providers = [
  {
    name: "NHTSA vPIC",
    stage: "identity",
    status: "ready",
    detail: "Публичный VIN/WMI decode. Это не EPC и не источник OEM-номеров.",
    meta: ["public_api", "live callable"],
  },
  {
    name: "Local platform rules",
    stage: "identity",
    status: "ready",
    detail: "Локальные правила ROW/JDM/WMI и CRM-conflict checks.",
    meta: ["local_rules", "no secrets"],
  },
  {
    name: "Parts-Catalogs API",
    stage: "oem_catalog",
    status: "missing",
    detail: "Основной кандидат VIN/frame -> vehicle -> group -> OEM parts.",
    meta: ["PARTS_CATALOGS_API_KEY", "PARTS_CATALOGS_BASE_URL"],
  },
  {
    name: "17VIN API",
    stage: "oem_catalog",
    status: "missing",
    detail: "Второй источник EPC: VIN decode, common parts, part search by VIN.",
    meta: ["VIN17_ACCOUNT", "VIN17_SECRET"],
  },
  {
    name: "partslink24 / brand EPC",
    stage: "oem_catalog",
    status: "manual",
    detail: "Дилерский уровень для BMW/VAG/Mercedes и точных опций.",
    meta: ["manual subscription", "no secret in Git"],
  },
  {
    name: "PARTSAPI.RU",
    stage: "catalog_cross",
    status: "missing",
    detail: "VINdecodeOE, getPartsbyVIN, applicability, crosses.",
    meta: ["PARTSAPI_KEY", "PARTSAPI_BASE_URL"],
  },
  {
    name: "AUTOPOISK",
    stage: "catalog_cross",
    status: "missing",
    detail: "Профессиональный EPC/Cross tab под подпиской.",
    meta: ["AUTOPOISK_TOKEN"],
  },
  {
    name: "ROSSKO",
    stage: "procurement_price",
    status: "missing",
    detail: "Ключевой поставщик для закупки, склада и сроков по Красноярску.",
    meta: ["ROSSKO_KEY1", "ROSSKO_KEY2"],
  },
  {
    name: "AutoEuro / Armtek / Autopiter",
    stage: "procurement_price",
    status: "missing",
    detail: "Дополнительные B2B источники цены, склада, сроков и брендов.",
    meta: ["AUTOEURO_API_KEY", "ARMTEK_LOGIN", "AUTOPITER_USER_ID"],
  },
  {
    name: "ZZap",
    stage: "market_price",
    status: "missing",
    detail: "Рыночная РФ-оценка и видимость замен, отдельно от закупки.",
    meta: ["ZZAP_API_KEY", "manual fallback"],
  },
];

const commands = [
  {
    title: "Быстрый поиск VIN/OEM кода",
    command: "rg -n \"VIN|OEM|EPC|catalog|partsapi|vin17\" autostop_manager docs tests",
  },
  {
    title: "Карта проекта",
    command: "tree -a -I '.git|.venv|__pycache__|.pytest_cache|data|generated_invoices' -L 2",
  },
  {
    title: "Проверить готовность провайдеров",
    command: ".venv/bin/python -m autostop_manager.cli catalog-status",
  },
  {
    title: "План CRM VIN -> OEM -> кроссы -> запись",
    command:
      ".venv/bin/python -m autostop_manager.cli crm-vin-parts-plan --card-id demo --part \"передние колодки\" --vin SYNTHETICVIN00001 --make Toyota --model Prado --city Красноярск",
  },
  {
    title: "Фокусные тесты VIN/OEM слоя",
    command:
      ".venv/bin/python -m pytest -q tests/test_vin_lookup.py tests/test_vehicle_identity.py tests/test_catalog_clients.py tests/test_oem_catalog_lookup.py tests/test_crm_vin_parts_workflow.py tests/test_vin_parts_benchmark.py tests/test_vin_parts_work_order.py",
  },
  {
    title: "Быстрый lint Python",
    command: "ruff check autostop_manager tests",
  },
];

const backlog = [
  {
    title: "1. EPC credentials and legal access",
    status: "missing",
    detail:
      "Получить тестовые ключи/подписки Parts-Catalogs, PartsAPI, 17VIN и определить, какие бренды реально закрывают AutoStop VIN/frame кейсы.",
    meta: ["read-only first", "no secrets in Git"],
  },
  {
    title: "2. Единый contract для OEM candidates",
    status: "manual",
    detail:
      "Зафиксировать нормализованные поля: provider, OEM number, group, side, position, quantity, production split, options, supersession, confidence.",
    meta: ["catalog_clients.py", "tests/test_oem_catalog_lookup.py"],
  },
  {
    title: "3. Golden fixtures без реальных VIN",
    status: "manual",
    detail:
      "Собрать синтетические payload fixtures для Parts-Catalogs, PartsAPI и 17VIN, чтобы тестировать нормализацию без клиентских VIN.",
    meta: ["privacy gate", "synthetic identifiers"],
  },
  {
    title: "4. Provider smoke bench",
    status: "manual",
    detail:
      "Добавить команду, которая проверяет ключи, dry-run формы запросов и один read-only тестовый lookup по каждому провайдеру.",
    meta: ["no CRM writes", "redacted output"],
  },
  {
    title: "5. CRM writeback gate",
    status: "manual",
    detail:
      "Разрешать финальную строку материалов только после VIN/frame-specific OEM, применяемости, выбранного артикула и подтверждённой цены.",
    meta: ["description matrix", "materials selected part only"],
  },
  {
    title: "6. Optional Node frontend",
    status: "optional",
    detail:
      "Если этот статический экран станет рабочим интерфейсом, тогда ставить Node/npm и делать SPA. Сейчас сборка не нужна.",
    meta: ["defer", "static works now"],
  },
];

const panels = {
  tools: document.querySelector('[data-panel="tools"]'),
  providers: document.querySelector('[data-panel="providers"]'),
  commands: document.querySelector('[data-panel="commands"]'),
  backlog: document.querySelector('[data-panel="backlog"]'),
};

const searchInput = document.querySelector("#search");
const segments = [...document.querySelectorAll(".segment")];

function statusText(status) {
  return {
    ready: "Готово",
    missing: "Нет доступа",
    manual: "Ручной доступ",
    optional: "Опционально",
  }[status] || status;
}

function cardTemplate(item, className) {
  const searchable = [item.name || "", item.title || "", item.detail || "", item.stage || "", ...item.meta]
    .join(" ")
    .toLowerCase();
  const meta = item.meta.map((value) => `<span class="meta-chip">${value}</span>`).join("");
  return `
    <article class="${className}" data-search="${searchable}">
      <div class="card-topline">
        <h3>${item.name || item.title}</h3>
        <span class="pill ${item.status}">${statusText(item.status)}</span>
      </div>
      <p>${item.detail}</p>
      <div class="meta-list">${meta}</div>
    </article>
  `;
}

function renderTools() {
  panels.tools.innerHTML = tools.map((item) => cardTemplate(item, "tool-card")).join("");
}

function renderProviders() {
  panels.providers.innerHTML = providers.map((item) => cardTemplate(item, "provider-card")).join("");
}

function renderCommands() {
  panels.commands.innerHTML = commands
    .map(
      (item, index) => `
        <article class="command-card" data-search="${[item.title, item.command].join(" ").toLowerCase()}">
          <div>
            <h3>${item.title}</h3>
            <code>${item.command}</code>
          </div>
          <button class="copy-button" type="button" data-command-index="${index}">Copy</button>
        </article>
      `,
    )
    .join("");
}

function renderBacklog() {
  panels.backlog.innerHTML = backlog.map((item) => cardTemplate(item, "backlog-card")).join("");
}

function activeView() {
  return document.querySelector(".segment.active")?.dataset.view || "overview";
}

function filterVisibleCards() {
  const query = searchInput.value.trim().toLowerCase();
  const view = activeView();
  const visiblePanel = document.querySelector(`[data-panel="${view}"]`);
  if (!visiblePanel || view === "overview") return;

  const cards = [...visiblePanel.querySelectorAll("[data-search]")];
  let visibleCount = 0;
  cards.forEach((card) => {
    const visible = !query || card.dataset.search.includes(query);
    card.hidden = !visible;
    visibleCount += visible ? 1 : 0;
  });

  visiblePanel.querySelector(".empty-state")?.remove();
  if (!visibleCount) {
    visiblePanel.insertAdjacentHTML("beforeend", '<div class="empty-state">Ничего не найдено</div>');
  }
}

function switchView(view) {
  document.querySelectorAll("[data-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.panel !== view;
  });
  segments.forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  filterVisibleCards();
}

function bindCopyButtons() {
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-command-index]");
    if (!button) return;
    const command = commands[Number(button.dataset.commandIndex)].command;
    await copyText(command);
    button.classList.add("copied");
    button.textContent = "Copied";
    setTimeout(() => {
      button.classList.remove("copied");
      button.textContent = "Copy";
    }, 1200);
  });
}

async function copyText(value) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const field = document.createElement("textarea");
  field.value = value;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.left = "-9999px";
  document.body.appendChild(field);
  field.select();
  document.execCommand("copy");
  field.remove();
}

renderTools();
renderProviders();
renderCommands();
renderBacklog();
bindCopyButtons();
segments.forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
searchInput.addEventListener("input", filterVisibleCards);

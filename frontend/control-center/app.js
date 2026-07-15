const stateClass = {
  green: "status-green",
  yellow: "status-yellow",
  red: "status-red",
};

const emptyReport = {
  schema: "ControlReportV1",
  generated_at: "",
  summary: { status: "unknown", risk_count: 0 },
  risks: [],
  git: { dirty: false, branch: "unknown", status_short: [] },
  providers: { provider_count: 0, configured_count: 0, stage_matrix: [] },
  memory: { total_count: 0, duplicate_groups: 0, expired_count: 0, superseded_count: 0 },
  knowledge: { documents_indexed: 0, domain_count: 0, annotation_count: 0 },
  mcp: { manager: { tool_count: 0 }, crm: { tool_count: 0 } },
  server_environment: { ok: false, os: {}, core_tools: {}, ports: {} },
  codex_readiness: { ok: false, skills: {}, hooks: {}, mcp_imports: {} },
  runtime_readiness: { ok: false, manager_venv: {}, crm_venv: {}, browser_document_tools: {}, env_files: {} },
  production_ops: { ok: false, compose: {}, nginx: {}, watchdog: {}, container: {} },
  open_risk: { score: 0, level: "unknown", items: [] },
  ports: { public_listeners: [], local_listeners: [] },
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setStatus(status) {
  const badge = document.querySelector("#statusBadge");
  badge.className = `status ${stateClass[status] || "status-unknown"}`;
  badge.textContent = status || "unknown";
}

function metric(label, value, tone = "blue") {
  return `<div class="metric" style="border-left-color: var(--${tone})">
    <span>${escapeHtml(label)}</span>
    <strong>${escapeHtml(value)}</strong>
  </div>`;
}

function metricRow(label, value) {
  return `<div class="metric-row"><div class="label-row"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(value)}</span></div></div>`;
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function render(rawReport) {
  const report = rawReport && typeof rawReport === "object" && !Array.isArray(rawReport) ? rawReport : emptyReport;
  const summary = report.summary || {};
  setStatus(summary.status || "unknown");
  document.querySelector("#summaryGrid").innerHTML = [
    metric("Risks", summary.risk_count ?? 0, summary.red_risks ? "red" : summary.yellow_risks ? "yellow" : "green"),
    metric("Memory", summary.memory_total ?? report.memory?.total_count ?? 0, "blue"),
    metric("Knowledge Docs", summary.knowledge_documents ?? report.knowledge?.documents_indexed ?? 0, "green"),
    metric(
      "Providers",
      `${summary.providers_configured ?? report.providers?.configured_count ?? 0}/${summary.providers_total ?? report.providers?.provider_count ?? 0}`,
      "yellow",
    ),
    metric("Open Risk", `${summary.open_risk_score ?? report.open_risk?.score ?? 0}`, summary.open_risk_level === "red" ? "red" : summary.open_risk_level === "yellow" ? "yellow" : "green"),
  ].join("");

  renderRisks(asArray(report.risks));
  renderGit(report.git || {});
  renderProviders(report.providers || {});
  renderKnowledgeMemory(report);
  renderMcp(report.mcp || {});
  renderPorts(report.ports || {});
  renderEnvironment(report);
  renderRuntimeProduction(report);

  document.querySelector("#generatedAt").textContent = report.generated_at
    ? `Generated ${report.generated_at}`
    : "No report loaded";
}

function renderRisks(risks) {
  document.querySelector("#riskCount").textContent = String(risks.length);
  const container = document.querySelector("#risks");
  if (!risks.length) {
    container.innerHTML = `<div class="risk green"><strong>No active risks</strong><span class="small">green</span></div>`;
    return;
  }
  container.innerHTML = risks
    .map((risk) => `<div class="risk ${escapeHtml(risk.severity)}">
      <div class="label-row"><strong>${escapeHtml(risk.category)}</strong><span class="small">${escapeHtml(risk.severity)}</span></div>
      <span class="small">${escapeHtml(risk.message)}</span>
    </div>`)
    .join("");
}

function renderGit(git) {
  document.querySelector("#gitState").textContent = git.dirty ? "dirty" : "clean";
  const statusLines = asArray(git.status_short);
  const lines = statusLines.length ? statusLines.join("\n") : `branch ${git.branch || "unknown"}`;
  document.querySelector("#gitLines").textContent = lines;
}

function renderProviders(providers) {
  document.querySelector("#providerRatio").textContent =
    `${providers.configured_count || 0}/${providers.provider_count || 0}`;
  const rows = asArray(providers.stage_matrix);
  document.querySelector("#providers").innerHTML = rows.length
    ? rows
      .map((row) => `<div class="matrix-row">
        <div class="label-row"><strong>${escapeHtml(row.label || row.stage)}</strong><span class="small">${escapeHtml(row.stage)}</span></div>
        <span class="small">${escapeHtml(row.configured_count || 0)} configured, ${escapeHtml(row.live_callable_count || 0)} live-readable</span>
      </div>`)
      .join("")
    : `<div class="matrix-row"><strong>No provider matrix</strong><span class="small">unknown</span></div>`;
}

function renderKnowledgeMemory(report) {
  const memory = report.memory || {};
  const knowledge = report.knowledge || {};
  document.querySelector("#memoryTotal").textContent = String(memory.total_count || 0);
  document.querySelector("#knowledgeMemory").innerHTML = [
    ["Memory total", memory.total_count || 0],
    ["Duplicate groups", memory.duplicate_groups || 0],
    ["Expired memory", memory.expired_count || 0],
    ["Knowledge domains", knowledge.domain_count || 0],
    ["Knowledge docs", knowledge.documents_indexed || 0],
    ["Annotations", knowledge.annotation_count || 0],
  ]
    .map(([label, value]) => metricRow(label, value))
    .join("");
}

function renderMcp(mcp) {
  const managerCount = mcp.manager?.tool_count || 0;
  const crmCount = mcp.crm?.tool_count || 0;
  document.querySelector("#mcpCount").textContent = String(managerCount + crmCount);
  document.querySelector("#mcp").innerHTML = [
    ["Manager tools", managerCount],
    ["CRM tools", crmCount],
    ["Manager catalog", mcp.manager?.ok ? "ok" : "check"],
    ["CRM catalog", mcp.crm?.ok ? "ok" : "check"],
  ]
    .map(([label, value]) => metricRow(label, value))
    .join("");
}

function renderPorts(ports) {
  const listeners = [...asArray(ports.public_listeners), ...asArray(ports.local_listeners)];
  document.querySelector("#portCount").textContent = String(listeners.length);
  document.querySelector("#ports").innerHTML = listeners.length
    ? listeners.slice(0, 10).map((item) => `<div class="port-row"><span class="small">${escapeHtml(item.line || item.local_address)}</span></div>`).join("")
    : `<div class="port-row"><strong>No listener data</strong><span class="small">unknown</span></div>`;
}

function boolLabel(value) {
  return value ? "ok" : "check";
}

function renderEnvironment(report) {
  const env = report.server_environment || {};
  const codex = report.codex_readiness || {};
  document.querySelector("#environmentState").textContent = env.ok && codex.ok ? "ok" : "check";
  const requiredTools = env.core_tools?.required_present_count ?? 0;
  const systemSkills = codex.skills?.system_skill_count ?? 0;
  const pluginSkills = codex.skills?.plugin_skill_count ?? 0;
  document.querySelector("#environment").innerHTML = [
    ["OS", env.os?.pretty_name || "unknown"],
    ["Required tools", `${requiredTools}`],
    ["System skills", systemSkills],
    ["Plugin skills", pluginSkills],
    ["Manager hook", boolLabel(codex.hooks?.manager?.hook_installed)],
    ["CRM hook", boolLabel(codex.hooks?.crm?.hook_installed)],
    ["Manager MCP import", boolLabel(codex.mcp_imports?.manager?.ok)],
    ["CRM MCP import", boolLabel(codex.mcp_imports?.crm?.ok)],
  ]
    .map(([label, value]) => metricRow(label, value))
    .join("");
}

function renderRuntimeProduction(report) {
  const runtime = report.runtime_readiness || {};
  const prod = report.production_ops || {};
  document.querySelector("#runtimeState").textContent = runtime.ok && prod.ok ? "ok" : "check";
  document.querySelector("#runtimeProduction").innerHTML = [
    ["Manager venv", boolLabel(runtime.manager_venv?.ok)],
    ["CRM venv", boolLabel(runtime.crm_venv?.ok)],
    ["Browser/docs", boolLabel(runtime.browser_document_tools?.ok)],
    ["Manager env keys", runtime.env_files?.manager?.key_count ?? 0],
    ["CRM env keys", runtime.env_files?.crm?.key_count ?? 0],
    ["Compose config", boolLabel(prod.compose?.config?.ok)],
    ["Nginx config", boolLabel(prod.nginx?.config?.ok)],
    ["Watchdog timer", prod.watchdog?.timer?.active_state || "unknown"],
    ["CRM container", prod.container?.autostopcrm?.health || prod.container?.autostopcrm?.state || "unknown"],
  ]
    .map(([label, value]) => metricRow(label, value))
    .join("");
}

fetch("./control-report.json", { cache: "no-store" })
  .then((response) => {
    if (!response.ok) throw new Error("missing report");
    return response.json();
  })
  .then(render)
  .catch(() => render(emptyReport));

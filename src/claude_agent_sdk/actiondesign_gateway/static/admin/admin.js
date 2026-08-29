"use strict";

const API = "/api/actiondesign-agent";
const PAGE_SIZE = 20;
const EVENT_PAGE_SIZE = 50;
const PROVIDER_GROUPS = [
  ["openai", "OpenAI"],
  ["claude", "Claude"],
  ["mimo", "MiMo"],
  ["claude-code", "Claude Code"],
];
const state = {
  sessionToken: "",
  sessionExpiresAt: "",
  sessionEpoch: 0,
  controllers: new Set(),
  profiles: [],
  models: null,
  health: new Map(),
  policy: null,
  mcpServers: [],
  mcpDeployments: [],
  mcpDeploymentJobId: "",
  mcpDeploymentPollTimer: 0,
  validatingMcpDeployment: false,
  managedSecrets: [],
  clientApiKeys: [],
  editingProvider: null,
  editingModel: null,
  auditPage: 1,
  conversationPage: 1,
  requestPage: 1,
  conversationDetail: null,
};

const $ = (id) => document.getElementById(id);
const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
};

function sanitizeText(value) {
  return String(value ?? "")
    .replace(/\b(Bearer|Basic)\s+\S+/gi, "$1 [REDACTED]")
    .replace(/\b(api[_-]?key|password|token|authorization|secret|cookie|credential)\s*[:=]\s*[^\s,;]+/gi, "$1=[REDACTED]")
    .slice(0, 1000);
}

function showNotice(message, kind = "success") {
  const notice = $("notice");
  notice.textContent = sanitizeText(message);
  notice.className = `notice ${kind}`;
  clearTimeout(showNotice.timer);
  showNotice.timer = setTimeout(() => notice.classList.add("hidden"), 4500);
}

function showError(prefix, error) {
  if (!error || error.name === "AbortError" || !error.message) return;
  showNotice(`${prefix}：${sanitizeText(error.message)}`, "error");
}

function errorMessage(payload, response) {
  const detail = payload && payload.detail;
  if (typeof detail === "string") return sanitizeText(detail);
  if (detail && typeof detail === "object") {
    if (detail.code === "ADMIN_REMOTE_DISABLED") {
      return "当前服务器禁止公网管理登录，请通过 SSH 隧道访问 127.0.0.1 后重试";
    }
    if (detail.code === "ADMIN_HTTPS_REQUIRED") {
      return "远程管理登录只允许通过 HTTPS，请使用服务器提供的 https:// 管理地址";
    }
    if (detail.code === "PROFILE_HOST_NOT_ALLOWED") {
      return "该 Base URL 主机未加入服务器 ACTIONDESIGN_PROFILE_ALLOWED_HOSTS";
    }
    return sanitizeText(detail.message || detail.code || "请求失败");
  }
  return sanitizeText((payload && (payload.message || payload.error)) || `${response.status} ${response.statusText}`);
}

async function api(path, options = {}, admin = true) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (admin) {
    if (!state.sessionToken) throw new Error("请先登录管理会话");
    headers.set("Authorization", `Bearer ${state.sessionToken}`);
  }
  const epoch = state.sessionEpoch;
  const controller = new AbortController();
  state.controllers.add(controller);
  let response;
  try {
    response = await fetch(`${API}${path}`, {
      ...options,
      headers,
      signal: controller.signal,
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
      credentials: "same-origin",
    });
  } finally {
    state.controllers.delete(controller);
  }
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) {
    if (response.status === 401 && admin) logout(false);
    throw new Error(errorMessage(payload, response));
  }
  if (admin && epoch !== state.sessionEpoch) throw new Error("");
  return payload;
}

function redact(value, depth = 0) {
  if (depth > 12) return "[TRUNCATED]";
  if (Array.isArray(value)) return value.slice(0, 500).map((item) => redact(item, depth + 1));
  if (value && typeof value === "object") {
    const result = {};
    for (const [key, item] of Object.entries(value)) {
      result[key] = /api.?key|password|token|authorization|secret|cookie|credential/i.test(key)
        ? "[REDACTED]"
        : redact(item, depth + 1);
    }
    return result;
  }
  return typeof value === "string" ? sanitizeText(value) : value;
}

function badge(text, color = "") { return element("span", `badge ${color}`.trim(), text); }
function td(text, className = "") { return element("td", className, text); }
function emptyRow(tbody, columns, message) {
  const row = element("tr");
  const cell = td(message, "empty");
  cell.colSpan = columns;
  row.append(cell);
  tbody.replaceChildren(row);
}
function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? sanitizeText(value) : date.toLocaleString("zh-CN", { hour12: false });
}
function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
function formatUsage(usage) {
  if (!usage || typeof usage !== "object") return "—";
  const input = usage.input_tokens ?? usage.prompt_tokens;
  const output = usage.output_tokens ?? usage.completion_tokens;
  const total = usage.total_tokens;
  return [input !== undefined ? `in ${input}` : "", output !== undefined ? `out ${output}` : "", total !== undefined ? `total ${total}` : ""].filter(Boolean).join(" · ") || "—";
}
function toIso(value) { return value ? new Date(value).toISOString() : ""; }

function updateSessionUi() {
  const loggedIn = Boolean(state.sessionToken);
  document.body.classList.toggle("authenticated", loggedIn);
  $("login-panel").classList.toggle("hidden", loggedIn);
  $("logout-btn").classList.toggle("hidden", !loggedIn);
  $("session-state").textContent = loggedIn ? `管理会话已登录${state.sessionExpiresAt ? ` · ${formatTime(state.sessionExpiresAt)} 到期` : ""}` : "未登录";
}

function clearDisplayedClientKey() {
  $("revealed-client-key-value").textContent = "";
  $("revealed-client-key").classList.add("hidden");
}

async function copyText(text) {
  if (!text) throw new Error("没有可复制的内容");
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Public HTTP pages cannot always use the asynchronous Clipboard API.
    }
  }
  const input = element("textarea");
  input.value = text;
  input.readOnly = true;
  input.style.position = "fixed";
  input.style.left = "-9999px";
  input.style.opacity = "0";
  document.body.append(input);
  input.focus();
  input.select();
  input.setSelectionRange(0, input.value.length);
  const copied = document.execCommand("copy");
  input.remove();
  if (!copied) throw new Error("浏览器拒绝复制");
}

function clearSensitiveUi() {
  document.querySelectorAll("input[type='password']").forEach((input) => { input.value = ""; });
  document.querySelectorAll("tbody").forEach((body) => body.replaceChildren());
  clearDisplayedClientKey();
  $("dialog-body").replaceChildren();
  $("dialog-pages").replaceChildren();
  $("dialog-pages").classList.add("hidden");
  if ($("detail-dialog").open) $("detail-dialog").close();
  for (const id of ["provider-editor", "model-editor", "provider-secret-editor", "managed-secret-editor", "mcp-server-editor", "mcp-deployment-editor", "mcp-deployment-job"]) $(id).classList.add("hidden");
  clearTimeout(state.mcpDeploymentPollTimer);
  state.mcpDeploymentPollTimer = 0;
  state.mcpDeploymentJobId = "";
  state.validatingMcpDeployment = false;
}

function logout(notify = true) {
  state.sessionEpoch += 1;
  state.controllers.forEach((controller) => controller.abort());
  state.controllers.clear();
  state.sessionToken = "";
  state.sessionExpiresAt = "";
  state.profiles = [];
  state.models = null;
  state.health = new Map();
  state.policy = null;
  state.mcpServers = [];
  state.mcpDeployments = [];
  state.managedSecrets = [];
  state.clientApiKeys = [];
  state.editingProvider = null;
  state.editingModel = null;
  state.conversationDetail = null;
  clearSensitiveUi();
  updateSessionUi();
  if (notify) showNotice("管理会话已退出");
}

async function login(event) {
  event.preventDefault();
  const password = $("admin-password").value;
  try {
    const result = await api("/admin/login", { method: "POST", body: JSON.stringify({ password }) }, false);
    state.sessionEpoch += 1;
    state.sessionToken = result.sessionToken;
    state.sessionExpiresAt = result.expiresAt;
    $("admin-password").value = "";
    updateSessionUi();
    showNotice("管理会话已建立；凭据仅保存在当前页面内存");
    await loadChannels();
  } catch (error) {
    $("admin-password").value = "";
    showError("登录失败", error);
  }
}

async function loadHealth() {
  try {
    const health = await api("/health", {}, false);
    $("health-text").textContent = `${health.runtime} · protocol ${health.protocolVersion}`;
    document.querySelector(".health").classList.add("ok");
  } catch (error) {
    $("health-text").textContent = `网关异常：${sanitizeText(error.message)}`;
  }
}

async function loadChannels() {
  if (!state.sessionToken) return;
  const [profilesResult, modelsResult, healthResult, policyResult] = await Promise.allSettled([
    api("/admin/providers"),
    api("/models", {}, false),
    api("/admin/channels/health"),
    api("/admin/runtime-policy"),
  ]);
  if (profilesResult.status === "rejected") return showError("渠道加载失败", profilesResult.reason);
  state.profiles = [...profilesResult.value].sort((a, b) => b.priority - a.priority || a.id.localeCompare(b.id));
  state.models = modelsResult.status === "fulfilled" ? modelsResult.value : null;
  state.health = new Map((healthResult.status === "fulfilled" ? healthResult.value : []).map((item) => [item.profileId, item]));
  state.policy = policyResult.status === "fulfilled" ? policyResult.value : null;
  renderChannels();
  renderRouting();
  renderModels();
  if (modelsResult.status === "rejected") showError("路由加载失败", modelsResult.reason);
  if (healthResult.status === "rejected") showError("渠道健康加载失败", healthResult.reason);
  if (policyResult.status === "rejected") showError("安全策略加载失败", policyResult.reason);
}

function channelHost(profile) {
  try { return new URL(profile.baseUrl).hostname; } catch { return "无效 URL"; }
}

function hostPolicy(profile) {
  const host = channelHost(profile);
  const allowed = state.policy && state.policy.profileAllowedHosts ? state.policy.profileAllowedHosts : [];
  if (!state.policy) return { text: host, color: "orange" };
  if (!allowed.length) return { text: `${host} · 未配置限制`, color: "orange" };
  return { text: `${host} · ${allowed.includes(host) ? "allowlisted" : "未允许"}`, color: allowed.includes(host) ? "green" : "red" };
}

function healthBadge(item) {
  const color = item.status === "healthy" ? "green" : item.status === "unhealthy" ? "red" : item.status === "disabled" ? "" : "orange";
  return badge(item.status, color);
}

function renderChannels() {
  const tbody = $("channels-body");
  $("metric-profiles").textContent = state.profiles.length;
  if (!state.profiles.length) return emptyRow(tbody, 6, "暂无 Provider 渠道");
  tbody.replaceChildren(...state.profiles.map((profile) => {
    const row = element("tr");
    const name = td();
    name.append(element("strong", "", profile.displayName), element("div", "subtle mono", `${profile.provider} · ${profile.id}`));
    const endpoint = td();
    endpoint.append(element("div", "mono endpoint", profile.baseUrl), element("div", "subtle mono", `固定 path ${profile.providerPath}`));
    const config = td();
    config.append(badge(profile.enabled ? "启用" : "停用", profile.enabled ? "green" : ""), document.createTextNode(" "), badge(profile.keyConfigured ? profile.secretMasked || "Key 已配置" : "缺少 Key", profile.keyConfigured ? "blue" : "red"), element("div", "subtle", `P${profile.priority} · ${profile.timeoutSeconds}s · ${profile.authMode}`));
    const observed = state.health.get(profile.id) || { status: profile.enabled ? (profile.keyConfigured ? "unknown" : "unconfigured") : "disabled" };
    const health = td();
    health.append(healthBadge(observed), element("div", "subtle", observed.checkedAt ? `${formatTime(observed.checkedAt)} · ${observed.latencyMs} ms` : "尚未检测"));
    if (observed.errorCode || observed.message) health.append(element("div", "subtle error-text", `${observed.errorCode || ""}${observed.message ? ` · ${observed.message}` : ""}`));
    const policy = td();
    const host = hostPolicy(profile);
    policy.append(badge(host.text, host.color), element("div", "subtle", state.policy ? "retryable 错误后切换 · 每路由 1 次" : "重试策略未加载"));
    const actions = td();
    const wrap = element("div", "row-actions");
    const test = element("button", "link-btn", "连接测试");
    test.disabled = !profile.enabled || !profile.keyConfigured;
    test.addEventListener("click", () => testConnection(profile));
    wrap.append(test);
    if (profile.source === "managed") {
      const edit = element("button", "link-btn", "编辑");
      edit.addEventListener("click", () => openProviderEditor(profile));
      wrap.append(edit);
    }
    const remove = element("button", "link-btn danger", "删除");
    remove.addEventListener("click", () => deleteProvider(profile));
    wrap.append(remove);
    actions.append(wrap);
    row.append(name, endpoint, config, health, policy, actions);
    return row;
  }));
}

function renderRouting() {
  const tbody = $("routing-body");
  const routes = state.models && state.models.routingOrder ? state.models.routingOrder : [];
  $("metric-routes").textContent = routes.length;
  $("metric-default").textContent = state.models ? state.models.defaultProvider : "—";
  $("metric-default-model").textContent = state.models && state.models.defaultModel ? state.models.defaultModel : "无可用默认模型";
  if (!routes.length) return emptyRow(tbody, 7, "暂无可用路由");
  tbody.replaceChildren(...routes.map((route) => {
    const row = element("tr");
    const profile = state.profiles.find((item) => item.id === route.profileId);
    const channel = td();
    channel.append(element("strong", "", profile ? profile.displayName : route.provider), element("div", "subtle mono", route.profileId));
    const protocol = td(); protocol.append(badge(route.protocol, route.protocol === "openai" ? "blue" : "purple"));
    const flags = td();
    if (route.isDefaultProfile) flags.append(badge("默认渠道", "blue"), document.createTextNode(" "));
    if (route.isDefaultModel) flags.append(badge("默认模型", "green"));
    row.append(td(route.position), channel, protocol, td(route.model, "mono"), td(route.profilePriority), td(route.modelPriority), flags);
    return row;
  }));
}

function defaultModel(id = "") {
  return { id, name: id, priority: 100, supportsImages: true, contextWindowTokens: 100000, maxOutputTokens: 8192, supportsNativeTools: true, supportsThinking: false, supportsStreaming: true };
}

function defaultProviderGroup(protocol) {
  return protocol === "claude" ? "claude" : "openai";
}

function populateProviderGroups(selected = "") {
  const groups = new Map(PROVIDER_GROUPS);
  for (const profile of state.profiles) {
    if (!groups.has(profile.provider)) groups.set(profile.provider, `${profile.provider}（现有分组）`);
  }
  if (selected && !groups.has(selected)) groups.set(selected, `${selected}（现有分组）`);
  const suggestions = $("provider-groups");
  suggestions.replaceChildren(...Array.from(groups, ([value, label]) => {
    const option = element("option", "", label);
    option.value = value;
    return option;
  }));
  $("provider-type").value = selected || defaultProviderGroup($("provider-protocol").value);
}

function clearProviderModelCandidates() {
  $("provider-model-options").replaceChildren();
}

function openProviderEditor(profile = null) {
  state.editingProvider = profile;
  clearProviderModelCandidates();
  $("provider-editor").classList.remove("hidden");
  $("provider-editor-title").textContent = profile ? `编辑 ${profile.id}` : "新增渠道";
  $("provider-id").value = profile ? profile.id : "";
  $("provider-id").disabled = Boolean(profile);
  $("provider-name").value = profile ? profile.displayName : "";
  $("provider-priority").value = profile ? profile.priority : 100;
  $("provider-protocol").value = profile ? profile.protocol : "openai";
  populateProviderGroups(profile ? profile.provider : "");
  $("provider-auth").value = profile ? profile.authMode : "bearer";
  $("provider-api-key").value = "";
  $("provider-api-key").required = !profile;
  $("provider-timeout").value = profile ? profile.timeoutSeconds : 120;
  $("provider-enabled").checked = profile ? profile.enabled : true;
  $("provider-url").value = profile ? profile.baseUrl : "";
  $("provider-default-model").value = profile ? profile.defaultModel : "";
  $("provider-default-model").disabled = false;
  $("provider-editor").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeProviderEditor() {
  state.editingProvider = null;
  clearProviderModelCandidates();
  $("provider-form").reset();
  $("provider-id").disabled = false;
  $("provider-default-model").disabled = false;
  $("provider-editor").classList.add("hidden");
}

async function discoverProviderModels() {
  const profileId = $("provider-id");
  const baseUrl = $("provider-url");
  const apiKey = $("provider-api-key");
  const timeout = $("provider-timeout");
  for (const input of [profileId, baseUrl, timeout]) {
    if (!input.checkValidity()) {
      input.reportValidity();
      return;
    }
  }
  apiKey.setCustomValidity("");
  if (!state.editingProvider && !apiKey.value) {
    apiKey.setCustomValidity("新建渠道获取模型前必须填写 API Key");
    apiKey.reportValidity();
    apiKey.setCustomValidity("");
    return;
  }
  if (apiKey.value && !apiKey.checkValidity()) {
    apiKey.reportValidity();
    return;
  }

  const button = $("discover-provider-models");
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "获取中…";
  clearProviderModelCandidates();
  try {
    const result = await api(`/admin/providers/${encodeURIComponent(profileId.value.trim())}/models/discover`, {
      method: "POST",
      body: JSON.stringify({
        protocol: $("provider-protocol").value,
        baseUrl: baseUrl.value.trim(),
        apiKey: apiKey.value,
        authMode: $("provider-auth").value,
        timeoutSeconds: Number(timeout.value),
      }),
    });
    const models = Array.isArray(result.models)
      ? result.models.filter((model) => model && typeof model.id === "string" && model.id.trim())
      : [];
    if (!models.length) {
      showNotice("上游未返回可用模型，请手工填写默认模型", "error");
      return;
    }
    $("provider-model-options").replaceChildren(...models.map((model) => {
      const option = element("option");
      option.value = model.id;
      option.label = model.name && model.name !== model.id ? model.name : model.id;
      return option;
    }));
    const current = $("provider-default-model").value.trim();
    if (!current) $("provider-default-model").value = models[0].id;
    const selected = $("provider-default-model").value.trim();
    const selectedFound = models.some((model) => model.id === selected);
    const suffix = result.truncated ? "；列表已截断" : "";
    showNotice(
      selectedFound
        ? `已获取 ${models.length} 个模型${suffix}`
        : `已获取 ${models.length} 个模型${suffix}；当前默认模型不在上游列表中，请确认或重新选择`,
      selectedFound ? "success" : "error",
    );
  } catch (error) {
    showError("模型获取失败", error);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

async function saveProvider(event) {
  event.preventDefault();
  const existing = state.editingProvider;
  const shouldTest = event.submitter && event.submitter.id === "save-test-provider";
  const defaultModelId = $("provider-default-model").value.trim();
  const models = existing ? [...existing.models] : [];
  if (!models.some((model) => model.id === defaultModelId)) models.push(defaultModel(defaultModelId));
  const request = {
    id: $("provider-id").value.trim(),
    provider: $("provider-type").value.trim(),
    displayName: $("provider-name").value.trim(),
    protocol: $("provider-protocol").value,
    baseUrl: $("provider-url").value.trim(),
    apiKey: $("provider-api-key").value,
    defaultModel: defaultModelId,
    models,
    authMode: $("provider-auth").value,
    timeoutSeconds: Number($("provider-timeout").value),
    priority: Number($("provider-priority").value),
    enabled: $("provider-enabled").checked,
  };
  try {
    await api(`/admin/providers/${encodeURIComponent(request.id)}`, { method: "PUT", body: JSON.stringify(request) });
  } catch (error) {
    showError("渠道保存失败", error);
    return;
  } finally {
    $("provider-api-key").value = "";
  }
  if (shouldTest) {
    try {
      const result = await api(`/admin/profiles/${encodeURIComponent(request.id)}/connection-test`, { method: "POST", body: JSON.stringify({ model: request.defaultModel }) });
      showNotice(`渠道 ${request.id} 已保存；连接状态：${result.status} · ${result.latencyMs} ms`, result.status === "healthy" ? "success" : "error");
    } catch (error) {
      showError(`渠道 ${request.id} 已保存，但连接测试失败`, error);
      closeProviderEditor();
      await loadChannels();
      return;
    }
  } else {
    showNotice(`渠道 ${request.id} 已保存`);
  }
  closeProviderEditor();
  await loadChannels();
}

async function deleteProvider(profile) {
  const sourceHint = profile.source === "environment"
    ? "删除后将持久化隐藏该环境渠道，但不会改写服务器 .env"
    : "关联的托管 Provider Secret 将同时删除";
  if (!confirm(`确定删除渠道 ${profile.id}？\n${sourceHint}`)) return;
  try {
    const result = await api(`/admin/providers/${encodeURIComponent(profile.id)}`, { method: "DELETE" });
    if (!result.success) throw new Error("渠道不存在或已被删除");
    showNotice(`渠道 ${profile.id} 已删除`);
    await loadChannels();
  } catch (error) { showError("渠道删除失败", error); }
}

async function testConnection(profile) {
  try {
    const result = await api(`/admin/profiles/${encodeURIComponent(profile.id)}/connection-test`, { method: "POST", body: JSON.stringify({ model: profile.defaultModel }) });
    state.health.set(profile.id, result);
    renderChannels();
    showNotice(`${profile.id} 连接状态：${result.status} · ${result.latencyMs} ms`, result.status === "healthy" ? "success" : "error");
  } catch (error) { showError("连接测试失败", error); }
}

function renderModels() {
  const tbody = $("models-body");
  if (!tbody) return;
  const routes = state.models && state.models.routingOrder ? state.models.routingOrder : [];
  const defaultRoute = routes.length ? routes[0] : null;
  const defaultProfile = defaultRoute ? state.profiles.find((profile) => profile.id === defaultRoute.profileId) : null;
  $("model-metric-default-provider").textContent = defaultRoute ? (defaultProfile ? defaultProfile.displayName : defaultRoute.provider) : "—";
  $("model-metric-default-profile").textContent = defaultRoute ? `${defaultRoute.profileId} · 路由 #${defaultRoute.position}` : "当前没有可用路由";
  $("model-metric-default-model").textContent = defaultRoute ? defaultRoute.model : "—";
  $("model-metric-route-count").textContent = routes.length;
  const rows = state.profiles.flatMap((profile) => (profile.models || []).map((model) => ({ profile, model })));
  if (!rows.length) return emptyRow(tbody, 7, "暂无模型");
  tbody.replaceChildren(...rows.map(({ profile, model }) => {
    const row = element("tr");
    const channel = td(); channel.append(element("strong", "", profile.displayName), element("div", "subtle mono", profile.id));
    const modelCell = td(); modelCell.append(element("strong", "mono", model.id), element("div", "subtle", model.name));
    const capabilities = td();
    if (model.supportsStreaming) capabilities.append(badge("stream", "blue"), document.createTextNode(" "));
    if (model.supportsNativeTools) capabilities.append(badge("tools", "purple"), document.createTextNode(" "));
    if (model.supportsImages) capabilities.append(badge("image", "green"), document.createTextNode(" "));
    if (model.supportsThinking) capabilities.append(badge("thinking", "orange"));
    const limits = td(); limits.append(element("div", "", `${model.contextWindowTokens} ctx`), element("div", "subtle", `${model.maxOutputTokens} max output`));
    const available = profile.enabled && profile.keyConfigured;
    const route = routes.find((item) => item.profileId === profile.id && item.model === model.id);
    const status = td(); status.append(badge(available ? "可路由" : "不可用", available ? "green" : "red"), document.createTextNode(" "));
    if (profile.defaultModel === model.id) status.append(badge("渠道默认", "blue"), document.createTextNode(" "));
    if (route && route.position === 1) status.append(badge("全局默认路由", "green"));
    else if (route) status.append(badge(`路由 #${route.position}`, ""));
    const actions = td(); const wrap = element("div", "row-actions");
    const edit = element("button", "link-btn", "编辑"); edit.addEventListener("click", () => openModelEditor(profile, model));
    wrap.append(edit);
    if (profile.defaultModel !== model.id) {
      const makeDefault = element("button", "link-btn", "设为默认"); makeDefault.addEventListener("click", () => setDefaultModel(profile, model));
      const remove = element("button", "link-btn danger", "删除"); remove.addEventListener("click", () => deleteModel(profile, model));
      wrap.append(makeDefault, remove);
    }
    actions.append(wrap);
    row.append(channel, modelCell, capabilities, limits, td(model.priority), status, actions);
    return row;
  }));
}

function fillModelProfiles(selected = "") {
  $("model-profile").replaceChildren(...state.profiles.map((profile) => {
    const option = element("option", "", `${profile.displayName} (${profile.id})`);
    option.value = profile.id;
    return option;
  }));
  if (selected) $("model-profile").value = selected;
}

function openModelEditor(profile = null, model = null) {
  state.editingModel = model ? { profileId: profile.id, modelId: model.id } : null;
  fillModelProfiles(profile ? profile.id : "");
  $("model-editor-title").textContent = model ? `编辑 ${model.id}` : "新增模型";
  $("model-profile").disabled = Boolean(model);
  $("model-id").disabled = Boolean(model);
  const value = model || defaultModel();
  $("model-id").value = value.id;
  $("model-name").value = value.name || value.id;
  $("model-priority").value = value.priority;
  $("model-context").value = value.contextWindowTokens;
  $("model-output").value = value.maxOutputTokens;
  $("model-streaming").checked = value.supportsStreaming !== false;
  $("model-tools").checked = Boolean(value.supportsNativeTools);
  $("model-images").checked = Boolean(value.supportsImages);
  $("model-thinking").checked = Boolean(value.supportsThinking);
  $("model-editor").classList.remove("hidden");
  $("model-editor").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeModelEditor() {
  state.editingModel = null;
  $("model-form").reset();
  $("model-profile").disabled = false;
  $("model-id").disabled = false;
  $("model-editor").classList.add("hidden");
}

async function saveModel(event) {
  event.preventDefault();
  const profileId = $("model-profile").value;
  const model = {
    id: $("model-id").value.trim(), name: $("model-name").value.trim(), priority: Number($("model-priority").value),
    contextWindowTokens: Number($("model-context").value), maxOutputTokens: Number($("model-output").value),
    supportsStreaming: $("model-streaming").checked, supportsNativeTools: $("model-tools").checked,
    supportsImages: $("model-images").checked, supportsThinking: $("model-thinking").checked,
  };
  const path = state.editingModel
    ? `/admin/profiles/${encodeURIComponent(profileId)}/models/${encodeURIComponent(state.editingModel.modelId)}`
    : `/admin/profiles/${encodeURIComponent(profileId)}/models`;
  try {
    await api(path, { method: state.editingModel ? "PUT" : "POST", body: JSON.stringify(model) });
    showNotice(`模型 ${model.id} 已保存`);
    closeModelEditor();
    await loadChannels();
  } catch (error) { showError("模型保存失败", error); }
}

async function setDefaultModel(profile, model) {
  try {
    await api(`/admin/profiles/${encodeURIComponent(profile.id)}/default-model`, { method: "PUT", body: JSON.stringify({ modelId: model.id }) });
    showNotice(`${profile.id} 默认模型已设为 ${model.id}`);
    await loadChannels();
  } catch (error) { showError("默认模型更新失败", error); }
}

async function deleteModel(profile, model) {
  if (!confirm(`确定删除模型 ${model.id}？`)) return;
  try {
    await api(`/admin/profiles/${encodeURIComponent(profile.id)}/models/${encodeURIComponent(model.id)}`, { method: "DELETE" });
    showNotice(`模型 ${model.id} 已删除`);
    await loadChannels();
  } catch (error) { showError("模型删除失败", error); }
}

function latestSecretAudit(items) {
  const provider = new Map();
  const managed = new Map();
  for (const item of items || []) {
    const details = item.details || {};
    if (item.action.startsWith("provider_secret_") && details.profileId && !provider.has(details.profileId)) provider.set(details.profileId, item);
    if (item.action.startsWith("secret_") && details.secretId && !managed.has(details.secretId)) managed.set(details.secretId, item);
  }
  return { provider, managed };
}

async function loadSecrets() {
  if (!state.sessionToken) return;
  const [profilesResult, secretsResult, auditResult, clientKeysResult] = await Promise.allSettled([
    api("/admin/providers"), api("/admin/secrets"), api("/admin/audit-logs?limit=500&offset=0"), api("/admin/client-api-keys"),
  ]);
  if (profilesResult.status === "fulfilled") state.profiles = profilesResult.value;
  if (secretsResult.status === "fulfilled") state.managedSecrets = secretsResult.value;
  const audit = latestSecretAudit(auditResult.status === "fulfilled" ? auditResult.value.items : []);
  renderProviderSecrets(audit.provider);
  renderManagedSecrets(audit.managed);
  if (clientKeysResult.status === "fulfilled") {
    state.clientApiKeys = clientKeysResult.value.items;
    renderClientApiKeys();
  }
  if (profilesResult.status === "rejected") showError("Provider Secret 状态加载失败", profilesResult.reason);
  if (secretsResult.status === "rejected") showError("Secret 列表加载失败", secretsResult.reason);
  if (auditResult.status === "rejected") showError("Secret 修改人加载失败", auditResult.reason);
  if (clientKeysResult.status === "rejected") showError("Client API Key 加载失败", clientKeysResult.reason);
}

function renderClientApiKeys() {
  const tbody = $("client-keys-body");
  if (!state.clientApiKeys.length) return emptyRow(tbody, 5, "暂无 Client API Key");
  tbody.replaceChildren(...state.clientApiKeys.map((item) => {
    const row = element("tr");
    const actions = td();
    const remove = element("button", "link-btn danger", "删除");
    remove.addEventListener("click", () => deleteClientApiKey(item));
    actions.append(remove);
    row.append(td(item.name), td(item.prefix, "mono"), td(formatTime(item.createdAt)), td(formatTime(item.lastUsedAt)), actions);
    return row;
  }));
}

async function loadClientApiKeys() {
  try {
    const result = await api("/admin/client-api-keys");
    state.clientApiKeys = result.items;
    renderClientApiKeys();
  } catch (error) { showError("Client API Key 加载失败", error); }
}

async function createClientApiKey(event) {
  event.preventDefault();
  const nameInput = $("client-key-name");
  try {
    const result = await api("/admin/client-api-keys", { method: "POST", body: JSON.stringify({ name: nameInput.value.trim() }) });
    $("revealed-client-key-value").textContent = result.key;
    $("revealed-client-key").classList.remove("hidden");
    nameInput.value = "";
    showNotice("Client API Key 已创建；测试阶段将在当前页面持续显示");
    await loadClientApiKeys();
  } catch (error) { showError("Client API Key 创建失败", error); }
  finally { nameInput.value = ""; }
}

async function deleteClientApiKey(item) {
  if (!confirm(`删除 Client API Key ${item.name} 后，新请求将立即返回 401，是否继续？`)) return;
  try {
    await api(`/admin/client-api-keys/${encodeURIComponent(item.id)}`, { method: "DELETE" });
    showNotice(`Client API Key ${item.name} 已删除`);
    await loadClientApiKeys();
  } catch (error) { showError("Client API Key 删除失败", error); }
}

function renderProviderSecrets(auditMap) {
  const tbody = $("provider-secrets-body");
  if (!state.profiles.length) return emptyRow(tbody, 5, "暂无 Provider");
  tbody.replaceChildren(...state.profiles.map((profile) => {
    const row = element("tr");
    const name = td(); name.append(element("strong", "", profile.displayName), element("div", "subtle mono", profile.id));
    const status = td(); status.append(badge(profile.keyConfigured ? "已配置" : "未配置", profile.keyConfigured ? "green" : "red"), document.createTextNode(" "), element("code", "mono", profile.secretMasked || "—"));
    const source = td(); source.append(badge(profile.source === "managed" ? "托管" : "环境", profile.source === "managed" ? "purple" : "orange"));
    const audit = auditMap.get(profile.id);
    const modified = td(); modified.append(element("div", "", audit ? formatTime(audit.timestamp) : "—"), element("div", "subtle", audit ? audit.actor : profile.source === "environment" ? "deployment environment" : "暂无审计"));
    const actions = td(); const wrap = element("div", "row-actions");
    if (profile.source === "managed") {
      const update = element("button", "link-btn", "更新"); update.addEventListener("click", () => openProviderSecret(profile, "update"));
      const rotate = element("button", "link-btn", "轮换"); rotate.disabled = !profile.keyConfigured; rotate.addEventListener("click", () => openProviderSecret(profile, "rotate"));
      const revoke = element("button", "link-btn danger", "撤销"); revoke.disabled = !profile.keyConfigured; revoke.addEventListener("click", () => revokeProviderSecret(profile));
      wrap.append(update, rotate, revoke);
    } else wrap.append(element("span", "subtle", "部署环境管理"));
    actions.append(wrap);
    row.append(name, status, source, modified, actions);
    return row;
  }));
}

function openProviderSecret(profile, mode) {
  $("provider-secret-profile").value = profile.id;
  $("provider-secret-mode").value = mode;
  $("provider-secret-value").value = "";
  $("provider-secret-title").textContent = `${mode === "rotate" ? "轮换" : "更新"} ${profile.id} Provider Secret`;
  $("provider-secret-editor").classList.remove("hidden");
  $("provider-secret-value").focus();
}

function closeProviderSecret() {
  $("provider-secret-value").value = "";
  $("provider-secret-editor").classList.add("hidden");
}

async function saveProviderSecret(event) {
  event.preventDefault();
  const profileId = $("provider-secret-profile").value;
  const mode = $("provider-secret-mode").value;
  const value = $("provider-secret-value").value;
  try {
    await api(`/admin/profiles/${encodeURIComponent(profileId)}/secret${mode === "rotate" ? "/rotate" : ""}`, { method: mode === "rotate" ? "POST" : "PUT", body: JSON.stringify({ value }) });
    showNotice(`${profileId} Provider Secret 已${mode === "rotate" ? "轮换" : "更新"}`);
    closeProviderSecret();
    await loadSecrets();
  } catch (error) { showError("Provider Secret 操作失败", error); }
  finally { $("provider-secret-value").value = ""; }
}

async function revokeProviderSecret(profile) {
  if (!confirm(`撤销 ${profile.id} Provider Secret 后该渠道将不可用，是否继续？`)) return;
  try {
    await api(`/admin/profiles/${encodeURIComponent(profile.id)}/secret`, { method: "DELETE" });
    showNotice(`${profile.id} Provider Secret 已撤销`);
    await loadSecrets();
  } catch (error) { showError("Provider Secret 撤销失败", error); }
}

const MCP_DEPLOYMENT_TERMINAL_STATES = new Set(["running", "failed", "rolled_back", "rollback_failed"]);

async function loadMcpDeployments() {
  if (!state.sessionToken) return;
  try {
    state.mcpDeployments = await api("/admin/mcp-deployments");
    renderMcpDeployments();
  } catch (error) {
    state.mcpDeployments = [];
    emptyRow($("mcp-deployments-body"), 6, String(error?.message || error).includes("MCP_DEPLOYMENTS_DISABLED") ? "MCP 容器部署功能尚未启用" : "部署服务不可用");
    if (!String(error?.message || error).includes("MCP_DEPLOYMENTS_DISABLED")) showError("MCP 部署加载失败", error);
  }
}

function deploymentConfigVersion(record) {
  return state.mcpServers.find((item) => item.id === record.id)?.configVersion ?? record.configVersion ?? 0;
}

function renderMcpDeployments() {
  const tbody = $("mcp-deployments-body");
  if (!state.mcpDeployments.length) return emptyRow(tbody, 6, "暂无 MCP 容器部署");
  tbody.replaceChildren(...state.mcpDeployments.map((record) => {
    const row = element("tr");
    const name = td(); name.append(element("strong", "", record.registration?.displayName || record.id), element("div", "subtle mono", record.id));
    const source = td();
    if (record.source?.type === "oci") source.append(badge("OCI", "purple"), element("div", "subtle mono endpoint", record.source.image));
    else source.append(badge("Git", "blue"), element("div", "subtle mono endpoint", `${record.source?.repository || ""}@${(record.source?.commit || "").slice(0, 12)}`));
    const status = td(); status.append(badge(record.status, record.status === "running" ? "green" : record.status === "stopped" ? "orange" : "red"), element("div", "subtle mono", `deployment v${record.deploymentVersion} · config v${deploymentConfigVersion(record)}`));
    const actions = td(); const wrap = element("div", "row-actions");
    const edit = element("button", "link-btn", "更新"); edit.addEventListener("click", () => openMcpDeployment(record));
    const toggle = element("button", "link-btn", record.status === "running" ? "停用" : "启用"); toggle.addEventListener("click", () => setDeploymentEnabled(record, record.status !== "running"));
    const logs = element("button", "link-btn", "日志"); logs.addEventListener("click", () => showDeploymentLogs(record.id));
    const rollback = element("button", "link-btn", "回滚"); rollback.disabled = !record.previousRevision; rollback.addEventListener("click", () => rollbackDeployment(record));
    const remove = element("button", "link-btn danger", "删除"); remove.addEventListener("click", () => deleteDeployment(record));
    wrap.append(edit, toggle, logs, rollback, remove); actions.append(wrap);
    row.append(name, source, td(record.manifest?.transport || "—", "mono"), status, td((record.activeRevision || "").slice(0, 16) || "—", "mono"), actions);
    return row;
  }));
}

function updateMcpDeploymentFields() {
  const isGit = $("mcp-deployment-source").value === "git";
  $("mcp-deployment-oci-fields").classList.toggle("hidden", isGit);
  $("mcp-deployment-git-fields").classList.toggle("hidden", !isGit);
  $("mcp-deployment-image").required = !isGit;
  $("mcp-deployment-repository").required = isGit;
  $("mcp-deployment-commit").required = isGit;
  $("mcp-deployment-transport").disabled = isGit;
  const isStdio = !isGit && $("mcp-deployment-transport").value === "stdio";
  $("mcp-deployment-http-fields").classList.toggle("hidden", isGit || isStdio);
  $("mcp-deployment-stdio-fields").classList.toggle("hidden", isGit || !isStdio);
  $("mcp-deployment-health-path").disabled = $("mcp-deployment-health-type").value === "tcp";
  if ($("mcp-deployment-health-type").value === "tcp") $("mcp-deployment-health-path").value = "/health";
}

function openMcpDeployment(record = null) {
  const source = record?.source || { type: "oci" };
  const manifest = record?.manifest || null;
  const registration = record?.registration || {};
  $("mcp-deployment-id").value = record?.id || "";
  $("mcp-deployment-id").disabled = Boolean(record);
  $("mcp-deployment-version").value = String(record?.deploymentVersion || 0);
  $("mcp-deployment-config-version").value = String(record ? deploymentConfigVersion(record) : 0);
  $("mcp-deployment-name").value = registration.displayName || "";
  $("mcp-deployment-source").value = source.type || "oci";
  $("mcp-deployment-source").disabled = false;
  $("mcp-deployment-enabled").checked = registration.enabled ?? true;
  $("mcp-deployment-timeout").value = String(registration.timeoutSeconds || 30);
  $("mcp-deployment-expected").value = registration.expectedServerName || "";
  $("mcp-deployment-credential").value = source.credentialSecretId || "";
  $("mcp-deployment-image").value = source.image || "";
  $("mcp-deployment-repository").value = source.repository || "";
  $("mcp-deployment-commit").value = source.commit || "";
  $("mcp-deployment-transport").value = manifest?.transport || "streamable_http";
  $("mcp-deployment-port").value = String(manifest?.http?.containerPort || 8000);
  $("mcp-deployment-mcp-path").value = manifest?.http?.mcpPath || "/mcp";
  $("mcp-deployment-health-type").value = manifest?.http?.healthCheck?.type || "http";
  $("mcp-deployment-health-path").value = manifest?.http?.healthCheck?.path || "/health";
  $("mcp-deployment-command").value = JSON.stringify(manifest?.stdio?.command || ["node", "server.js"], null, 2);
  $("mcp-deployment-cpu").value = String(manifest?.resources?.cpu || 0.5);
  $("mcp-deployment-memory").value = String(manifest?.resources?.memoryMiB || 256);
  $("mcp-deployment-pids").value = String(manifest?.resources?.pids || 128);
  $("mcp-deployment-startup-timeout").value = String(manifest?.startupTimeoutSeconds || 60);
  $("mcp-deployment-environment").value = JSON.stringify(manifest?.environment || [], null, 2);
  $("mcp-deployment-static-headers").value = JSON.stringify(registration.staticHeaders || {}, null, 2);
  $("mcp-deployment-secret-bindings").value = JSON.stringify(registration.secretBindings || [], null, 2);
  $("mcp-deployment-title").textContent = record ? `更新 MCP 部署 · ${record.id}` : "新建 MCP 部署";
  updateMcpDeploymentFields();
  $("mcp-deployment-editor").classList.remove("hidden");
}

function closeMcpDeployment() {
  $("mcp-deployment-form").reset();
  $("mcp-deployment-id").disabled = false;
  $("mcp-deployment-source").disabled = false;
  $("mcp-deployment-version").value = "0";
  $("mcp-deployment-config-version").value = "0";
  $("mcp-deployment-environment").value = "[]";
  $("mcp-deployment-static-headers").value = "{}";
  $("mcp-deployment-secret-bindings").value = "[]";
  $("mcp-deployment-editor").classList.add("hidden");
  updateMcpDeploymentFields();
}

async function saveMcpDeployment(event) {
  event.preventDefault();
  const id = $("mcp-deployment-id").value.trim();
  try {
    const sourceType = $("mcp-deployment-source").value;
    const environment = parseMcpEditorJson("mcp-deployment-environment", "[]");
    const staticHeaders = parseMcpEditorJson("mcp-deployment-static-headers", "{}");
    const secretBindings = parseMcpEditorJson("mcp-deployment-secret-bindings", "[]");
    if (!Array.isArray(environment) || !Array.isArray(secretBindings) || !staticHeaders || Array.isArray(staticHeaders) || typeof staticHeaders !== "object") throw new Error("部署 Secret/Header JSON 格式不正确");
    const credentialSecretId = $("mcp-deployment-credential").value.trim();
    const source = sourceType === "oci" ? {
      type: "oci", image: $("mcp-deployment-image").value.trim(), credentialSecretId,
    } : {
      type: "git", repository: $("mcp-deployment-repository").value.trim(), commit: $("mcp-deployment-commit").value.trim(), dockerfilePath: "Dockerfile", contextPath: ".", manifestPath: "mcp-deploy.yaml", credentialSecretId,
    };
    let manifest = null;
    if (sourceType === "oci") {
      const transport = $("mcp-deployment-transport").value;
      manifest = {
        apiVersion: "actiondesign.mcp/v1", transport, environment,
        resources: { cpu: Number($("mcp-deployment-cpu").value), memoryMiB: Number($("mcp-deployment-memory").value), pids: Number($("mcp-deployment-pids").value) },
        startupTimeoutSeconds: Number($("mcp-deployment-startup-timeout").value),
        ...(transport === "streamable_http" ? { http: { containerPort: Number($("mcp-deployment-port").value), mcpPath: $("mcp-deployment-mcp-path").value.trim(), healthCheck: { type: $("mcp-deployment-health-type").value, path: $("mcp-deployment-health-path").value.trim() || "/health" } } } : { stdio: { command: parseMcpEditorJson("mcp-deployment-command", "[]") } }),
      };
    }
    const result = await api(`/admin/mcp-deployments/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({
        id, source, manifest,
        registration: {
          displayName: $("mcp-deployment-name").value.trim(), expectedServerName: $("mcp-deployment-expected").value.trim(), timeoutSeconds: Number($("mcp-deployment-timeout").value), enabled: $("mcp-deployment-enabled").checked, staticHeaders, secretBindings,
        },
        expectedDeploymentVersion: Number($("mcp-deployment-version").value),
        expectedConfigVersion: Number($("mcp-deployment-config-version").value),
      }),
    });
    closeMcpDeployment();
    state.mcpDeploymentJobId = result.jobId;
    $("mcp-deployment-job").classList.remove("hidden");
    $("validate-mcp-deployment").classList.add("hidden");
    $("mcp-deployment-job-status").textContent = `${id} · queued · ${result.jobId}`;
    pollMcpDeploymentJob();
  } catch (error) { showError("MCP 部署启动失败", error); await Promise.allSettled([loadMcpDeployments(), loadMcpServers()]); }
}

async function pollMcpDeploymentJob() {
  clearTimeout(state.mcpDeploymentPollTimer);
  const jobId = state.mcpDeploymentJobId;
  if (!jobId || !state.sessionToken) return;
  try {
    const job = await api(`/admin/mcp-deployment-jobs/${encodeURIComponent(jobId)}`);
    $("mcp-deployment-job-status").textContent = `${job.deploymentId} · ${job.status}${job.errorCode ? ` · ${job.errorCode}` : ""} · ${jobId}`;
    const awaiting = job.status === "awaiting_browser_validation";
    $("validate-mcp-deployment").classList.toggle("hidden", !awaiting);
    if (awaiting && !state.validatingMcpDeployment) {
      state.validatingMcpDeployment = true;
      await validateCurrentMcpDeployment();
      state.validatingMcpDeployment = false;
      return;
    }
    if (MCP_DEPLOYMENT_TERMINAL_STATES.has(job.status)) {
      await Promise.allSettled([loadMcpDeployments(), loadMcpServers()]);
      return;
    }
    state.mcpDeploymentPollTimer = setTimeout(pollMcpDeploymentJob, 1500);
  } catch (error) { showError("部署任务轮询失败", error); }
}

async function sha256Json(value) {
  const canonical = JSON.stringify(value);
  if (!globalThis.crypto?.subtle) return `unavailable:${canonical.length}`;
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical));
  return `sha256:${Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

async function validateCurrentMcpDeployment() {
  const jobId = state.mcpDeploymentJobId;
  if (!jobId) return;
  $("validate-mcp-deployment").disabled = true;
  let validationPayload = { success: false, toolListHash: "" };
  try {
    const candidate = await api(`/admin/mcp-deployment-jobs/${encodeURIComponent(jobId)}/candidate-config`);
    if (candidate.transport === "stdio") {
      validationPayload = { success: true, toolListHash: "stdio-gateway-validation" };
    } else {
      const initialized = await browserMcpPost(candidate, { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "actiondesign-admin-deployer", version: "1.0.0" } } });
      await browserMcpPost(candidate, { jsonrpc: "2.0", method: "notifications/initialized" }, initialized.sessionId);
      const listed = await browserMcpPost(candidate, { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} }, initialized.sessionId);
      const tools = listed.payload?.result?.tools || [];
      validationPayload = { success: true, toolListHash: await sha256Json(tools), initializeOk: true, toolsListOk: true };
    }
    const committed = await api(`/admin/mcp-deployment-jobs/${encodeURIComponent(jobId)}/browser-validation`, { method: "POST", body: JSON.stringify(validationPayload) });
    $("mcp-deployment-job-status").textContent = `${committed.id || committed.deploymentId || "deployment"} · ${committed.status || "running"}`;
    showNotice("MCP 候选验证通过并已发布");
  } catch (error) {
    try { await api(`/admin/mcp-deployment-jobs/${encodeURIComponent(jobId)}/browser-validation`, { method: "POST", body: JSON.stringify(validationPayload) }); } catch { /* rollback is best effort and reported by polling */ }
    showError("MCP 候选验证失败，已请求恢复旧版本", error);
  } finally {
    $("validate-mcp-deployment").disabled = false;
    await Promise.allSettled([loadMcpDeployments(), loadMcpServers()]);
    state.mcpDeploymentPollTimer = setTimeout(pollMcpDeploymentJob, 1000);
  }
}

async function showDeploymentLogs(id) {
  try { openJsonDialog(`MCP 部署日志 · ${id}`, await api(`/admin/mcp-deployments/${encodeURIComponent(id)}/logs`)); }
  catch (error) { showError("MCP 部署日志加载失败", error); }
}

async function setDeploymentEnabled(record, enabled) {
  try {
    await api(`/admin/mcp-deployments/${encodeURIComponent(record.id)}/enabled`, { method: "PUT", body: JSON.stringify({ enabled, expectedDeploymentVersion: record.deploymentVersion, expectedConfigVersion: deploymentConfigVersion(record) }) });
    await Promise.allSettled([loadMcpDeployments(), loadMcpServers()]);
  } catch (error) { showError("MCP 部署状态更新失败", error); await Promise.allSettled([loadMcpDeployments(), loadMcpServers()]); }
}

async function rollbackDeployment(record) {
  if (!confirm(`确定将 ${record.id} 回滚到上一 Revision？`)) return;
  try {
    await api(`/admin/mcp-deployments/${encodeURIComponent(record.id)}/rollback`, { method: "POST", body: JSON.stringify({ expectedDeploymentVersion: record.deploymentVersion, expectedConfigVersion: deploymentConfigVersion(record) }) });
    await Promise.allSettled([loadMcpDeployments(), loadMcpServers()]);
  } catch (error) { showError("MCP 部署回滚失败", error); await Promise.allSettled([loadMcpDeployments(), loadMcpServers()]); }
}

async function deleteDeployment(record) {
  if (!confirm(`确定删除部署 ${record.id}、容器、路由和 MCP 配置？`)) return;
  try {
    await api(`/admin/mcp-deployments/${encodeURIComponent(record.id)}`, { method: "DELETE", body: JSON.stringify({ expectedDeploymentVersion: record.deploymentVersion, expectedConfigVersion: deploymentConfigVersion(record) }) });
    await Promise.allSettled([loadMcpDeployments(), loadMcpServers()]);
  } catch (error) { showError("MCP 部署删除失败", error); await Promise.allSettled([loadMcpDeployments(), loadMcpServers()]); }
}

async function loadMcpServers() {
  if (!state.sessionToken) return;
  try {
    state.mcpServers = await api("/admin/mcp-servers");
    renderMcpServers();
  } catch (error) { showError("MCP Server 加载失败", error); }
}

function renderMcpServers() {
  const tbody = $("mcp-servers-body");
  if (!state.mcpServers.length) return emptyRow(tbody, 6, "暂无 MCP Server");
  tbody.replaceChildren(...state.mcpServers.map((server) => {
    const row = element("tr");
    const name = td(); name.append(element("strong", "", server.displayName), element("div", "subtle mono", server.id));
    const source = td(); source.append(badge(server.source), element("div", "subtle mono", server.transport));
    const mode = td(); mode.append(badge(server.executionMode === "browser_direct" ? "浏览器直连" : "Gateway 代理", server.executionMode === "browser_direct" ? "green" : "orange"));
    const status = td();
    status.append(badge(server.enabled ? "已启用" : "已停用", server.enabled ? "green" : "orange"), element("div", "subtle mono", `v${server.configVersion}`));
    if (server.configStatus === "invalid") status.append(element("div", "error-text", server.errorCode));
    const actions = td(); const wrap = element("div", "row-actions");
    const edit = element("button", "link-btn", server.editable ? "编辑" : "查看");
    edit.addEventListener("click", () => server.editable ? openMcpServer(server) : openJsonDialog(`${server.id} · stdio 摘要`, server));
    const test = element("button", "link-btn", server.executionMode === "browser_direct" ? "浏览器直连测试" : "stdio 后端测试");
    test.disabled = !server.enabled || server.configStatus === "invalid";
    test.addEventListener("click", () => testMcpServer(server));
    wrap.append(edit, test);
    if (server.editable) {
      const toggle = element("button", "link-btn", server.enabled ? "停用" : "启用");
      toggle.addEventListener("click", () => setMcpServerEnabled(server, !server.enabled));
      const remove = element("button", "link-btn danger", "删除");
      remove.addEventListener("click", () => deleteMcpServer(server));
      wrap.append(toggle, remove);
    }
    actions.append(wrap);
    row.append(name, source, mode, status, td(server.streamableHttp?.url || "stdio 配置未公开", "mono"), actions);
    return row;
  }));
}

function parseMcpEditorJson(id, fallback) {
  const text = $(id).value.trim() || fallback;
  const value = JSON.parse(text);
  return value;
}

function openMcpServer(server = null) {
  $("mcp-server-id").value = server?.id || "";
  $("mcp-server-id").disabled = Boolean(server);
  $("mcp-server-name").value = server?.displayName || "";
  $("mcp-server-expected").value = server?.expectedServerName || "";
  $("mcp-server-timeout").value = String(server?.timeoutSeconds || 30);
  $("mcp-server-sse-timeout").value = String(server?.streamableHttp?.sseReadTimeoutSeconds || 300);
  $("mcp-server-enabled").checked = server?.enabled ?? true;
  $("mcp-server-version").value = String(server?.configVersion || 0);
  $("mcp-server-url").value = server?.streamableHttp?.url || "";
  $("mcp-server-static-headers").value = JSON.stringify(server?.streamableHttp?.staticHeaders || {}, null, 2);
  $("mcp-server-secret-bindings").value = JSON.stringify(server?.streamableHttp?.secretBindings || [], null, 2);
  $("mcp-server-title").textContent = server ? `编辑 HTTP MCP · ${server.id}` : "新增 HTTP MCP";
  $("mcp-server-editor").classList.remove("hidden");
}

function closeMcpServer() {
  $("mcp-server-form").reset();
  $("mcp-server-id").disabled = false;
  $("mcp-server-version").value = "0";
  $("mcp-server-static-headers").value = "{}";
  $("mcp-server-secret-bindings").value = "[]";
  $("mcp-server-editor").classList.add("hidden");
}

async function saveMcpServer(event) {
  event.preventDefault();
  const id = $("mcp-server-id").value.trim();
  try {
    const staticHeaders = parseMcpEditorJson("mcp-server-static-headers", "{}");
    const secretBindings = parseMcpEditorJson("mcp-server-secret-bindings", "[]");
    if (!staticHeaders || Array.isArray(staticHeaders) || typeof staticHeaders !== "object") throw new Error("静态 Header 必须是 JSON 对象");
    if (!Array.isArray(secretBindings)) throw new Error("敏感 Header 必须是 JSON 数组");
    await api(`/admin/mcp-servers/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({
        id,
        displayName: $("mcp-server-name").value.trim(),
        transport: "streamable_http",
        expectedConfigVersion: Number($("mcp-server-version").value),
        expectedServerName: $("mcp-server-expected").value.trim(),
        timeoutSeconds: Number($("mcp-server-timeout").value),
        enabled: $("mcp-server-enabled").checked,
        streamableHttp: {
          url: $("mcp-server-url").value.trim(),
          staticHeaders,
          secretBindings,
          sseReadTimeoutSeconds: Number($("mcp-server-sse-timeout").value),
        },
      }),
    });
    showNotice(`MCP Server ${id} 已保存`);
    closeMcpServer();
    await loadMcpServers();
  } catch (error) {
    if (String(error?.message || error).includes("MCP_CONFIG_VERSION_CONFLICT")) {
      showNotice("配置版本冲突：编辑内容已保留，列表已刷新，请核对后重新提交", "error");
      await loadMcpServers();
    } else showError("MCP Server 保存失败", error);
  }
}

async function setMcpServerEnabled(server, enabled) {
  try {
    if (server.managedBy === "deployment") {
      await api(`/admin/mcp-deployments/${encodeURIComponent(server.id)}/enabled`, {
        method: "PUT", body: JSON.stringify({ enabled, expectedConfigVersion: server.configVersion, expectedDeploymentVersion: server.deploymentVersion }),
      });
    } else {
      await api(`/admin/mcp-servers/${encodeURIComponent(server.id)}/enabled`, {
        method: "PUT", body: JSON.stringify({ enabled, expectedConfigVersion: server.configVersion }),
      });
    }
    await Promise.allSettled([loadMcpServers(), loadMcpDeployments()]);
  } catch (error) { showError("MCP Server 状态更新失败", error); await Promise.allSettled([loadMcpServers(), loadMcpDeployments()]); }
}

async function deleteMcpServer(server) {
  if (!confirm(`确定删除 MCP Server ${server.id}？`)) return;
  try {
    const path = server.managedBy === "deployment" ? `/admin/mcp-deployments/${encodeURIComponent(server.id)}` : `/admin/mcp-servers/${encodeURIComponent(server.id)}`;
    await api(path, {
      method: "DELETE", body: JSON.stringify({ expectedConfigVersion: server.configVersion, ...(server.managedBy === "deployment" ? { expectedDeploymentVersion: server.deploymentVersion } : {}) }),
    });
    await Promise.allSettled([loadMcpServers(), loadMcpDeployments()]);
  } catch (error) { showError("MCP Server 删除失败", error); await Promise.allSettled([loadMcpServers(), loadMcpDeployments()]); }
}

function parseMcpRpcResponse(text, contentType) {
  if (!contentType.includes("text/event-stream")) return JSON.parse(text);
  const data = text.split(/\r?\n/).filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trim()).find((line) => line && line !== "[DONE]");
  if (!data) throw new Error("MCP SSE 响应为空");
  return JSON.parse(data);
}

async function browserMcpPost(server, body, sessionId = "") {
  const headers = {
    ...server.streamableHttp.headers,
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
    ...(sessionId ? { "Mcp-Session-Id": sessionId } : {}),
  };
  const response = await fetch(server.streamableHttp.url, { method: "POST", headers, body: JSON.stringify(body), cache: "no-store", redirect: "error" });
  const text = await response.text();
  if (!response.ok) throw new Error(`目标 MCP HTTP ${response.status}: ${text.slice(0, 500)}`);
  return { payload: text ? parseMcpRpcResponse(text, response.headers.get("content-type") || "") : null, sessionId: response.headers.get("mcp-session-id") || sessionId };
}

async function testMcpServer(server) {
  try {
    if (server.executionMode !== "browser_direct") {
      const result = await api(`/admin/mcp-servers/${encodeURIComponent(server.id)}/connection-test`, { method: "POST" });
      return openJsonDialog(`stdio 后端连接测试 · ${server.id}`, result);
    }
    const initialized = await browserMcpPost(server, { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "actiondesign-admin-browser", version: "1.0.0" } } });
    await browserMcpPost(server, { jsonrpc: "2.0", method: "notifications/initialized" }, initialized.sessionId);
    const listed = await browserMcpPost(server, { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} }, initialized.sessionId);
    openJsonDialog(`浏览器直连测试 · ${server.id}`, { serverInfo: initialized.payload?.result?.serverInfo, tools: listed.payload?.result?.tools || [] });
  } catch (error) { showError("MCP 连接测试失败（请检查目标 CORS）", error); }
}

function renderManagedSecrets(auditMap) {
  const tbody = $("managed-secrets-body");
  if (!state.managedSecrets.length) return emptyRow(tbody, 6, "暂无 MCP / OAuth / 其他 Secret");
  tbody.replaceChildren(...state.managedSecrets.map((secret) => {
    const row = element("tr");
    const name = td(); name.append(element("strong", "", secret.displayName), element("div", "subtle mono", secret.id));
    const status = td(); status.append(badge(secret.configured ? "已配置" : "已撤销", secret.configured ? "green" : "red"));
    const audit = auditMap.get(secret.id);
    const actions = td(); const wrap = element("div", "row-actions");
    const edit = element("button", "link-btn", "更新"); edit.addEventListener("click", () => openManagedSecret(secret, "upsert"));
    const rotate = element("button", "link-btn", "轮换"); rotate.disabled = !secret.configured; rotate.addEventListener("click", () => openManagedSecret(secret, "rotate"));
    const revoke = element("button", "link-btn danger", "撤销"); revoke.disabled = !secret.configured; revoke.addEventListener("click", () => revokeManagedSecret(secret));
    wrap.append(edit, rotate, revoke); actions.append(wrap);
    const kind = td(); kind.append(element("div", "mono", secret.kind), element("div", "subtle mono", secret.exposure || "browser_header"));
    row.append(name, kind, status, td(formatTime(secret.updatedAt)), td(audit ? audit.actor : "—"), actions);
    return row;
  }));
}

function openManagedSecret(secret = null, mode = "upsert") {
  $("managed-secret-mode").value = mode;
  $("managed-secret-id").value = secret ? secret.id : "";
  $("managed-secret-kind").value = secret ? secret.kind : "mcp_key";
  $("managed-secret-exposure").value = secret?.exposure || "browser_header";
  $("managed-secret-name").value = secret ? secret.displayName : "";
  $("managed-secret-value").value = "";
  const locked = Boolean(secret);
  $("managed-secret-id").disabled = locked;
  $("managed-secret-kind").disabled = mode === "rotate";
  $("managed-secret-exposure").disabled = mode === "rotate";
  $("managed-secret-name").disabled = mode === "rotate";
  $("managed-secret-value").required = !secret || mode === "rotate";
  $("managed-secret-title").textContent = mode === "rotate" ? `轮换 ${secret.id}` : secret ? `更新 ${secret.id}` : "新增 Secret";
  $("managed-secret-editor").classList.remove("hidden");
}

function closeManagedSecret() {
  $("managed-secret-form").reset();
  $("managed-secret-value").value = "";
  $("managed-secret-id").disabled = false;
  $("managed-secret-kind").disabled = false;
  $("managed-secret-exposure").disabled = false;
  $("managed-secret-name").disabled = false;
  $("managed-secret-editor").classList.add("hidden");
}

async function saveManagedSecret(event) {
  event.preventDefault();
  const mode = $("managed-secret-mode").value;
  const id = $("managed-secret-id").value.trim();
  const value = $("managed-secret-value").value;
  try {
    if (mode === "rotate") {
      await api(`/admin/secrets/${encodeURIComponent(id)}/rotate`, { method: "POST", body: JSON.stringify({ value }) });
    } else {
      const body = { id, kind: $("managed-secret-kind").value.trim(), displayName: $("managed-secret-name").value.trim(), exposure: $("managed-secret-exposure").value };
      if (value) body.value = value;
      await api(`/admin/secrets/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) });
    }
    showNotice(`Secret ${id} 已${mode === "rotate" ? "轮换" : "保存"}`);
    closeManagedSecret();
    await loadSecrets();
  } catch (error) { showError("Secret 操作失败", error); }
  finally { $("managed-secret-value").value = ""; }
}

async function revokeManagedSecret(secret) {
  if (!confirm(`确定撤销 Secret ${secret.id}？`)) return;
  try {
    await api(`/admin/secrets/${encodeURIComponent(secret.id)}`, { method: "DELETE" });
    showNotice(`Secret ${secret.id} 已撤销`);
    await loadSecrets();
  } catch (error) { showError("Secret 撤销失败", error); }
}

function makePager(container, page, total, onChange, pageSize = PAGE_SIZE) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const prev = element("button", "secondary-btn", "上一页");
  const next = element("button", "secondary-btn", "下一页");
  prev.disabled = page <= 1; next.disabled = page >= pages;
  prev.addEventListener("click", () => onChange(page - 1)); next.addEventListener("click", () => onChange(page + 1));
  container.replaceChildren(element("span", "", `第 ${page} / ${pages} 页 · 共 ${total} 条`), prev, next);
}

async function loadAudit(page = state.auditPage) {
  if (!state.sessionToken) return;
  try {
    const result = await api(`/admin/audit-logs?limit=${PAGE_SIZE}&offset=${(page - 1) * PAGE_SIZE}`);
    state.auditPage = page;
    const tbody = $("audit-body");
    if (!result.items.length) emptyRow(tbody, 5, "暂无系统审计记录");
    else tbody.replaceChildren(...result.items.map((item) => {
      const row = element("tr");
      const status = td(); status.append(badge(item.success ? "成功" : "失败", item.success ? "green" : "red"));
      const detail = td(); const view = element("button", "link-btn", "查看脱敏详情"); view.addEventListener("click", () => openJsonDialog(`${item.action} · ${formatTime(item.timestamp)}`, item.details)); detail.append(view);
      row.append(td(formatTime(item.timestamp)), td(item.action, "mono"), td(item.actor), status, detail); return row;
    }));
    makePager($("audit-pages"), page, result.total, loadAudit);
  } catch (error) { showError("审计日志加载失败", error); }
}

function requestQuery(page) {
  const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String((page - 1) * PAGE_SIZE) });
  const fields = [["filter-conversation", "conversationId"], ["filter-request", "requestId"], ["filter-run", "runId"], ["filter-provider", "provider"], ["filter-profile", "profileId"], ["filter-model", "model"], ["filter-protocol", "protocol"], ["filter-status", "status"], ["filter-stream", "stream"], ["filter-error", "errorCode"]];
  for (const [id, key] of fields) { const value = $(id).value.trim(); if (value) query.set(key, value); }
  const startedAt = toIso($("filter-start").value); const endedAt = toIso($("filter-end").value);
  if (startedAt) query.set("startedAt", startedAt); if (endedAt) query.set("endedAt", endedAt);
  return query.toString();
}

async function loadRequests(page = state.requestPage) {
  if (!state.sessionToken) return;
  try {
    const result = await api(`/admin/request-logs?${requestQuery(page)}`);
    state.requestPage = page;
    const tbody = $("requests-body");
    if (!result.items.length) emptyRow(tbody, 7, "暂无请求日志");
    else tbody.replaceChildren(...result.items.map((item) => {
      const row = element("tr");
      const identity = td(); identity.append(element("div", "", formatTime(item.timestamp)), element("div", "subtle mono", item.requestId), element("div", "subtle mono", `${item.conversationId} · ${item.runId}`));
      const route = td(); route.append(element("strong", "", item.provider), element("div", "subtle mono", item.profileId));
      const model = td(); model.append(element("div", "mono", item.model), badge(item.stream ? "SSE" : "JSON", item.stream ? "blue" : ""));
      const status = td(); status.append(badge(item.status, item.status === "success" ? "green" : item.status === "cancelled" ? "orange" : "red")); if (item.errorCode) status.append(element("div", "subtle error-text", item.errorCode));
      const usage = td(); usage.append(element("div", "", formatUsage(item.usage)), element("div", "subtle", item.finishReason || "—"));
      const action = td(); const view = element("button", "link-btn", "metadata 详情"); view.addEventListener("click", () => showRequest(item.requestId)); action.append(view);
      row.append(identity, route, model, status, usage, td(`${item.durationMs} ms`), action); return row;
    }));
    makePager($("request-pages"), page, result.total, loadRequests);
  } catch (error) { showError("请求日志加载失败", error); }
}

function keyValueGrid(entries) {
  const grid = element("div", "detail-grid");
  for (const [key, value] of entries) grid.append(element("div", "detail-key", key), element("div", "detail-value", value === "" || value === undefined || value === null ? "—" : value));
  return grid;
}

async function showRequest(requestId) {
  try {
    const record = await api(`/admin/request-logs/${encodeURIComponent(requestId)}`);
    $("dialog-title").textContent = `请求 metadata · ${requestId}`;
    const body = $("dialog-body");
    body.replaceChildren(
      keyValueGrid([["conversationId", record.conversationId], ["runId", record.runId], ["渠道", `${record.provider} / ${record.profileId}`], ["模型", record.model], ["状态", record.status], ["模式", record.stream ? "SSE" : "非流式"], ["耗时", `${record.durationMs} ms`], ["stop reason", record.finishReason], ["usage", formatUsage(record.usage)], ["错误", `${record.errorCode || ""}${record.errorMessage ? ` · ${record.errorMessage}` : ""}`]]),
      element("h3", "section-title", `Attempts ${record.attemptCount}`),
      ...record.attempts.map((attempt) => {
        const details = element("details", "event-details");
        details.append(element("summary", "", `Attempt ${attempt.attempt} · ${attempt.provider}/${attempt.model} · ${attempt.status}`), jsonPre(attempt));
        return details;
      }),
      element("p", "subtle", "完整 request/response、tool-result 和 SSE 最终组装结果请在完整会话中查看。"),
    );
    $("dialog-pages").classList.add("hidden");
    $("detail-dialog").showModal();
  } catch (error) { showError("请求详情加载失败", error); }
}

function conversationQuery(page) {
  const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String((page - 1) * PAGE_SIZE) });
  const fields = [["conversation-id-filter", "conversationId"], ["conversation-request-filter", "requestId"], ["conversation-run-filter", "runId"], ["conversation-provider-filter", "provider"], ["conversation-model-filter", "model"], ["conversation-status-filter", "status"]];
  for (const [id, key] of fields) { const value = $(id).value.trim(); if (value) query.set(key, value); }
  if ($("conversation-incomplete-filter").checked) query.set("incompleteOnly", "true");
  const from = toIso($("conversation-start-filter").value); const to = toIso($("conversation-end-filter").value);
  if (from) query.set("updatedFrom", from); if (to) query.set("updatedTo", to);
  return query.toString();
}

async function loadConversations(page = state.conversationPage) {
  if (!state.sessionToken) return;
  try {
    const result = await api(`/admin/conversations?${conversationQuery(page)}`);
    state.conversationPage = page;
    const tbody = $("conversations-body");
    if (!result.items.length) emptyRow(tbody, 7, "暂无 Conversation 日志");
    else tbody.replaceChildren(...result.items.map((item) => {
      const row = element("tr");
      const id = td(); id.append(element("code", "mono", item.conversationId)); if (item.incompleteReason) id.append(element("div", "subtle error-text", item.incompleteReason));
      const status = td(); status.append(badge(item.status, item.status === "completed" ? "green" : item.status === "aborted" ? "orange" : "blue"));
      const actions = td(); const wrap = element("div", "row-actions");
      const view = element("button", "link-btn", "查看事件"); view.addEventListener("click", () => openConversation(item)); wrap.append(view);
      if (item.status === "open") {
        const complete = element("button", "link-btn", "归档为完成"); complete.addEventListener("click", () => sealConversation(item, "completed"));
        const abort = element("button", "link-btn danger", "归档为中止"); abort.addEventListener("click", () => sealConversation(item, "aborted")); wrap.append(complete, abort);
      }
      actions.append(wrap);
      row.append(id, td(formatTime(item.startedAt)), td(formatTime(item.updatedAt)), status, td(item.eventCount), td(formatBytes(item.sizeBytes)), actions); return row;
    }));
    makePager($("conversation-pages"), page, result.total, loadConversations);
  } catch (error) { showError("Conversation 加载失败", error); }
}

function eventPayload(event) {
  if (event.type === "gateway_request") return event.request;
  if (event.type === "gateway_response") return event.response;
  if (event.type === "gateway_error") return event.error;
  if (event.type === "trace") return event.trace;
  return event;
}

function renderConversationEvent(event, index, absoluteIndex) {
  const card = element("article", "event-card");
  const head = element("div", "event-card-head");
  const type = event.type || event.event || "event";
  head.append(element("strong", "", `${absoluteIndex + 1}. ${type}`), element("span", "subtle", formatTime(event.time || event.timestamp)));
  card.append(head);
  card.append(keyValueGrid([["requestId", event.requestId], ["runId", event.runId], ["traceId", event.traceId || (event.trace && event.trace.traceId)], ["attempt", event.attempt], ["turnStatus", event.turnStatus], ["provider", event.provider], ["profileId", event.profileId], ["tool", event.toolName || event.toolCallId]]));
  const payload = eventPayload(event);
  const usage = payload && payload.usage;
  const stopReason = payload && (payload.finish_reason || payload.stop_reason || (usage && (usage.finish_reason || usage.stop_reason)));
  if (usage || stopReason || (payload && payload.tool_calls)) card.append(keyValueGrid([["usage", formatUsage(usage)], ["stop reason", stopReason], ["tool calls", payload && Array.isArray(payload.tool_calls) ? payload.tool_calls.map((item) => item.name || item.id || "tool").join(", ") : "—"]]));
  if (type === "gateway_response") card.append(element("div", "event-label", "Response / SSE 最终组装结果"));
  if (type === "tool_result") card.append(element("div", "event-label", "Tool result 审计"));
  if (type === "trace") card.append(element("div", "event-label", "脱敏 trace"));
  const details = element("details", "event-details");
  details.append(element("summary", "", `查看脱敏 JSON · page item ${index + 1}`), jsonPre(event));
  card.append(details);
  return card;
}

function openConversation(summary) {
  state.conversationDetail = { id: summary.conversationId, page: 1 };
  $("dialog-title").textContent = `完整会话 · ${summary.conversationId}`;
  $("detail-dialog").showModal();
  loadConversationDetail(1);
}

async function loadConversationDetail(page) {
  if (!state.conversationDetail) return;
  const id = state.conversationDetail.id;
  try {
    const result = await api(`/admin/conversations/${encodeURIComponent(id)}?limit=${EVENT_PAGE_SIZE}&offset=${(page - 1) * EVENT_PAGE_SIZE}`);
    state.conversationDetail.page = page;
    const body = $("dialog-body");
    const start = (page - 1) * EVENT_PAGE_SIZE;
    body.replaceChildren(...result.items.map((event, index) => renderConversationEvent(event, index, start + index)));
    if (!result.items.length) body.append(element("p", "empty", "暂无会话事件"));
    $("dialog-pages").classList.remove("hidden");
    makePager($("dialog-pages"), page, result.total, loadConversationDetail, EVENT_PAGE_SIZE);
  } catch (error) {
    $("dialog-body").replaceChildren(element("p", "empty error-text", `本页加载失败：${sanitizeText(error.message)}`));
    $("dialog-pages").classList.add("hidden");
  }
}

async function sealConversation(summary, status) {
  if (!confirm(`仅归档日志状态：将 ${summary.conversationId} 标记为 ${status}？`)) return;
  try {
    await api(`/admin/conversations/${encodeURIComponent(summary.conversationId)}/seal`, { method: "POST", body: JSON.stringify({ status }) });
    showNotice(`Conversation 日志已归档为 ${status}`);
    await loadConversations();
  } catch (error) { showError("Conversation 归档失败", error); }
}

function jsonPre(value) {
  const pre = element("pre", "json-view");
  pre.textContent = JSON.stringify(redact(value), null, 2);
  return pre;
}
function openJsonDialog(title, value) {
  $("dialog-title").textContent = title;
  $("dialog-body").replaceChildren(jsonPre(value));
  $("dialog-pages").classList.add("hidden");
  $("detail-dialog").showModal();
}

function switchView(name) {
  const titles = {
    channels: ["渠道", "配置 Provider、固定请求路径、路由优先级和连接健康"],
    models: ["模型", "管理模型能力、参数限制、默认模型和可用状态"],
    secrets: ["Secret", "管理 Provider、MCP、Client API Key 和其他 Secret"],
    requests: ["请求日志", "按标识、渠道、模型、状态和时间筛选脱敏 metadata"],
    conversations: ["完整会话", "分页查看 request/response、tool-result、SSE 最终结果和脱敏 trace"],
    audit: ["系统审计", "查看管理员登录及配置和 Secret 生命周期操作"],
  };
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  $("view-title").textContent = titles[name][0]; $("view-subtitle").textContent = titles[name][1];
  if (!state.sessionToken) return;
  if (name === "channels") loadChannels();
  if (name === "models") state.profiles.length ? renderModels() : loadChannels();
  if (name === "secrets") { loadSecrets(); loadMcpServers(); loadMcpDeployments(); }
  if (name === "requests") loadRequests();
  if (name === "conversations") loadConversations();
  if (name === "audit") loadAudit();
}

document.addEventListener("DOMContentLoaded", () => {
  updateSessionUi(); loadHealth();
  $("login-form").addEventListener("submit", login); $("logout-btn").addEventListener("click", () => logout());
  $("refresh-channels").addEventListener("click", loadChannels); $("new-provider").addEventListener("click", () => openProviderEditor()); $("cancel-provider").addEventListener("click", closeProviderEditor); $("reset-provider").addEventListener("click", () => openProviderEditor(state.editingProvider)); $("provider-form").addEventListener("submit", saveProvider); $("discover-provider-models").addEventListener("click", discoverProviderModels); $("provider-protocol").addEventListener("change", () => { clearProviderModelCandidates(); if (!state.editingProvider) populateProviderGroups(defaultProviderGroup($("provider-protocol").value)); });
  for (const id of ["provider-id", "provider-url", "provider-api-key"]) $(id).addEventListener("input", clearProviderModelCandidates);
  $("provider-auth").addEventListener("change", clearProviderModelCandidates);
  $("refresh-models").addEventListener("click", loadChannels); $("new-model").addEventListener("click", () => openModelEditor()); $("cancel-model").addEventListener("click", closeModelEditor); $("reset-model").addEventListener("click", () => state.editingModel ? openModelEditor(state.profiles.find((item) => item.id === state.editingModel.profileId), state.profiles.find((item) => item.id === state.editingModel.profileId).models.find((item) => item.id === state.editingModel.modelId)) : openModelEditor()); $("model-form").addEventListener("submit", saveModel);
  $("refresh-secrets").addEventListener("click", loadSecrets); $("provider-secret-form").addEventListener("submit", saveProviderSecret); $("cancel-provider-secret").addEventListener("click", closeProviderSecret); $("new-secret").addEventListener("click", () => openManagedSecret()); $("managed-secret-form").addEventListener("submit", saveManagedSecret); $("cancel-managed-secret").addEventListener("click", closeManagedSecret);
  $("refresh-mcp-servers").addEventListener("click", loadMcpServers); $("new-mcp-server").addEventListener("click", () => openMcpServer()); $("mcp-server-form").addEventListener("submit", saveMcpServer); $("cancel-mcp-server").addEventListener("click", closeMcpServer);
  $("refresh-mcp-deployments").addEventListener("click", () => Promise.allSettled([loadMcpDeployments(), loadMcpServers()])); $("new-mcp-deployment").addEventListener("click", () => openMcpDeployment()); $("mcp-deployment-form").addEventListener("submit", saveMcpDeployment); $("cancel-mcp-deployment").addEventListener("click", closeMcpDeployment); $("mcp-deployment-source").addEventListener("change", updateMcpDeploymentFields); $("mcp-deployment-transport").addEventListener("change", updateMcpDeploymentFields); $("mcp-deployment-health-type").addEventListener("change", updateMcpDeploymentFields); $("validate-mcp-deployment").addEventListener("click", async () => { if (state.validatingMcpDeployment) return; state.validatingMcpDeployment = true; try { await validateCurrentMcpDeployment(); } finally { state.validatingMcpDeployment = false; } }); $("close-mcp-deployment-job").addEventListener("click", () => { clearTimeout(state.mcpDeploymentPollTimer); state.mcpDeploymentPollTimer = 0; state.mcpDeploymentJobId = ""; $("mcp-deployment-job").classList.add("hidden"); });
  $("create-client-key-form").addEventListener("submit", createClientApiKey); $("refresh-client-keys").addEventListener("click", loadClientApiKeys); $("clear-client-key").addEventListener("click", clearDisplayedClientKey);
  $("copy-client-key").addEventListener("click", async () => { try { await copyText($("revealed-client-key-value").textContent); showNotice("Client API Key 已复制"); } catch { showNotice("复制失败，请手动复制", "error"); } });
  $("refresh-audit").addEventListener("click", () => loadAudit()); $("refresh-conversations").addEventListener("click", () => loadConversations()); $("refresh-requests").addEventListener("click", () => loadRequests());
  $("request-filters").addEventListener("submit", (event) => { event.preventDefault(); state.requestPage = 1; loadRequests(1); });
  $("conversation-filters").addEventListener("submit", (event) => { event.preventDefault(); state.conversationPage = 1; loadConversations(1); });
  $("dialog-close").addEventListener("click", () => { state.conversationDetail = null; $("dialog-body").replaceChildren(); $("detail-dialog").close(); });
  document.querySelectorAll(".nav-item").forEach((item) => item.addEventListener("click", () => switchView(item.dataset.view)));
  window.addEventListener("pagehide", clearSensitiveUi);
});

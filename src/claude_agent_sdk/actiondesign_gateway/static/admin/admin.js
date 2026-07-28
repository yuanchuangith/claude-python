"use strict";

const API = "/api/actiondesign-agent";
const PAGE_SIZE = 20;
const state = {
  sessionToken: "",
  sessionExpiresAt: "",
  profiles: [],
  models: null,
  editingProfile: null,
  auditPage: 1,
  conversationPage: 1,
  requestPage: 1,
};

const $ = (id) => document.getElementById(id);
const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
};

function showNotice(message, kind = "success") {
  const notice = $("notice");
  notice.textContent = message;
  notice.className = `notice ${kind}`;
  clearTimeout(showNotice.timer);
  showNotice.timer = setTimeout(() => notice.classList.add("hidden"), 4500);
}

function errorMessage(payload, response) {
  const detail = payload && payload.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") return detail.message || detail.code || JSON.stringify(detail);
  return (payload && (payload.message || payload.error)) || `${response.status} ${response.statusText}`;
}

async function api(path, options = {}, admin = true) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (admin) {
    if (!state.sessionToken) throw new Error("请先登录管理会话");
    headers.set("Authorization", `Bearer ${state.sessionToken}`);
  }
  const response = await fetch(`${API}${path}`, { ...options, headers });
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) {
    if (response.status === 401 && admin) logout(false);
    throw new Error(errorMessage(payload, response));
  }
  return payload;
}

function redact(value, depth = 0) {
  if (depth > 10) return "[TRUNCATED]";
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
  if (typeof value === "string" && /^(Bearer|Basic)\s+\S+/i.test(value)) return "[REDACTED]";
  return value;
}

function badge(text, color = "") {
  return element("span", `badge ${color}`.trim(), text);
}

function td(text, className = "") {
  return element("td", className, text);
}

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
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function updateSessionUi() {
  const loggedIn = Boolean(state.sessionToken);
  $("login-panel").classList.toggle("hidden", loggedIn);
  $("logout-btn").classList.toggle("hidden", !loggedIn);
  $("session-state").textContent = loggedIn
    ? `管理会话已登录${state.sessionExpiresAt ? ` · ${formatTime(state.sessionExpiresAt)} 到期` : ""}`
    : "未登录";
}

function logout(notify = true) {
  state.sessionToken = "";
  state.sessionExpiresAt = "";
  state.profiles = [];
  state.models = null;
  $("admin-password").value = "";
  $("custom-access-key").value = "";
  $("revealed-key-value").textContent = "";
  $("revealed-key").classList.add("hidden");
  updateSessionUi();
  if (notify) showNotice("管理会话已退出");
}

async function login(event) {
  event.preventDefault();
  const password = $("admin-password").value;
  try {
    const result = await api("/admin/login", { method: "POST", body: JSON.stringify({ password }) }, false);
    state.sessionToken = result.sessionToken;
    state.sessionExpiresAt = result.expiresAt;
    $("admin-password").value = "";
    updateSessionUi();
    showNotice("管理会话已建立；凭据仅保存在当前页面内存");
    await Promise.allSettled([loadChannels(), loadAccessKeyStatus()]);
  } catch (error) {
    showNotice(`登录失败：${error.message}`, "error");
  }
}

async function loadHealth() {
  try {
    const health = await api("/health", {}, false);
    $("health-text").textContent = `${health.runtime} · protocol ${health.protocolVersion}`;
    document.querySelector(".health").classList.add("ok");
  } catch (error) {
    $("health-text").textContent = `网关异常：${error.message}`;
  }
}

async function loadChannels() {
  if (!state.sessionToken) return;
  try {
    const [profiles, models] = await Promise.all([api("/admin/profiles"), api("/models", {}, false)]);
    state.profiles = [...profiles].sort((a, b) => b.priority - a.priority);
    state.models = models;
    renderProfiles();
    renderRouting();
  } catch (error) {
    showNotice(`渠道加载失败：${error.message}`, "error");
  }
}

function renderRouting() {
  const tbody = $("routing-body");
  const routes = state.models && state.models.routingOrder ? state.models.routingOrder : [];
  $("metric-profiles").textContent = state.profiles.length;
  $("metric-routes").textContent = routes.length;
  $("metric-default").textContent = state.models ? state.models.defaultProvider : "—";
  $("metric-default-model").textContent = state.models && state.models.defaultModel ? state.models.defaultModel : "无可用默认模型";
  if (!routes.length) return emptyRow(tbody, 7, "暂无可用路由");
  tbody.replaceChildren(...routes.map((route) => {
    const row = element("tr");
    const profile = state.profiles.find((item) => item.id === route.profileId);
    const channelCell = td();
    channelCell.append(element("strong", "", profile ? profile.displayName : route.provider), element("div", "subtle mono", route.profileId));
    const protocolCell = td();
    protocolCell.append(badge(route.protocol === "openai" ? "OpenAI" : "Claude", route.protocol === "openai" ? "blue" : "purple"));
    const flags = td();
    if (route.isDefaultProfile) flags.append(badge("默认渠道", "blue"));
    if (route.isDefaultModel) flags.append(badge("默认模型", "green"));
    row.append(td(route.position), channelCell, protocolCell, td(route.model, "mono"), td(route.profilePriority), td(route.modelPriority), flags);
    return row;
  }));
}

function renderProfiles() {
  const tbody = $("profiles-body");
  if (!state.profiles.length) return emptyRow(tbody, 8, "暂无渠道 Profile");
  tbody.replaceChildren(...state.profiles.map((profile) => {
    const row = element("tr");
    const nameCell = td();
    nameCell.append(element("strong", "", profile.displayName), element("div", "subtle", profile.provider));
    const protocolCell = td();
    protocolCell.append(badge(profile.protocol === "openai" ? "OpenAI" : "Claude", profile.protocol === "openai" ? "blue" : "purple"));
    const statusCell = td();
    statusCell.append(badge(profile.enabled ? "启用" : "停用", profile.enabled ? "green" : ""), document.createTextNode(" "), badge(profile.keyConfigured ? "Key 已配置" : "缺少 Key", profile.keyConfigured ? "blue" : "red"));
    const sourceCell = td();
    sourceCell.append(badge(profile.source === "environment" ? "环境" : "托管", profile.source === "environment" ? "orange" : "purple"));
    const actions = td();
    const wrap = element("div", "row-actions");
    const edit = element("button", "link-btn", "编辑");
    edit.type = "button";
    edit.addEventListener("click", () => openProfileEditor(profile));
    wrap.append(edit);
    if (profile.source === "managed") {
      const remove = element("button", "link-btn danger", "删除");
      remove.type = "button";
      remove.addEventListener("click", () => deleteProfile(profile));
      wrap.append(remove);
    }
    actions.append(wrap);
    row.append(nameCell, td(profile.id, "mono"), protocolCell, td(profile.priority), td(profile.defaultModel, "mono"), statusCell, sourceCell, actions);
    return row;
  }));
}

function defaultModel() {
  return { id: "", name: "", priority: 100, supportsImages: true, contextWindowTokens: 100000, maxOutputTokens: 8192, supportsNativeTools: true, supportsThinking: false };
}

function addModelRow(model = defaultModel(), environment = false) {
  const row = element("div", "model-row");
  const field = (labelText, type, name, value, attrs = {}) => {
    const label = element("label", attrs.className || "", labelText);
    const input = element("input");
    input.type = type;
    input.dataset.field = name;
    if (type === "checkbox") input.checked = Boolean(value);
    else input.value = value ?? "";
    for (const [key, attrValue] of Object.entries(attrs)) if (key !== "className") input[key] = attrValue;
    label.append(input);
    return label;
  };
  const id = field("模型 ID", "text", "id", model.id, { required: true, maxLength: 256, disabled: environment });
  id.querySelector("input").addEventListener("input", updateDefaultModelOptions);
  const name = field("显示名称", "text", "name", model.name || model.id, { required: true, maxLength: 256, disabled: environment });
  const priority = field("优先级", "number", "priority", model.priority || 100, { required: true, min: 1, max: 10000 });
  const context = field("上下文 Token", "number", "contextWindowTokens", model.contextWindowTokens || 100000, { required: true, min: 1024, max: 10000000 });
  const output = field("输出 Token", "number", "maxOutputTokens", model.maxOutputTokens || 8192, { required: true, min: 256, max: 1000000 });
  const images = field("图片", "checkbox", "supportsImages", model.supportsImages, { className: "check" });
  const tools = field("原生工具", "checkbox", "supportsNativeTools", model.supportsNativeTools, { className: "check" });
  const thinking = field("Thinking", "checkbox", "supportsThinking", model.supportsThinking, { className: "check" });
  const remove = element("button", "model-remove", "×");
  remove.type = "button";
  remove.disabled = environment;
  remove.title = environment ? "环境模型不能删除" : "删除模型";
  remove.addEventListener("click", () => {
    if ($("models-editor").children.length <= 1) return showNotice("至少保留一个模型", "error");
    row.remove();
    updateDefaultModelOptions();
  });
  row.append(id, name, priority, context, output, images, tools, thinking, remove);
  $("models-editor").append(row);
  updateDefaultModelOptions();
}

function updateDefaultModelOptions() {
  const select = $("profile-default-model");
  const previous = select.value;
  const values = [...document.querySelectorAll("#models-editor [data-field='id']")].map((input) => input.value.trim()).filter(Boolean);
  select.replaceChildren(...values.map((value) => {
    const option = element("option", "", value);
    option.value = value;
    return option;
  }));
  if (values.includes(previous)) select.value = previous;
}

function setEnvironmentLock(environment) {
  for (const id of ["profile-provider", "profile-name", "profile-protocol", "profile-auth", "profile-timeout", "profile-enabled", "profile-url", "profile-key"]) $(id).disabled = environment;
  $("add-model").disabled = environment;
  $("profile-editor-note").textContent = environment
    ? "环境渠道只允许修改优先级、默认模型和模型优先级/能力；连接与认证配置由部署环境管理。"
    : "Provider Key 只提交后端，保存后不回显。";
}

function openProfileEditor(profile = null) {
  state.editingProfile = profile;
  const environment = profile && profile.source === "environment";
  $("profile-editor").classList.remove("hidden");
  $("profile-editor-title").textContent = profile ? `编辑 ${profile.id}` : "新增渠道";
  $("profile-source").value = profile ? profile.source : "managed";
  $("profile-id").value = profile ? profile.id : "";
  $("profile-id").disabled = Boolean(profile);
  $("profile-provider").value = profile ? profile.provider : "";
  $("profile-name").value = profile ? profile.displayName : "";
  $("profile-priority").value = profile ? profile.priority : 100;
  $("profile-protocol").value = profile ? profile.protocol : "openai";
  $("profile-auth").value = profile ? profile.authMode : "bearer";
  $("profile-timeout").value = profile ? profile.timeoutSeconds : 120;
  $("profile-enabled").checked = profile ? profile.enabled : true;
  $("profile-url").value = profile ? profile.baseUrl : "";
  $("profile-key").value = "";
  $("models-editor").replaceChildren();
  for (const model of profile ? profile.models : [defaultModel()]) addModelRow(model, environment);
  setEnvironmentLock(environment);
  updateDefaultModelOptions();
  $("profile-default-model").value = profile ? profile.defaultModel : "";
  $("profile-editor").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeProfileEditor() {
  state.editingProfile = null;
  $("profile-form").reset();
  $("profile-key").value = "";
  $("models-editor").replaceChildren();
  $("profile-editor").classList.add("hidden");
}

function readModels() {
  return [...document.querySelectorAll("#models-editor .model-row")].map((row) => {
    const value = (name) => row.querySelector(`[data-field='${name}']`);
    return {
      id: value("id").value.trim(),
      name: value("name").value.trim() || value("id").value.trim(),
      priority: Number(value("priority").value),
      contextWindowTokens: Number(value("contextWindowTokens").value),
      maxOutputTokens: Number(value("maxOutputTokens").value),
      supportsImages: value("supportsImages").checked,
      supportsNativeTools: value("supportsNativeTools").checked,
      supportsThinking: value("supportsThinking").checked,
    };
  });
}

async function saveProfile(event) {
  event.preventDefault();
  const models = readModels();
  if (!models.length || models.some((model) => !model.id)) return showNotice("请填写完整模型 ID", "error");
  const request = {
    id: $("profile-id").value.trim(),
    provider: $("profile-provider").value.trim(),
    displayName: $("profile-name").value.trim(),
    priority: Number($("profile-priority").value),
    protocol: $("profile-protocol").value,
    baseUrl: $("profile-url").value.trim(),
    apiKey: $("profile-key").value,
    defaultModel: $("profile-default-model").value,
    models,
    authMode: $("profile-auth").value,
    timeoutSeconds: Number($("profile-timeout").value),
    enabled: $("profile-enabled").checked,
  };
  try {
    await api(`/admin/profiles/${encodeURIComponent(request.id)}`, { method: "PUT", body: JSON.stringify(request) });
    $("profile-key").value = "";
    showNotice(`Profile ${request.id} 已保存`);
    closeProfileEditor();
    await loadChannels();
  } catch (error) {
    showNotice(`保存失败：${error.message}`, "error");
  }
}

async function deleteProfile(profile) {
  if (!confirm(`确定删除托管 Profile ${profile.id}？`)) return;
  try {
    await api(`/admin/profiles/${encodeURIComponent(profile.id)}`, { method: "DELETE" });
    showNotice(`Profile ${profile.id} 已删除`);
    await loadChannels();
  } catch (error) {
    showNotice(`删除失败：${error.message}`, "error");
  }
}

async function loadAccessKeyStatus() {
  if (!state.sessionToken) return;
  try {
    const status = await api("/admin/access-key");
    const container = $("key-status");
    container.replaceChildren(badge(status.configured ? "已配置" : "未配置", status.configured ? "green" : "orange"), element("code", "mono", status.masked || "—"));
  } catch (error) {
    showNotice(`访问 Key 状态加载失败：${error.message}`, "error");
  }
}

async function rotateAccessKey(event) {
  event.preventDefault();
  if (!confirm("轮换后旧 Browser API Key 将立即失效，是否继续？")) return;
  const value = $("custom-access-key").value.trim();
  try {
    const result = await api("/admin/access-key/rotate", { method: "POST", body: JSON.stringify(value ? { apiKey: value } : {}) });
    $("custom-access-key").value = "";
    $("revealed-key-value").textContent = result.apiKey;
    $("revealed-key").classList.remove("hidden");
    showNotice("访问 Key 已轮换；明文只显示这一次");
    await loadAccessKeyStatus();
  } catch (error) {
    showNotice(`访问 Key 轮换失败：${error.message}`, "error");
  }
}

function makePager(container, page, total, onChange) {
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const prev = element("button", "secondary-btn", "上一页");
  const next = element("button", "secondary-btn", "下一页");
  prev.disabled = page <= 1;
  next.disabled = page >= pages;
  prev.addEventListener("click", () => onChange(page - 1));
  next.addEventListener("click", () => onChange(page + 1));
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
      const resultCell = td(); resultCell.append(badge(item.success ? "成功" : "失败", item.success ? "green" : "red"));
      const detailCell = td();
      const details = element("button", "link-btn", "查看脱敏详情");
      details.addEventListener("click", () => openJsonDialog(`${item.action} · ${formatTime(item.timestamp)}`, item.details));
      detailCell.append(details);
      row.append(td(formatTime(item.timestamp)), td(item.action, "mono"), td(item.actor), resultCell, detailCell);
      return row;
    }));
    makePager($("audit-pages"), page, result.total, loadAudit);
  } catch (error) { showNotice(`审计日志加载失败：${error.message}`, "error"); }
}

async function loadConversations(page = state.conversationPage) {
  if (!state.sessionToken) return;
  try {
    const result = await api(`/admin/conversations?limit=${PAGE_SIZE}&offset=${(page - 1) * PAGE_SIZE}`);
    state.conversationPage = page;
    const tbody = $("conversations-body");
    if (!result.items.length) emptyRow(tbody, 7, "暂无 Conversation 日志");
    else tbody.replaceChildren(...result.items.map((item) => {
      const row = element("tr");
      const idCell = td(); idCell.append(element("code", "mono", item.conversationId), item.incompleteReason ? element("div", "subtle", item.incompleteReason) : document.createTextNode(""));
      const statusCell = td(); statusCell.append(badge(item.status, item.status === "completed" ? "green" : item.status === "aborted" ? "orange" : "blue"));
      const actions = td(); const wrap = element("div", "row-actions");
      const view = element("button", "link-btn", "展开轮次"); view.addEventListener("click", () => showConversation(item)); wrap.append(view);
      if (item.status === "open") {
        const complete = element("button", "link-btn", "结束"); complete.addEventListener("click", () => sealConversation(item, "completed"));
        const abort = element("button", "link-btn danger", "中止"); abort.addEventListener("click", () => sealConversation(item, "aborted"));
        wrap.append(complete, abort);
      }
      actions.append(wrap);
      row.append(idCell, td(formatTime(item.startedAt)), td(formatTime(item.updatedAt)), statusCell, td(item.eventCount), td(formatBytes(item.sizeBytes)), actions);
      return row;
    }));
    makePager($("conversation-pages"), page, result.total, loadConversations);
  } catch (error) { showNotice(`Conversation 加载失败：${error.message}`, "error"); }
}

async function showConversation(summary) {
  try {
    const result = await api(`/admin/conversations/${encodeURIComponent(summary.conversationId)}?limit=500&offset=0`);
    $("dialog-title").textContent = `完整对话 · ${summary.conversationId}`;
    const body = $("dialog-body");
    body.replaceChildren(...result.items.map((item, index) => {
      const details = element("details", "event-details");
      const type = item.type || item.event || item.status || "event";
      details.append(element("summary", "", `事件 ${index + 1} · ${type}`), jsonPre(item));
      return details;
    }));
    if (!result.items.length) body.append(element("p", "empty", "暂无会话事件"));
    $("detail-dialog").showModal();
  } catch (error) { showNotice(`Conversation 详情加载失败：${error.message}`, "error"); }
}

async function sealConversation(summary, status) {
  if (!confirm(`确定将 ${summary.conversationId} 标记为 ${status}？`)) return;
  try {
    await api(`/admin/conversations/${encodeURIComponent(summary.conversationId)}/seal`, { method: "POST", body: JSON.stringify({ status }) });
    showNotice(`Conversation 已标记为 ${status}`);
    await loadConversations();
  } catch (error) { showNotice(`Conversation 状态更新失败：${error.message}`, "error"); }
}

function requestQuery(page) {
  const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String((page - 1) * PAGE_SIZE) });
  for (const [id, key] of [["filter-provider", "provider"], ["filter-profile", "profileId"], ["filter-protocol", "protocol"], ["filter-status", "status"]]) {
    const value = $(id).value.trim(); if (value) query.set(key, value);
  }
  return query.toString();
}

async function loadRequests(page = state.requestPage) {
  if (!state.sessionToken) return;
  try {
    const result = await api(`/admin/request-logs?${requestQuery(page)}`);
    state.requestPage = page;
    const tbody = $("requests-body");
    if (!result.items.length) emptyRow(tbody, 8, "暂无请求日志");
    else tbody.replaceChildren(...result.items.map((item) => {
      const row = element("tr");
      const route = td(); route.append(element("strong", "", item.provider), element("div", "subtle mono", item.profileId));
      const protocol = td(); protocol.append(badge(item.protocol, item.protocol === "openai" ? "blue" : "purple"));
      const status = td(); status.append(badge(item.status, item.status === "success" ? "green" : item.status === "cancelled" ? "orange" : "red"));
      const action = td(); const view = element("button", "link-btn", "详情"); view.addEventListener("click", () => showRequest(item.requestId)); action.append(view);
      row.append(td(formatTime(item.timestamp)), route, protocol, td(item.model, "mono"), td(item.attempt || 1), status, td(`${item.durationMs} ms`), action);
      return row;
    }));
    makePager($("request-pages"), page, result.total, loadRequests);
  } catch (error) { showNotice(`请求日志加载失败：${error.message}`, "error"); }
}

async function showRequest(requestId) {
  try {
    const record = await api(`/admin/request-logs/${encodeURIComponent(requestId)}`);
    openJsonDialog(`请求详情 · ${requestId}`, record);
  } catch (error) { showNotice(`请求详情加载失败：${error.message}`, "error"); }
}

function jsonPre(value) {
  const pre = element("pre", "json-view");
  pre.textContent = JSON.stringify(redact(value), null, 2);
  return pre;
}

function openJsonDialog(title, value) {
  $("dialog-title").textContent = title;
  $("dialog-body").replaceChildren(jsonPre(value));
  $("detail-dialog").showModal();
}

function switchView(name) {
  const titles = {
    channels: ["渠道与路由", "配置 OpenAI / Claude 渠道、模型能力和自动降级优先级"],
    security: ["访问 Key", "管理 Browser API Key，明文只在创建或轮换时显示一次"],
    audit: ["系统审计", "查看管理登录、配置修改和安全操作记录"],
    conversations: ["完整对话", "按 Conversation 查看请求、响应、降级尝试和实际路由"],
    requests: ["请求日志", "筛选脱敏 metadata 请求索引"],
  };
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  $("view-title").textContent = titles[name][0];
  $("view-subtitle").textContent = titles[name][1];
  if (!state.sessionToken) return;
  if (name === "channels") loadChannels();
  if (name === "security") loadAccessKeyStatus();
  if (name === "audit") loadAudit();
  if (name === "conversations") loadConversations();
  if (name === "requests") loadRequests();
}

document.addEventListener("DOMContentLoaded", () => {
  updateSessionUi();
  loadHealth();
  $("login-form").addEventListener("submit", login);
  $("logout-btn").addEventListener("click", () => logout());
  $("refresh-channels").addEventListener("click", loadChannels);
  $("new-profile").addEventListener("click", () => openProfileEditor());
  $("cancel-profile").addEventListener("click", closeProfileEditor);
  $("reset-profile").addEventListener("click", () => openProfileEditor(state.editingProfile));
  $("add-model").addEventListener("click", () => addModelRow());
  $("profile-form").addEventListener("submit", saveProfile);
  $("profile-protocol").addEventListener("change", () => { if (!state.editingProfile) $("profile-auth").value = $("profile-protocol").value === "openai" ? "bearer" : "api-key"; });
  $("rotate-key-form").addEventListener("submit", rotateAccessKey);
  $("refresh-key").addEventListener("click", loadAccessKeyStatus);
  $("copy-key").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("revealed-key-value").textContent); showNotice("访问 Key 已复制"); }
    catch { showNotice("复制失败，请手动复制", "error"); }
  });
  $("refresh-audit").addEventListener("click", () => loadAudit());
  $("refresh-conversations").addEventListener("click", () => loadConversations());
  $("refresh-requests").addEventListener("click", () => loadRequests());
  $("request-filters").addEventListener("submit", (event) => { event.preventDefault(); state.requestPage = 1; loadRequests(1); });
  $("dialog-close").addEventListener("click", () => $("detail-dialog").close());
  document.querySelectorAll(".nav-item").forEach((item) => item.addEventListener("click", () => switchView(item.dataset.view)));
});

import {api, notice} from "./common.js";

const byId = id => document.getElementById(id);
const form = byId("model-form");
const fields = byId("model-fields");
const provider = byId("provider");
const url = byId("model-url");
const model = byId("model-name");
const key = byId("api-key");
const clear = byId("clear-key");
const status = byId("model-status");
const installed = byId("installed-models");
const test = byId("test-model");
const save = byId("save-model");
const originalLanguages = byId("original-languages");
const translationLanguages = byId("translation-languages");
const endpoint = "/api/model-settings";
const params = {
  temperature: byId("temperature"),
  max_tokens: byId("max-tokens"),
  timeout: byId("timeout"),
  num_ctx: byId("num-ctx"),
  think: byId("think"),
  json_mode: byId("json-mode"),
};
let settings;

function setBusy(busy) {
  fields.disabled = test.disabled = save.disabled = busy;
}

function numberOrNull(input) {
  const value = input.value.trim();
  return value === "" ? null : Number(value);
}

function languageList(input) {
  const seen = new Set();
  return input.value.split(/[,，、;；\n]/).map(value => value.trim()).filter(value => {
    const key = value.toLowerCase();
    if (!value || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function keyState() {
  const matches = settings.api.has_key && url.value.trim().replace(/\/+$/, "") === settings.api.url.replace(/\/+$/, "");
  byId("clear-key-label").hidden = !matches;
  if (!matches) clear.checked = false;
  key.disabled = clear.checked;
  key.placeholder = matches ? "已保存，留空保留" : "输入 API Key（可选）";
}

function showProfile() {
  const local = provider.value === "ollama";
  const profile = settings[provider.value];
  url.value = profile.url;
  url.placeholder = local ? "http://127.0.0.1:11434" : "http://127.0.0.1:1234/v1";
  model.value = profile.model;
  model.placeholder = local ? "例如 qwen3:1.7b" : "例如 deepseek-chat、qwen2.5-7b-instruct";
  key.value = "";
  clear.checked = false;
  installed.hidden = true;
  installed.replaceChildren();
  byId("api-key-fields").hidden = local;
  byId("num-ctx-field").hidden = !local;
  byId("think-field").hidden = !local;
  byId("json-mode-field").hidden = local;
  params.temperature.value = profile.temperature ?? "";
  params.max_tokens.value = profile.max_tokens ?? "";
  params.timeout.value = profile.timeout ?? "";
  params.num_ctx.value = profile.num_ctx ?? "";
  params.think.checked = Boolean(profile.think);
  params.json_mode.checked = profile.json_mode !== false;
  keyState();
}

function showSettings(result) {
  settings = result;
  provider.value = result.active;
  originalLanguages.value = (result.original_languages || []).join(", ");
  translationLanguages.value = (result.translation_languages || []).join(", ");
  showProfile();
}

function draft() {
  const remote = provider.value === "api";
  return {
    provider: provider.value,
    url: url.value.trim(),
    model: model.value.trim(),
    api_key: remote && !clear.checked && key.value.trim() ? key.value.trim() : null,
    clear_key: remote && clear.checked,
    temperature: numberOrNull(params.temperature),
    max_tokens: numberOrNull(params.max_tokens),
    timeout: numberOrNull(params.timeout),
    num_ctx: remote ? null : numberOrNull(params.num_ctx),
    think: remote ? null : params.think.checked,
    json_mode: remote ? params.json_mode.checked : null,
    original_languages: languageList(originalLanguages),
    translation_languages: languageList(translationLanguages),
  };
}

async function run(label, action) {
  notice(status, label);
  setBusy(true);
  try {
    await action();
  } catch (error) {
    notice(status, error.message, true);
  } finally {
    setBusy(false);
  }
}

provider.addEventListener("change", () => { showProfile(); notice(status); });
url.addEventListener("input", () => {
  installed.hidden = true;
  keyState();
  notice(status);
});
model.addEventListener("input", () => notice(status));
key.addEventListener("input", () => notice(status));
clear.addEventListener("change", () => { keyState(); notice(status); });
for (const input of Object.values(params)) input.addEventListener("input", () => notice(status));
for (const input of [originalLanguages, translationLanguages]) input.addEventListener("input", () => notice(status));

byId("load-models").addEventListener("click", () => {
  if (!url.reportValidity()) return;
  run("正在读取模型…", async () => {
    const query = new URLSearchParams({provider: provider.value, url: url.value.trim()});
    const {models} = await api(endpoint + "/models?" + query);
    installed.replaceChildren(new Option("选择模型", ""), ...models.map(name => new Option(name, name)));
    installed.hidden = !models.length;
    notice(status, models.length ? "已读取 " + models.length + " 个模型。" : "接口没有返回可用模型，请手动填写。");
  });
});
installed.addEventListener("change", () => {
  if (installed.value) model.value = installed.value;
  notice(status);
});

test.addEventListener("click", () => {
  if (!form.reportValidity()) return;
  const payload = draft();
  run("正在测试连接…", async () => {
    await api(endpoint + "/test", "POST", payload);
    notice(status, "连接成功。");
  });
});
form.addEventListener("submit", event => {
  event.preventDefault();
  if (!settings || !form.reportValidity()) return;
  const payload = draft();
  run("正在保存…", async () => {
    showSettings(await api(endpoint, "PUT", payload));
    notice(status, "已保存，新对话请求会使用此模型。");
  });
});

api(endpoint).then(result => {
  showSettings(result);
  setBusy(false);
  notice(status);
}).catch(error => notice(status, error.message, true));

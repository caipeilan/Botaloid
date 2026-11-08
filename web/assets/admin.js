import {api, el, notice} from "./common.js";

const byId = id => document.getElementById(id);
const library = byId("library");
const editor = byId("editor");
const table = byId("songs");
const libraryStatus = byId("library-status");
const editorStatus = byId("editor-status");
const search = byId("search");
const filter = byId("filter");
const lyrics = byId("lyrics");
const fallback = byId("fallback");
const limit = 25;
let page = 1;
let lastList = 0;
let editing = null;
let fallbackChanged = false;
let debounce;

function listRow(item) {
  const row = el("tr");
  const title = el("td", "song-title", item.title);
  title.append(el("span", "mobile-author", item.author));
  const actions = el("td", "row-actions");
  const edit = el("button", "row-button", "编辑");
  const toggle = el("button", "row-button", item.enabled ? "停用" : "启用");
  edit.type = toggle.type = "button";
  edit.setAttribute("aria-label", "编辑 " + item.title);
  toggle.setAttribute("aria-label", toggle.textContent + " " + item.title);
  edit.addEventListener("click", async () => {
    edit.disabled = true;
    try { openEditor(await api("/api/songs/" + item.id)); }
    catch (error) { notice(libraryStatus, error.message, true); }
    finally { edit.disabled = false; }
  });
  toggle.addEventListener("click", async () => {
    toggle.disabled = true;
    notice(libraryStatus);
    try {
      await api("/api/songs/" + item.id, "PATCH", {enabled: !item.enabled});
      await loadList();
    } catch (error) {
      notice(libraryStatus, error.message, true);
      toggle.disabled = false;
    }
  });
  actions.append(edit, toggle);
  row.append(title, el("td", "author-column", item.author),
    el("td", item.enabled ? "song-state enabled" : "song-state", item.enabled ? "已启用" : "已停用"), actions);
  return row;
}

async function loadList() {
  const version = ++lastList;
  const query = new URLSearchParams({q: search.value.trim(), status: filter.value, page});
  table.setAttribute("aria-busy", "true");
  try {
    const {items, total} = await api("/api/songs?" + query);
    if (version !== lastList) return;
    const pages = Math.max(1, Math.ceil(total / limit));
    if (page > pages) {
      page = pages;
      return loadList();
    }
    table.replaceChildren(...items.map(listRow));
    if (!items.length) {
      const row = el("tr");
      const cell = el("td", "empty-row", search.value.trim() || filter.value !== "all" ? "没有匹配的歌曲" : "没有歌曲");
      cell.colSpan = 4;
      row.append(cell);
      table.append(row);
    }
    byId("total").textContent = total.toLocaleString("zh-CN") + " 首";
    byId("page-number").textContent = page + " / " + pages;
    byId("prev").disabled = page <= 1;
    byId("next").disabled = page >= pages;
  } catch (error) {
    if (version === lastList) notice(libraryStatus, error.message, true);
  } finally {
    if (version === lastList) table.removeAttribute("aria-busy");
  }
}

function onSearch() {
  page = 1;
  notice(libraryStatus);
  loadList();
}
search.addEventListener("input", () => {
  clearTimeout(debounce);
  debounce = setTimeout(onSearch, 150);
});
filter.addEventListener("change", onSearch);
byId("prev").addEventListener("click", () => { page--; loadList(); });
byId("next").addEventListener("click", () => { page++; loadList(); });

function lyricText(lines) {
  const output = [];
  let last = lines[0]?.stanza;
  for (const line of lines) {
    if (line.stanza !== last) output.push("");
    output.push(line.text);
    last = line.stanza;
  }
  return output.join("\n");
}

function fallbackOptions(selected = fallback.value) {
  const unique = [...new Set(lyrics.value.split("\n").map(line => line.trim()).filter(Boolean))];
  fallback.replaceChildren(new Option("无", ""), ...unique.map(line => new Option(line, line)));
  fallback.value = unique.includes(selected) ? selected : "";
}

function openEditor(item = null) {
  editing = item;
  fallbackChanged = false;
  byId("editor-title").textContent = item ? "编辑歌曲" : "新增歌曲";
  byId("title").value = item?.title || "";
  byId("author").value = item?.author || "";
  byId("enabled").checked = item ? Boolean(item.enabled) : true;
  lyrics.value = lyricText(item?.lyrics || []);
  fallbackOptions(item?.lyrics.find(line => line.fallback)?.text || "");
  notice(editorStatus);
  library.hidden = true;
  editor.hidden = false;
  document.querySelector(".settings-scroll").scrollTop = 0;
  byId("title").focus();
}

function closeEditor() {
  editor.hidden = true;
  library.hidden = false;
  search.focus();
}

function editedLines() {
  const previous = new Map();
  for (const line of editing?.lyrics || []) {
    if (!previous.has(line.text)) previous.set(line.text, []);
    previous.get(line.text).push(line);
  }
  const result = [];
  let stanza = 0;
  let breakPending = false;
  let marked = false;
  lyrics.value.split("\n").forEach(raw => {
    const text = raw.trim();
    if (!text) {
      breakPending = result.length > 0;
      return;
    }
    if (breakPending) stanza++;
    breakPending = false;
    const old = previous.get(text)?.shift();
    const isFallback = fallbackChanged ? !marked && fallback.value === text : Boolean(old?.fallback);
    marked ||= isFallback;
    result.push({stanza, text, translation: old?.translation || null, fallback: isFallback});
  });
  return result;
}

byId("add").addEventListener("click", () => openEditor());
byId("cancel").addEventListener("click", closeEditor);
lyrics.addEventListener("input", () => fallbackOptions());
fallback.addEventListener("change", () => { fallbackChanged = true; });
byId("song-form").addEventListener("submit", async event => {
  event.preventDefault();
  const title = byId("title").value.trim();
  const author = byId("author").value.trim();
  const lines = editedLines();
  if (!title || !author || !lines.length) {
    notice(editorStatus, "请填写曲名、作者和歌词。", true);
    return;
  }
  const save = byId("save");
  save.disabled = true;
  byId("cancel").disabled = true;
  notice(editorStatus, "正在保存…");
  try {
    const id = editing?.id;
    await api(id ? "/api/songs/" + id : "/api/songs", id ? "PUT" : "POST",
      {title, author, enabled: byId("enabled").checked, lyrics: lines});
    closeEditor();
    notice(libraryStatus, "已保存。");
    loadList();
  } catch (error) {
    notice(editorStatus, error.message, true);
  } finally {
    save.disabled = false;
    byId("cancel").disabled = false;
  }
});

const file = byId("file");
const importer = byId("import");
importer.addEventListener("click", () => file.click());
file.addEventListener("change", async () => {
  const selected = file.files[0];
  if (!selected) return;
  file.value = "";
  if (selected.size > 64 * 1024 * 1024) {
    notice(libraryStatus, "请选择小于 64 MB 的 JSON 文件。", true);
    return;
  }
  importer.disabled = true;
  notice(libraryStatus, "正在导入…");
  try {
    let data;
    try { data = JSON.parse((await selected.text()).replace(/^\uFEFF/, "")); }
    catch { throw new Error("无法读取 JSON 文件。"); }
    if (!Array.isArray(data) || !data.length) throw new Error("文件中需要有歌曲列表。");
    const {count} = await api("/api/import", "POST", data);
    notice(libraryStatus, "已导入 " + count + " 首。");
    loadList();
  } catch (error) {
    notice(libraryStatus, error.message, true);
  } finally {
    importer.disabled = false;
  }
});

loadList();

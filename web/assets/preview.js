import {el, notice} from "./common.js";
import {renderWelcome} from "./welcome.js";

const byId = id => document.getElementById(id);
const hello = {text: "ハロ ハワユ", title: "ハロ/ハワユ", author: "ナノウ / 初音ミク"};
const robot = {text: "人間じゃないので。", title: "匿名M", author: "ピノキオピー / 初音ミク / ARuFa"};
const threads = [
  {title: "你好，Botaloid", turns: [
    {user: "你好"}, {reply: [hello]},
    {user: "你是人类吗？"}, {reply: [robot]},
    {user: "那你是干什么用的？吃白饭的？"},
    {reply: [{text: "はい はいそうですね その通りです", title: "ボッカデラベリタ", author: "柊キライ / v flower"}, {text: "大丈夫か？うるせえよ", title: "オーバーライド", author: "吉田夜世 / 重音テト"}]},
  ]},
  {title: "S M T W T F S", turns: [
    {user: "S M T W T F S"},
    {reply: [{text: "S M T W T F S　Back to the beginning.", title: "パノプティコン", author: "r-906 / 初音ミク"}]},
  ]},
  {title: "找一首关于机器人的歌", turns: [
    {user: "找一首关于机器人的歌"},
    {reply: [{title: "匿名M", author: robot.author}, {title: "ナキムシロボット", author: "村人P / GUMI"}]},
  ]},
];
const songs = [
  {...hello, enabled: true}, {...robot, enabled: true},
  {title: "天使だと思っていたのに", author: "鬱P / 初音ミク", text: "出来ない", enabled: true},
  {...threads[0].turns[5].reply[0], enabled: true},
  {...threads[0].turns[5].reply[1], enabled: true},
  {...threads[1].turns[1].reply[0], enabled: false},
];
const pages = ["library", "model", "appearance"];
const drawerMode = matchMedia("(max-width: 720px)");
let current = threads[0];
let editing = null;

function setDrawer(open, restoreFocus = false) {
  const visible = open && drawerMode.matches;
  byId("sidebar").classList.toggle("is-open", visible);
  byId("scrim").hidden = !visible;
  byId("main").inert = visible;
  byId("open-sidebar").setAttribute("aria-expanded", String(visible));
  if (visible) byId("close-sidebar").focus();
  else if (restoreFocus && drawerMode.matches) byId("open-sidebar").focus();
}

function resizeInput() {
  const input = byId("message");
  input.style.height = "auto";
  input.style.height = Math.min(144, input.scrollHeight) + "px";
  byId("send").disabled = !input.value.trim();
}

function renderChat() {
  byId("page-title").textContent = current.title;
  byId("welcome").hidden = Boolean(current.turns.length);
  if (!current.turns.length) renderWelcome(current);
  byId("chat-view").classList.toggle("is-empty", !current.turns.length);
  byId("messages").replaceChildren(...current.turns.map(turn => {
    if (turn.user) return el("div", "message-user", turn.user);
    const answer = el("div", "message-assistant");
    const mark = el("span", "assistant-mark");
    const avatar = el("img");
    avatar.src = "/assets/bot-avatar.svg";
    avatar.alt = "";
    mark.append(avatar);
    const fragments = el("div", "fragments");
    for (const fragment of turn.reply) {
      const block = el("div");
      block.append(el("p", "lyric", fragment.text ?? "《" + fragment.title + "》"));
      block.append(el("p", "credit", fragment.text ? "from 《" + fragment.title + "》 by " + fragment.author : fragment.author));
      fragments.append(block);
    }
    answer.append(mark, fragments);
    return answer;
  }));
  resizeInput();
}

function renderThreads() {
  byId("threads").replaceChildren(...threads.map(thread => {
    const button = el("button", "thread");
    button.type = "button";
    button.append(el("span", "", thread.title));
    if (thread === current && !pages.includes(location.hash.slice(1))) button.setAttribute("aria-current", "true");
    button.addEventListener("click", () => {
      current = thread;
      byId("message").value = "";
      location.hash = "chat";
      route();
    });
    return button;
  }));
}

function renderSongs() {
  const query = byId("search").value.trim().toLowerCase();
  const filter = byId("filter").value;
  const items = songs.filter(song => (song.title + " " + song.author).toLowerCase().includes(query)
    && (filter === "all" || song.enabled === (filter === "enabled")));
  byId("songs").replaceChildren(...items.map(song => {
    const row = el("tr");
    const title = el("td");
    title.append(el("div", "song-title", song.title), el("div", "mobile-author", song.author));
    const state = el("td", "state-column");
    state.append(el("span", "song-state" + (song.enabled ? " enabled" : ""), song.enabled ? "已启用" : "已停用"));
    const actions = el("td", "row-actions");
    const edit = el("button", "row-button", "编辑");
    edit.type = "button";
    edit.setAttribute("aria-label", "编辑 " + song.title);
    edit.addEventListener("click", () => openEditor(song));
    const toggle = el("button", "row-button", song.enabled ? "停用" : "启用");
    toggle.type = "button";
    toggle.setAttribute("aria-label", (song.enabled ? "停用 " : "启用 ") + song.title);
    toggle.addEventListener("click", () => {
      song.enabled = !song.enabled;
      renderSongs();
      notice(byId("library-status"), "样例中已" + (song.enabled ? "启用" : "停用") + "《" + song.title + "》。");
    });
    actions.append(edit, toggle);
    row.append(title, el("td", "author-column", song.author), state, actions);
    return row;
  }));
  if (!items.length) {
    const row = el("tr");
    const cell = el("td", "empty-row", "没有找到歌曲。");
    cell.colSpan = 4;
    row.append(cell);
    byId("songs").append(row);
  }
  byId("total").textContent = "共 " + items.length + " 首";
}

function openEditor(song) {
  editing = song;
  byId("library").hidden = true;
  byId("editor").hidden = false;
  byId("editor-title").textContent = song ? "编辑歌曲" : "新增歌曲";
  byId("title").value = song?.title ?? "";
  byId("author").value = song?.author ?? "";
  byId("lyrics").value = song?.text ?? "";
  byId("enabled").checked = song?.enabled ?? true;
  byId("title").focus();
}

function route() {
  const page = location.hash.slice(1);
  const settings = pages.includes(page);
  byId("chat-view").hidden = settings;
  byId("settings-view").hidden = !settings;
  byId("editor").hidden = true;
  byId("settings-link").removeAttribute("aria-current");
  if (settings) byId("settings-link").setAttribute("aria-current", "page");
  for (const name of pages) {
    byId(name).hidden = page !== name;
    byId(name + "-tab").removeAttribute("aria-current");
    if (page === name) byId(name + "-tab").setAttribute("aria-current", "page");
  }
  byId("page-title").textContent = settings ? "设置" : current.title;
  byId("mobile-new").hidden = settings;
  setDrawer(false);
  renderThreads();
  if (!settings) renderChat();
}

for (const id of ["new-chat", "mobile-new"]) byId(id).addEventListener("click", () => {
  current = {title: "新对话", turns: []};
  byId("message").value = "";
  location.hash = "chat";
  route();
  byId("message").focus();
});
byId("composer").addEventListener("submit", event => {
  event.preventDefault();
  const text = byId("message").value.trim();
  if (!text) return;
  if (!current.turns.length) {
    current.title = Array.from(text).slice(0, 24).join("");
    threads.unshift(current);
  }
  current.turns.push({user: text}, {reply: [hello]});
  byId("message").value = "";
  renderThreads();
  renderChat();
  byId("scroll-area").scrollTop = byId("scroll-area").scrollHeight;
});
byId("message").addEventListener("input", resizeInput);
byId("message").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    if (!byId("send").disabled) byId("composer").requestSubmit();
  }
});
byId("open-sidebar").addEventListener("click", () => setDrawer(true));
for (const id of ["close-sidebar", "scrim"]) byId(id).addEventListener("click", () => setDrawer(false, true));
document.addEventListener("keydown", event => { if (event.key === "Escape") setDrawer(false, true); });
drawerMode.addEventListener("change", () => setDrawer(false));
byId("search").addEventListener("input", renderSongs);
byId("filter").addEventListener("change", renderSongs);
byId("import").addEventListener("click", () => notice(byId("library-status"), "样例未连接曲库，导入请使用正式的曲库管理页面。"));
byId("add").addEventListener("click", () => openEditor(null));
byId("cancel").addEventListener("click", () => { route(); byId("add").focus(); });
byId("song-form").addEventListener("submit", event => {
  event.preventDefault();
  const draft = {title: byId("title").value.trim(), author: byId("author").value.trim(), text: byId("lyrics").value.trim(), enabled: byId("enabled").checked};
  if (!draft.title || !draft.author || !draft.text) return;
  if (editing) Object.assign(editing, draft);
  else songs.push(draft);
  renderSongs();
  route();
  notice(byId("library-status"), "已保存在当前样例中。");
  byId("add").focus();
});
byId("provider").addEventListener("change", () => {
  const local = byId("provider").value === "ollama";
  byId("api-key-fields").hidden = local;
  byId("local-models").hidden = !local;
  byId("model-url").value = local ? "http://localhost:11434" : "https://api.deepseek.com";
  byId("model-name").value = local ? "" : "deepseek-flash";
  byId("model-name").placeholder = local ? "输入已安装的模型名称" : "输入模型名称";
  notice(byId("model-status"));
});
for (const id of ["test-model", "load-models"]) byId(id).addEventListener("click", () => notice(byId("model-status"), "样例未连接模型。"));
byId("model-form").addEventListener("submit", event => {
  event.preventDefault();
  byId("api-key").value = "";
  notice(byId("model-status"), "样例已更新，模型配置请在正式的设置页面中保存。");
});
window.addEventListener("hashchange", route);
renderSongs();
route();

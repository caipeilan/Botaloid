import {api, el, icon, notice} from "./common.js";
import {readChats, setupSidebar, storageKey} from "./navigation.js";
import {renderWelcome} from "./welcome.js";

const byId = id => document.getElementById(id);
const messages = byId("messages");
const message = byId("message");
const status = byId("chat-status");
const composer = byId("composer");
const setDrawer = setupSidebar();
const requested = new URLSearchParams(location.hash.slice(1)).get("chat");
let threads = [];
let current;
let pending = null;

try {
  const saved = readChats();
  if (Array.isArray(saved?.threads)) {
    threads = saved.threads.filter(item => item && Array.isArray(item.turns)).slice(0, 30);
    if (location.hash !== "#new") current = threads.find(item => item.id === requested) || threads.find(item => item.id === saved.active);
  }
} catch {}

function fresh() {
  return {
    id: globalThis.crypto?.randomUUID?.() || String(Date.now()),
    title: "新对话",
    turns: [],
    tone: "neutral",
  };
}
current ||= fresh();
if (requested !== null || location.hash === "#new") window.history.replaceState(null, "", location.pathname + location.search);

function save() {
  try {
    localStorage.setItem(storageKey, JSON.stringify({active: current.id, threads}));
  } catch {}
}

function resizeInput() {
  message.style.height = "auto";
  message.style.height = Math.min(144, message.scrollHeight) + "px";
  byId("send").disabled = Boolean(pending) || !message.value.trim();
}

function removeThread(thread, event) {
  event.stopPropagation();
  if (!confirm("删除「" + thread.title + "」？")) return;
  threads = threads.filter(item => item !== thread);
  if (pending === thread) pending = null;
  if (current === thread) current = threads[0] || fresh();
  save();
  render();
  message.focus();
}

function threadRow(thread) {
  const row = el("div", "thread-row");
  const button = el("button", "thread");
  button.type = "button";
  button.title = thread.title;
  if (thread === current) button.setAttribute("aria-current", "true");
  button.append(el("span", "", thread.title));
  button.addEventListener("click", () => switchThread(thread));
  const remove = el("button", "thread-delete");
  remove.type = "button";
  remove.title = "删除对话";
  remove.setAttribute("aria-label", "删除对话 " + thread.title);
  remove.append(icon("close"));
  remove.addEventListener("click", event => removeThread(thread, event));
  row.append(button, remove);
  return row;
}

function render() {
  byId("thread-title").textContent = current.title;
  byId("threads").replaceChildren(...threads.map(threadRow));

  const empty = current.turns.length === 0;
  byId("chat-stage").classList.toggle("is-empty", empty);
  byId("welcome").hidden = !empty;
  if (empty) renderWelcome(current);
  messages.replaceChildren(...current.turns.map(turn => {
    if (turn.role === "user") return el("div", "message-user", turn.text);
    const answer = el("div", "message-assistant");
    answer.setAttribute("aria-label", turn.songs?.length ? "曲目" : "回复");
    const mark = el("span", "assistant-mark");
    const avatar = el("img");
    avatar.src = "/assets/bot-avatar.svg";
    avatar.alt = "";
    mark.append(avatar);
    const fragments = el("div", "fragments");
    for (const fragment of turn.fragments || []) {
      const block = el("div");
      block.append(
        el("p", "lyric", fragment.text),
        el("p", "credit", "from 《" + fragment.title + "》 by " + fragment.author),
      );
      fragments.append(block);
    }
    for (const song of turn.songs || []) {
      const block = el("div");
      block.append(el("p", "lyric song-title", "《" + song.title + "》"), el("p", "credit", song.author));
      if (song.excerpt) block.append(el("p", "lyric song-excerpt", song.excerpt));
      fragments.append(block);
    }
    answer.append(mark, fragments);
    return answer;
  }));
  if (pending) notice(status, pending === current ? "正在找歌词…" : "正在回复另一条对话…");
  else notice(status, current.error || current.notice, Boolean(current.error));
  resizeInput();
  const area = byId("scroll-area");
  area.scrollTop = area.scrollHeight;
}

function switchThread(thread) {
  current = thread;
  message.value = "";
  setDrawer(false, false);
  save();
  render();
  message.focus();
}

composer.addEventListener("submit", async event => {
  event.preventDefault();
  const text = message.value.trim();
  if (!text || pending) return;

  const thread = current;
  const history = thread.turns.slice(-10).map(turn => {
    if (turn.role === "user") return {role: "user", text: turn.text};
    const fragments = turn.fragments || [];
    const songs = turn.songs || [];
    const items = songs.length ? songs : fragments;
    const lines = songs.length ? songs.map(song => `《${song.title}》 ${song.author}\n${song.excerpt || ""}`) :
      fragments.map(fragment => fragment.text);
    return {
      role: "assistant",
      text: Array.from(lines.join("\n")).slice(0, 1000).join(""),
      source: items.at(-1)?.source,
      song_ids: items.map(item => item.id ?? item.song_id ?? item.source?.song_id).filter(Boolean),
      intent: turn.intent,
    };
  });
  if (!thread.turns.length) {
    thread.title = Array.from(text.replace(/\s+/g, " ")).slice(0, 36).join("");
  }
  thread.turns.push({role: "user", text});
  thread.error = "";
  thread.notice = "";
  threads = [thread, ...threads.filter(item => item !== thread)].slice(0, 30);
  pending = thread;
  message.value = "";
  save();
  render();
  try {
    const {fragments, songs = [], tone, intent, ended} = await api("/api/chat", "POST", {message: text, history, tone: thread.tone});
    thread.tone = tone;
    if (fragments.length || songs.length) thread.turns.push({role: "assistant", fragments, songs, intent});
    else if (ended) thread.notice = "这首歌唱完了。";
    else if (intent === "find_song" || intent === "recommend") thread.notice = "没找到合适的曲目。";
    else thread.error = "没找到合适的歌词。";
  } catch (error) {
    thread.error = error.message === "Failed to fetch" ? "连接失败，请重试。" : error.message;
    if (current === thread && !message.value) message.value = text;
  } finally {
    pending = null;
    thread.turns = thread.turns.slice(-60);
    save();
    render();
  }
});

message.addEventListener("input", resizeInput);
message.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    if (!byId("send").disabled) composer.requestSubmit();
  }
});
for (const id of ["new-chat", "mobile-new"]) byId(id).addEventListener("click", () => switchThread(fresh()));
api("/api/config").then(result => { byId("settings-link").hidden = !result.admin; }).catch(() => {});
save();
render();

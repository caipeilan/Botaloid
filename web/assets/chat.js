import {api, chat, el, icon, notice} from "./common.js";
import {readChats, setupSidebar, storageKey} from "./navigation.js";
import {renderWelcome} from "./welcome.js";

const byId = id => document.getElementById(id);
const messages = byId("messages");
const message = byId("message");
const status = byId("chat-status");
const composer = byId("composer");
const area = byId("scroll-area");
const setDrawer = setupSidebar();
const requested = new URLSearchParams(location.hash.slice(1)).get("chat");
const pending = new Map();
const waiting = {
  replanning: ["换个方向找找…", "试试另一种接法…"],
  fallback: ["找一句能接住这句话的歌词…", "找找怎么表达这份态度…"],
  curated: ["找一句合适的保底台词…", "再翻翻备好的歌词…"],
  planning: ["听听这句话里的意思…", "想想怎么接住这句话…", "理一理前面聊到的内容…"],
  retrieving: ["正在翻找曲库…", "找找唱过这份心情的歌…", "沿着歌词找一找…"],
  selecting: ["挑一句合适的歌词…", "听听哪一句更贴切…", "找个自然的接法…"],
  validating: ["核对歌词和出处…", "确认这几句的顺序…"],
};
let threads = [];
let current;
let renderedThread = null;
let renderedTurns = [];
let storageError = "";
let saveTimer;

try {
  const saved = readChats();
  if (Array.isArray(saved?.threads)) {
    threads = saved.threads.filter(item => item && Array.isArray(item.turns)).slice(0, 30);
    for (const thread of threads) {
      if (thread.awaiting) thread.error = "上次请求未完成。";
      thread.awaiting = false;
    }
    if (location.hash !== "#new") current = threads.find(item => item.id === requested) || threads.find(item => item.id === saved.active);
  }
} catch {}

function fresh() {
  return {id: globalThis.crypto?.randomUUID?.() || String(Date.now()), title: "新对话", turns: [], tone: "neutral", draft: ""};
}
current ||= fresh();
message.value = current.draft || "";
if (requested !== null || location.hash === "#new") window.history.replaceState(null, "", location.pathname + location.search);

function save() {
  clearTimeout(saveTimer);
  try {
    localStorage.setItem(storageKey, JSON.stringify({active: current.id, threads}));
    storageError = "";
  } catch {
    storageError = "当前浏览器无法保存对话。";
  }
}

function resizeInput() {
  message.style.height = "auto";
  message.style.height = Math.min(144, message.scrollHeight) + "px";
  byId("send").disabled = pending.has(current.id) || !message.value.trim();
}

function showStatus() {
  const request = pending.get(current.id);
  if (request) {
    const lines = waiting[request.stage] || waiting.planning;
    notice(status, lines[request.line % lines.length]);
  } else notice(status, current.error || storageError || current.notice, Boolean(current.error || storageError));
}

function removeThread(thread, event) {
  event.stopPropagation();
  if (!confirm("删除「" + thread.title + "」？")) return;
  const request = pending.get(thread.id);
  if (request) {
    clearInterval(request.timer);
    pending.delete(thread.id);
    request.controller.abort();
  }
  threads = threads.filter(item => item !== thread);
  if (current === thread) {
    current = threads[0] || fresh();
    message.value = current.draft || "";
  }
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

function turnNode(turn) {
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
    block.append(el("p", "lyric", fragment.text), el("p", "credit", "from 《" + fragment.title + "》 by " + fragment.author));
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
}

function render(forceBottom = false) {
  const switched = renderedThread !== current;
  const follow = forceBottom || area.scrollHeight - area.scrollTop - area.clientHeight < 80;
  const oldHeight = area.scrollHeight;
  const oldTop = area.scrollTop;
  byId("thread-title").textContent = current.title;
  byId("threads").replaceChildren(...threads.map(threadRow));
  const empty = current.turns.length === 0;
  byId("chat-stage").classList.toggle("is-empty", empty);
  byId("welcome").hidden = !empty;
  if (empty) renderWelcome(current);
  const reset = switched || renderedTurns.some((turn, index) => current.turns[index] !== turn);
  if (reset) {
    messages.replaceChildren(...current.turns.map(turnNode));
  } else {
    messages.append(...current.turns.slice(renderedTurns.length).map(turnNode));
  }
  renderedThread = current;
  renderedTurns = current.turns.slice();
  showStatus();
  resizeInput();
  if (switched) area.scrollTop = current.scrollTop ?? area.scrollHeight;
  else if (follow) area.scrollTop = area.scrollHeight;
  else if (reset) area.scrollTop = Math.max(0, oldTop + area.scrollHeight - oldHeight);
}

function switchThread(thread) {
  current.draft = message.value;
  current.scrollTop = area.scrollTop;
  current = thread;
  message.value = current.draft || "";
  setDrawer(false, false);
  save();
  render();
  message.focus();
}

composer.addEventListener("submit", async event => {
  event.preventDefault();
  const text = message.value.trim();
  if (!text || pending.has(current.id)) return;
  const thread = current;
  const history = thread.turns.slice(-10).map(turn => {
    if (turn.role === "user") return {role: "user", text: turn.text};
    const fragments = turn.fragments || [];
    const songs = turn.songs || [];
    const items = songs.length ? songs : fragments;
    const lines = songs.length ? songs.map(song => `《${song.title}》 ${song.author}\n${song.excerpt || ""}`) : fragments.map(fragment => fragment.text);
    return {
      role: "assistant", text: Array.from(lines.join("\n")).slice(0, 1000).join(""),
      source: items.at(-1)?.source,
      song_ids: items.map(item => item.id ?? item.song_id ?? item.source?.song_id).filter(Boolean),
      intent: turn.intent, conditions: turn.conditions, fallback: turn.fallback, requested_intent: turn.requested_intent,
    };
  });
  if (!thread.turns.length) thread.title = Array.from(text.replace(/\s+/g, " ")).slice(0, 36).join("");
  thread.turns.push({role: "user", text});
  thread.error = thread.notice = thread.draft = "";
  thread.awaiting = true;
  threads = [thread, ...threads.filter(item => item !== thread)].slice(0, 30);
  const request = {controller: new AbortController(), stage: "planning", line: 0};
  pending.set(thread.id, request);
  request.timer = setInterval(() => {
    request.line++;
    if (current === thread) showStatus();
  }, 6000);
  message.value = "";
  save();
  render(true);
  try {
    const result = await chat({message: text, history, tone: thread.tone}, stage => {
      if (pending.get(thread.id) !== request) return;
      if (request.stage !== stage) request.line = 0;
      request.stage = stage;
      if (current === thread) showStatus();
    }, request.controller.signal);
    if (pending.get(thread.id) !== request) return;
    const {fragments, songs = [], tone, intent, ended, conditions, fallback, requested_intent} = result;
    thread.tone = tone;
    if (fragments.length || songs.length) thread.turns.push({role: "assistant", fragments, songs, intent, conditions, fallback, requested_intent});
    else if (ended) thread.notice = "这首歌唱完了。";
    else thread.notice = intent === "find_song" || intent === "recommend" ? "未找到匹配的曲目。" : "未找到匹配的歌词。";
  } catch (error) {
    if (pending.get(thread.id) === request && error.name !== "AbortError") {
      thread.error = error.message === "Failed to fetch" ? "连接失败，本次回复未完成。" : error.message;
    }
  } finally {
    clearInterval(request.timer);
    if (pending.get(thread.id) === request) {
      pending.delete(thread.id);
      thread.awaiting = false;
      thread.turns = thread.turns.slice(-60);
      save();
      if (current === thread) render();
    }
  }
});

message.addEventListener("input", () => {
  current.draft = message.value;
  const untitled = !current.turns.length;
  if (untitled) current.title = Array.from(message.value.trim().replace(/\s+/g, " ")).slice(0, 36).join("") || "新对话";
  if (current.draft && !threads.includes(current)) {
    threads = [current, ...threads].slice(0, 30);
  }
  if (untitled) {
    byId("thread-title").textContent = current.title;
    byId("threads").replaceChildren(...threads.map(threadRow));
  }
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { save(); showStatus(); }, 250);
  resizeInput();
});
message.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    if (!byId("send").disabled) composer.requestSubmit();
  }
});
document.addEventListener("visibilitychange", () => { if (document.hidden) save(); });
for (const id of ["new-chat", "mobile-new"]) byId(id).addEventListener("click", () => switchThread(fresh()));
api("/api/config").then(result => { byId("settings-link").hidden = !result.admin; }).catch(() => {});
save();
render();

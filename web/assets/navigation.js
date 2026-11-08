import {el, icon} from "./common.js";

export const storageKey = "botaloid-chats";

export function readChats() {
  try {
    return JSON.parse(localStorage.getItem(storageKey) ?? localStorage.getItem("vocaloid-rag-chats"));
  } catch { return null; }
}

export function setupSidebar() {
  const byId = id => document.getElementById(id);
  const mobile = matchMedia("(max-width: 720px)");
  let opened = false;
  function setDrawer(open, restoreFocus = true) {
    opened = open && mobile.matches;
    byId("sidebar").classList.toggle("is-open", opened);
    byId("scrim").hidden = !opened;
    byId("open-sidebar").setAttribute("aria-expanded", String(opened));
    document.querySelector(".chat-main").inert = opened;
    if (opened) byId("close-sidebar").focus();
    else if (restoreFocus && mobile.matches) byId("open-sidebar").focus();
  }
  byId("open-sidebar").addEventListener("click", () => setDrawer(true));
  for (const id of ["close-sidebar", "scrim"]) byId(id).addEventListener("click", () => setDrawer(false));
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && opened) setDrawer(false);
  });
  mobile.addEventListener("change", () => setDrawer(false, false));
  return setDrawer;
}

function iconButton(id, label, name) {
  const button = el("button", "icon-button " + (id === "open-sidebar" ? "drawer-toggle" : "drawer-close"));
  button.id = id;
  button.type = "button";
  button.setAttribute("aria-label", label);
  button.append(icon(name));
  return button;
}

function settingsNavigation() {
  const content = document.querySelector("main.admin-main");
  const tabs = document.querySelector(".settings-tabs");
  const back = document.querySelector(".back-link");
  const scrim = el("button", "scrim");
  scrim.id = "scrim";
  scrim.type = "button";
  scrim.tabIndex = -1;
  scrim.hidden = true;
  scrim.setAttribute("aria-label", "关闭会话列表");
  const sidebar = el("aside", "sidebar");
  sidebar.id = "sidebar";
  sidebar.setAttribute("aria-label", "会话列表");
  const brand = el("a", "brand");
  brand.href = "/";
  const mark = el("span", "brand-icon");
  const logo = el("img");
  logo.src = "/assets/brand-logo.svg";
  logo.alt = "";
  mark.append(logo);
  brand.append(mark, el("span", "", "Botaloid"));
  const newChat = el("a", "new-chat");
  newChat.href = "/#new";
  newChat.append(icon("plus"), document.createTextNode("新对话"));
  const threads = el("nav", "threads");
  threads.setAttribute("aria-label", "历史对话");
  const saved = readChats();
  for (const thread of Array.isArray(saved?.threads) ? saved.threads.slice(0, 30) : []) {
    if (!thread || typeof thread.id !== "string" || typeof thread.title !== "string" || !Array.isArray(thread.turns)) continue;
    const link = el("a", "thread");
    link.href = "/#chat=" + encodeURIComponent(thread.id);
    link.title = thread.title;
    link.append(el("span", "", thread.title));
    threads.append(link);
  }
  const settings = el("a", "side-link");
  settings.href = "/settings";
  settings.setAttribute("aria-current", "page");
  settings.append(icon("settings"), document.createTextNode("设置"));
  sidebar.append(brand, iconButton("close-sidebar", "关闭会话列表", "close"), newChat, threads, settings);
  const shell = el("div", "chat-main");
  const header = el("header", "chat-header");
  const toggle = iconButton("open-sidebar", "打开会话列表", "menu");
  toggle.setAttribute("aria-controls", "sidebar");
  toggle.setAttribute("aria-expanded", "false");
  back.classList.add("settings-back");
  header.append(toggle, el("span", "thread-title", "设置"), back);
  const view = el("div", "settings-view");
  const scroll = el("div", "settings-scroll");
  scroll.append(content);
  view.append(tabs, scroll);
  shell.append(header, view);
  document.body.className = "chat-page settings-page";
  document.body.replaceChildren(scrim, sidebar, shell);
  setupSidebar();
}

if (document.body.classList.contains("admin-page")) settingsNavigation();

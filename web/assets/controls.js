import {el, icon} from "./common.js";
import "./surfaces.js";

let opened;

function enhanceSelect(select) {
  const labels = Array.from(select.labels || []);
  const name = select.getAttribute("aria-label") || labels.map(label => {
    const copy = label.cloneNode(true);
    copy.querySelectorAll("select").forEach(node => node.remove());
    return copy.textContent.trim();
  }).join(" ") || "选项";
  const wrapper = el("span", "custom-select");
  const trigger = el("button", "select-trigger");
  const value = el("span", "select-value");
  const popup = el("div", "select-menu");
  const list = el("ul", "select-options");
  const id = select.id || "select-" + document.querySelectorAll(".custom-select").length;
  let active = select.selectedIndex;
  let prefix = "";
  let typedAt = 0;
  trigger.type = "button";
  trigger.id = id + "-trigger";
  trigger.setAttribute("role", "combobox");
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  trigger.setAttribute("aria-controls", id + "-options");
  list.id = id + "-options";
  popup.hidden = true;
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", name);
  trigger.append(value, icon("down"));
  select.before(wrapper);
  wrapper.append(select, trigger);
  popup.append(list);
  document.body.append(popup);
  select.classList.add("native-select");
  select.tabIndex = -1;
  select.setAttribute("aria-hidden", "true");
  for (const label of labels) {
    if (label.htmlFor === select.id) label.htmlFor = trigger.id;
  }

  function close() {
    popup.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    trigger.removeAttribute("aria-activedescendant");
    if (opened?.trigger === trigger) opened = null;
  }

  function highlight() {
    for (const item of list.querySelectorAll(".select-option")) item.classList.toggle("is-active", Number(item.dataset.index) === active);
    const item = list.querySelector(`[data-index="${active}"]`);
    if (item) {
      trigger.setAttribute("aria-activedescendant", item.id);
      item.scrollIntoView({block: "nearest"});
    }
  }

  function position() {
    const rect = trigger.getBoundingClientRect();
    const width = Math.min(Math.max(rect.width, 160), window.innerWidth - 24);
    popup.style.width = width + "px";
    popup.style.left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)) + "px";
    const below = window.innerHeight - rect.bottom - 14;
    const above = rect.top - 14;
    const upward = below < Math.min(240, list.scrollHeight + 16) && above > below;
    list.style.maxHeight = Math.max(44, Math.min(280, upward ? above : below) - 14) + "px";
    popup.style.top = (upward ? Math.max(12, rect.top - popup.offsetHeight - 6) : rect.bottom + 6) + "px";
  }

  function sync() {
    wrapper.hidden = select.hidden;
    trigger.disabled = select.matches(":disabled");
    value.textContent = select.selectedOptions[0]?.textContent || "";
    trigger.setAttribute("aria-label", name + "：" + value.textContent);
    list.replaceChildren(...Array.from(select.options, (option, index) => {
      const item = el("li", "select-option");
      item.id = id + "-option-" + index;
      item.dataset.index = index;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(option.selected));
      item.setAttribute("aria-disabled", String(option.disabled));
      item.append(el("span", "", option.textContent), icon("check"));
      return item;
    }));
    if (wrapper.hidden || trigger.disabled) close();
    else if (!popup.hidden) { highlight(); position(); }
  }

  function open() {
    if (trigger.disabled) return;
    opened?.close();
    sync();
    active = select.selectedIndex;
    popup.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    opened = {trigger, list, popup, close};
    position();
    highlight();
  }

  function choose(index) {
    if (!select.options[index] || select.options[index].disabled) return;
    select.selectedIndex = index;
    select.dispatchEvent(new Event("input", {bubbles: true}));
    select.dispatchEvent(new Event("change", {bubbles: true}));
    sync();
    close();
  }

  trigger.addEventListener("click", () => popup.hidden ? open() : close());
  trigger.addEventListener("blur", close);
  list.addEventListener("pointerdown", event => event.preventDefault());
  list.addEventListener("click", event => {
    const item = event.target.closest(".select-option");
    if (item) choose(Number(item.dataset.index));
  });
  trigger.addEventListener("keydown", event => {
    if (event.key === "Tab") { close(); return; }
    if (event.key === "Escape") { close(); event.preventDefault(); return; }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (popup.hidden) open(); else choose(active);
      return;
    }
    const options = Array.from(select.options).map((option, index) => ({option, index})).filter(item => !item.option.disabled);
    if (!options.length) return;
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      if (popup.hidden) open();
      const at = options.findIndex(item => item.index === active);
      const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 :
        Math.max(0, Math.min(options.length - 1, at + (event.key === "ArrowDown" ? 1 : -1)));
      active = options[next].index;
      highlight();
    } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      const now = Date.now();
      prefix = now - typedAt > 700 ? event.key : prefix + event.key;
      typedAt = now;
      const match = options.find(item => item.option.textContent.toLowerCase().startsWith(prefix.toLowerCase()));
      if (match) {
        if (popup.hidden) open();
        active = match.index;
        highlight();
      }
    }
  });
  select.addEventListener("change", sync);
  select.form?.addEventListener("reset", () => queueMicrotask(sync));
  new MutationObserver(sync).observe(select, {childList: true, subtree: true, attributes: true, characterData: true});
  const fieldset = select.closest("fieldset");
  if (fieldset) new MutationObserver(sync).observe(fieldset, {attributes: true, attributeFilter: ["disabled"]});
  sync();
}

document.querySelectorAll("select").forEach(enhanceSelect);
document.addEventListener("pointerdown", event => {
  if (opened && !opened.trigger.contains(event.target) && !opened.popup.contains(event.target)) opened.close();
});
window.addEventListener("hashchange", () => opened?.close());
window.addEventListener("resize", () => opened?.close());
document.addEventListener("scroll", event => {
  if (opened && event.target !== opened.list) opened.close();
}, true);

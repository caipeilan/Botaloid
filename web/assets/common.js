export function el(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = content;
  return element;
}

export function icon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", "/assets/icons.svg#" + name);
  svg.setAttribute("aria-hidden", "true");
  svg.append(use);
  return svg;
}

export async function api(url, method = "GET", data) {
  const response = await fetch(url, {
    method,
    headers: data === undefined ? {} : {"Content-Type": "application/json"},
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (Array.isArray(result.detail) && url === "/api/import") {
      const location = result.detail[0]?.loc || [];
      const entry = location[1];
      if (typeof entry === "number") throw new Error("第 " + (entry + 1) + " 首歌曲的格式有误。");
    }
    throw new Error(typeof result.detail === "string" ? result.detail : "请求失败，请重试。");
  }
  return result;
}

export function notice(element, text = "", error = false) {
  element.hidden = !text;
  element.textContent = text;
  element.dataset.error = String(error);
}

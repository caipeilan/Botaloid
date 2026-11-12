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

export async function chat(data, onStage, signal) {
  const response = await fetch("/api/chat/events", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data), signal,
  });
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    throw new Error(typeof result.detail === "string" ? result.detail : "请求失败。");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value, {stream: !done});
      let end;
      while ((end = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, end).trim();
        buffer = buffer.slice(end + 1);
        if (!line) continue;
        const event = JSON.parse(line);
        if (event.type === "stage") onStage(event.stage);
        else if (event.type === "result") return event.result;
        else if (event.type === "error") throw new Error(event.detail);
      }
      if (done) throw new Error("连接已中断，本次回复未完成。");
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export function notice(element, text = "", error = false) {
  element.hidden = !text;
  element.textContent = text;
  element.dataset.error = String(error);
}

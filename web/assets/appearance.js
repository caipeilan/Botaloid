import {notice} from "./common.js";

const key = "botaloid-font";
let font = "xiaolai";
try {
  if (localStorage.getItem(key) === "system") font = "system";
} catch {}
document.documentElement.dataset.font = font;

const select = document.getElementById("interface-font");
if (select) {
  select.value = font;
  select.addEventListener("change", () => {
    document.documentElement.dataset.font = select.value;
    const status = document.getElementById("font-status");
    try {
      localStorage.setItem(key, select.value);
      if (status) notice(status);
    } catch {
      if (status) notice(status, "字体已切换，当前浏览器无法保存设置。", true);
    }
  });
}

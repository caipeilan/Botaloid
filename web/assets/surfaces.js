const ns = "http://www.w3.org/2000/svg";
const frames = ".button,.icon-button,.row-button,.new-chat,.composer,.search,.select-trigger,.select-menu,.table-frame,.message-user,.thread,.thread-delete,.side-link,.settings-tabs a";
const fields = ".editor input:not([type='checkbox']),.lyric-input,.checkbox input";
const edges = ".chat-header,.admin-header,.settings-tabs,.font-preview";
const targets = frames + "," + fields + "," + edges + ",.sidebar";
const surfaces = new WeakMap();
const wrapped = new WeakSet();
let nextClip = 0;

// One seed per tab. It survives switching pages and only rolls on a manual reload.
function baseSeed() {
  const navigation = performance.getEntriesByType?.("navigation")?.[0];
  const reload = navigation?.type === "reload";
  let seed = Number(sessionStorage.getItem("botaloid-surface-seed"));
  if (reload || !Number.isFinite(seed) || seed <= 0) {
    seed = Math.floor(Math.random() * 4294967296);
    try { sessionStorage.setItem("botaloid-surface-seed", String(seed)); } catch {}
  }
  return seed;
}

const seedBase = baseSeed();

function hash(value) {
  let result = 2166136261;
  for (let i = 0; i < value.length; i++) {
    result ^= value.charCodeAt(i);
    result = Math.imul(result, 16777619);
  }
  return result >>> 0;
}

function seedFor(host, kind, width, height) {
  const text = (host.textContent || "").replace(/\s+/g, " ").slice(0, 160);
  return hash(seedBase + "|" + kind + "|" + Math.round(width) + "|" + Math.round(height) + "|" + text);
}

const bends = [
  [[.14, 0, .27, -1, .52, -.8], [.73, -.6, .85, 0, 1, 0]],
  [[.12, 0, .26, -.9, .45, -.4], [.66, .2, .84, 0, 1, 0]],
  [[.15, 0, .3, -.95, .5, 0], [.7, .95, .85, 0, 1, 0]],
  [[.25, 0, .32, .85, .56, .6], [.76, .4, .88, 0, 1, 0]],
  [[.08, 0, .17, .5, .32, .35], [.47, .2, .51, -.8, .7, -.4], [.84, -.1, .94, 0, 1, 0]],
  [[.22, 0, .2, -.9, .42, -.75], [.7, -.6, .9, 0, 1, 0]],
];

function randomFrom(seed) {
  let value = seed >>> 0;
  return () => {
    value = (Math.imul(value, 1664525) + 1013904223) >>> 0;
    return value / 4294967296;
  };
}

function curveSegments(x, y, endX, endY, amplitude, seed) {
  const length = Math.hypot(endX - x, endY - y);
  if (length < .5) return `L${endX} ${endY}`;
  const dx = (endX - x) / length;
  const dy = (endY - y) / length;
  const random = randomFrom(seed);
  let distance = 0;
  let path = "";
  while (distance < length) {
    const remaining = length - distance;
    let span = Math.min(remaining, 42 + random() * 48);
    if (remaining - span < 20) span = remaining;
    const bend = bends[Math.floor(random() * bends.length)];
    const height = Math.min(amplitude, span * .09) * (.7 + random() * .6) * (random() < .5 ? -1 : 1) * .5;
    for (const segment of bend) {
      const points = [];
      for (let i = 0; i < 6; i += 2) {
        const along = distance + segment[i] * span;
        const across = segment[i + 1] * height;
        points.push((x + dx * along - dy * across).toFixed(2), (y + dy * along + dx * across).toFixed(2));
      }
      path += "C" + points.join(" ");
    }
    distance += span;
  }
  return path;
}

function svgNode(tag, className) {
  const node = document.createElementNS(ns, tag);
  if (className) node.setAttribute("class", className);
  return node;
}

function framePath(w, h, seed, outset = 0) {
  const amplitude = Math.min(4.2, w * .065, h * .065);
  const inset = amplitude + 1.2 - outset;
  const r = Math.min(9, (w - 2 * inset) / 3, (h - 2 * inset) / 3);
  const right = w - inset;
  const bottom = h - inset;
  return `M${inset + r} ${inset}`
    + curveSegments(inset + r, inset, right - r, inset, amplitude, seed)
    + `Q${right} ${inset} ${right} ${inset + r}`
    + curveSegments(right, inset + r, right, bottom - r, amplitude, seed + 1)
    + `Q${right} ${bottom} ${right - r} ${bottom}`
    + curveSegments(right - r, bottom, inset + r, bottom, amplitude, seed + 2)
    + `Q${inset} ${bottom} ${inset} ${bottom - r}`
    + curveSegments(inset, bottom - r, inset, inset + r, amplitude, seed + 3)
    + `Q${inset} ${inset} ${inset + r} ${inset}Z`;
}

function linePath(w, y, seed) {
  return `M1.5 ${y}` + curveSegments(1.5, y, w - 1.5, y, 5, seed);
}

function paint(host) {
  const data = surfaces.get(host);
  const rect = host.getBoundingClientRect();
  const w = Math.round(rect.width * 100) / 100;
  const h = Math.round(rect.height * 100) / 100;
  if (!w || !h) return;
  data.seed = seedFor(host, data.kind, w, h);
  for (const art of [data.fill, data.ink]) art.setAttribute("viewBox", `0 0 ${w} ${h}`);
  if (data.kind === "sidebar") {
    const edge = `M${w - 7} 0` + curveSegments(w - 7, 0, w - 7, h, 5.5, data.seed);
    data.base.setAttribute("d", edge + `H0V0Z`);
    data.outline.setAttribute("d", edge);
  } else if (data.kind === "edges") {
    data.base.setAttribute("d", "");
    data.outline.setAttribute("d", (host.matches(".font-preview") ? linePath(w, 6, data.seed) : "") + linePath(w, h - 6, data.seed + 1));
  } else {
    // A textarea clips its text at the padding box, so draw its frame just outside
    // the element; otherwise a half-visible line crosses the border.
    const d = framePath(w, h, data.seed, host.querySelector("textarea") ? 6 : 0);
    data.base.setAttribute("d", d);
    data.outline.setAttribute("d", d);
    if (data.clip) {
      data.clip.setAttribute("d", d);
      const head = host.querySelector("thead");
      const y = head ? head.getBoundingClientRect().bottom - rect.top : 0;
      data.header.setAttribute("d", y > 0 ? linePath(w, y, data.seed + 10) + `L${w - 1.5} 0H1.5Z` : "");
      data.rules.setAttribute("d", Array.from(host.querySelectorAll("tr"), row => row.getBoundingClientRect().bottom - rect.top)
        .map((bottom, index) => bottom > 2 && bottom < h - 3 ? linePath(w, bottom, data.seed + 10 + index) : "").join(""));
    }
  }
}

const resize = new ResizeObserver(entries => entries.forEach(entry => paint(entry.target)));

function attach(host, kind = "frame") {
  if (surfaces.has(host)) { resize.observe(host); return; }
  host.classList.add("surface", "surface-" + kind);
  const fill = svgNode("svg", "surface-art surface-fill");
  const ink = svgNode("svg", "surface-art surface-ink");
  for (const art of [fill, ink]) {
    art.setAttribute("aria-hidden", "true");
    art.setAttribute("focusable", "false");
    art.setAttribute("preserveAspectRatio", "none");
  }
  const base = svgNode("path", "surface-base");
  const outline = svgNode("path", "surface-outline");
  fill.append(base);
  ink.append(outline);
  const data = {kind, fill, ink, base, outline};
  if (host.matches(".table-frame")) {
    const id = "surface-clip-" + nextClip++;
    const defs = svgNode("defs");
    const clipPath = svgNode("clipPath");
    clipPath.id = id;
    data.clip = svgNode("path");
    clipPath.append(data.clip);
    defs.append(clipPath);
    data.header = svgNode("path", "surface-header-fill");
    data.header.setAttribute("clip-path", `url(#${id})`);
    data.rules = svgNode("path", "surface-rule");
    data.rules.setAttribute("clip-path", `url(#${id})`);
    fill.prepend(defs);
    fill.append(data.header);
    ink.prepend(data.rules);
  }
  surfaces.set(host, data);
  host.prepend(fill);
  host.append(ink);
  resize.observe(host);
}

function mount(node) {
  if (node.matches(fields)) {
    if (wrapped.has(node)) {
      const host = node.parentElement;
      if (surfaces.has(host)) resize.observe(host);
      return;
    }
    wrapped.add(node);
    const wrapper = document.createElement("span");
    wrapper.className = node.type === "checkbox" ? "checkbox-surface" : "field-surface";
    node.before(wrapper);
    wrapper.append(node);
    attach(wrapper);
  } else attach(node, node.matches(".sidebar") ? "sidebar" : node.matches(edges) ? "edges" : "frame");
}

function scan(root) {
  if (root.matches?.(targets)) mount(root);
  root.querySelectorAll(targets).forEach(mount);
}

scan(document.body);
new MutationObserver(records => {
  for (const record of records) {
    for (const node of record.addedNodes) if (node.nodeType === 1) scan(node);
    for (const node of record.removedNodes) {
      if (node.nodeType !== 1 || node.isConnected) continue;
      if (surfaces.has(node)) resize.unobserve(node);
      node.querySelectorAll(".surface").forEach(host => resize.unobserve(host));
    }
  }
}).observe(document.body, {childList: true, subtree: true});

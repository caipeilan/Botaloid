export const welcomeLyrics = [
  {
    text: "What...Kawaii!",
    title: "は？お前ほんとマジお前分かってねえわマジで一旦とりあえず聞けってマジ",
    author: "足立レイ / d.j.ァネイロ",
  },
  {
    text: "ボカロはダサい",
    title: "ボカロはダサい",
    author: "ピノキオピー / 初音ミク",
  },
  {
    text: "あなたがオムレツなら あたしはカレーライスだよ",
    title: "散歩の邪魔",
    author: "いよわ",
  },
  {
    text: "ABCDEFGHIJKB",
    title: "ミィハー",
    author: "Chinozo / 重音テト",
  },
];

const selected = new WeakMap();

export function renderWelcome(conversation) {
  if (!selected.has(conversation)) selected.set(conversation, welcomeLyrics[Math.floor(Math.random() * welcomeLyrics.length)]);
  const lyric = selected.get(conversation);
  document.getElementById("welcome-lyric").textContent = lyric.text;
  document.getElementById("welcome-credit").textContent = "from 《" + lyric.title + "》 by " + lyric.author;
}

import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {test} from "node:test";

const source = await readFile(new URL("../web/assets/common.js", import.meta.url), "utf8");
const {chat} = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));

function response(events, split = false) {
  const bytes = new TextEncoder().encode(events.map(event => JSON.stringify(event) + "\n").join(""));
  return new Response(new ReadableStream({
    start(controller) {
      if (split) for (let i = 0; i < bytes.length; i += 7) controller.enqueue(bytes.slice(i, i + 7));
      else controller.enqueue(bytes);
      controller.close();
    },
  }));
}

test("partial UTF-8 events produce stages followed by one fixed reply", async () => {
  const result = {fragments: [{text: "月を見上げる。"}], songs: [], intent: "chat", tone: "warm"};
  const stages = [];
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return response([{type: "stage", stage: "planning"}, {type: "stage", stage: "selecting"},
      {type: "result", result}, {type: "result", result: {text: "must not replace the reply"}}], true);
  };
  try {
    assert.deepEqual(await chat({message: "你好"}, stage => stages.push(stage)), result);
    assert.deepEqual(stages, ["planning", "selecting"]);
    assert.equal(calls, 1);
  } finally { globalThis.fetch = original; }
});

test("service failure and incomplete streams do not regenerate", async () => {
  const original = globalThis.fetch;
  try {
    for (const events of [[{type: "stage", stage: "planning"}, {type: "error", detail: "模型格式错误"}],
      [{type: "stage", stage: "planning"}]]) {
      let calls = 0;
      globalThis.fetch = async () => { calls++; return response(events); };
      await assert.rejects(chat({message: "你好"}, () => {}), /模型格式错误|未完成/);
      assert.equal(calls, 1);
    }
  } finally { globalThis.fetch = original; }
});

test("request cancellation is passed to fetch", async () => {
  const original = globalThis.fetch;
  const controller = new AbortController();
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.signal, controller.signal);
    throw new DOMException("Aborted", "AbortError");
  };
  try {
    controller.abort();
    await assert.rejects(chat({message: "你好"}, () => {}, controller.signal), {name: "AbortError"});
  } finally { globalThis.fetch = original; }
});

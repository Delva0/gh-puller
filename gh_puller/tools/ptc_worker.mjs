// Fresh, permission-restricted Node process for one async TypeScript/JavaScript PTC program.
import vm from "node:vm";
import readline from "node:readline";
import { stripTypeScriptTypes } from "node:module";

const input = readline.createInterface({ input: process.stdin });
const pending = new Map();
let started = false, settled = false, sequence = 0, outputBytes = 0;
let maxOutputBytes = 0, maxCalls = 0;

function send(packet) {
  process.stdout.write(JSON.stringify(packet) + "\n");
}

function account(value) {
  outputBytes += Buffer.byteLength(value, "utf8");
  if (outputBytes > maxOutputBytes) throw new Error("PTC output limit exceeded; return fewer fields or excerpts");
}

async function run(config) {
  send({ type: "runtime", version: process.version });
  maxOutputBytes = config.maxOutputBytes;
  maxCalls = config.maxCalls;
  const tools = Object.create(null);
  for (const name of config.tools) {
    tools[name] = (args) => {
      if (settled) return Promise.reject(new Error("PTC program has already settled"));
      if (++sequence > maxCalls) return Promise.reject(new Error("PTC tool-call limit exceeded"));
      return new Promise((resolve, reject) => {
        // Serialize before retaining the promise so malformed input cannot leave a pending binding.
        const packet = JSON.stringify({ type: "call", id: sequence, name, args });
        pending.set(sequence, { resolve, reject });
        process.stdout.write(packet + "\n");
      });
    };
  }
  const console = Object.freeze({ log: (...values) => {
    const text = values.map(value => typeof value === "string" ? value : JSON.stringify(value)).join(" ");
    account(text);
    send({ type: "log", text });
  }});
  const source = stripTypeScriptTypes("(async () => {\n" + config.code + "\n})()");
  const result = await vm.runInNewContext(source, { tools: Object.freeze(tools), console }, {
    filename: "run_code.ts", contextCodeGeneration: { strings: false, wasm: false },
  });
  const encoded = JSON.stringify(result === undefined ? null : result);
  account(encoded);
  settled = true;
  send({ type: "done", result: JSON.parse(encoded) });
}

input.on("line", line => {
  const message = JSON.parse(line);
  if (!started) {
    started = true;
    run(message).catch(error => {
      settled = true;
      send({ type: "done", error: { type: error.name, message: String(error.message).slice(0, 8000) } });
    });
    return;
  }
  const promise = pending.get(message.id);
  if (!promise) return;
  pending.delete(message.id);
  if (message.error) {
    const error = new Error(message.error.message);
    error.name = "ToolCallError";
    error.toolName = message.name;
    error.result = message.result;
    promise.reject(error);
  } else {
    promise.resolve(message.result);
  }
});

process.on("unhandledRejection", error => {
  if (!settled) {
    settled = true;
    send({ type: "done", error: { type: "UnhandledRejection", message: String(error).slice(0, 8000) } });
  }
});

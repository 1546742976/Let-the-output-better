/** 从页面源码里抽出 EXAMPLES 载荷，写成 examples.json 供 Python 参考实现消费。 */

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "web", "app.js"), "utf-8");
const start = source.indexOf("const EXAMPLES = [");
const end = source.indexOf("\n];", start);
if (start < 0 || end < 0) throw new Error("在 app.js 里找不到 EXAMPLES 数组");
const block = source.slice(start + "const EXAMPLES =".length, end + 3).trim().replace(/;$/, "");
const examples = new Function(`return (${block});`)();

writeFileSync(join(here, "examples.json"), JSON.stringify(examples, null, 2), "utf-8");
console.log(`examples=${examples.length} -> sites/dev/examples.json`);

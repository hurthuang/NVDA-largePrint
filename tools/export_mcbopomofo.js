// 匯出小麥注音詞庫：詞\t讀音(空白分隔)\t分數（同詞同讀音取最高分）
// 用法：node tools/export_mcbopomofo.js <McBopomofoWeb 資料夾（build 過）> <輸出 tsv>
const fs = require("fs");
const path = require("path");

const [mcDir, outPath] = process.argv.slice(2);
if (!mcDir || !outPath) {
  console.error("用法：node tools/export_mcbopomofo.js <McBopomofoWeb 資料夾> <輸出 tsv>");
  process.exit(1);
}
// WebData.js 是 `const source = \`{...}\`; exports.webData = JSON.parse(source);`
// 同一個讀音的鍵會出現不只一次，JSON.parse 只留最後一筆，所以自己逐筆讀 source。
const webSrc = fs.readFileSync(path.resolve(mcDir, "dist/src/McBopomofo/WebData.js"), "utf8")
  .replace("exports.webData = JSON.parse(source);", "return source;");
const source = new Function("exports", webSrc)({});
const { BopomofoSyllable } = require(path.resolve(mcDir, "dist/src/Mandarin"));
const HAN = /^[㐀-鿿豈-﫿]+$/;
const best = new Map();
for (const m of source.matchAll(/("(?:[^"\\]|\\.)*"):("(?:[^"\\]|\\.)*")/g)) {
  const key = JSON.parse(m[1]), value = JSON.parse(m[2]);
  if (/^_(kana|punctuation|half|number|letter|numpad|ctrl)_/.test(key) || key.length % 2) continue;
  const rs = [];
  try {
    for (let i = 0; i < key.length; i += 2) rs.push(BopomofoSyllable.FromAbsoluteOrderString(key.substring(i, i + 2)).composedString);
  } catch (e) {
    continue;
  }
  const t = value.split(" ");
  for (let i = 0; i + 1 < t.length; i += 2) {
    const w = t[i];
    if (!HAN.test(w) || [...w].length !== rs.length) continue;
    const k = w + "\t" + rs.join(" ");
    const s = Number(t[i + 1]);
    if (!best.has(k) || best.get(k) < s) best.set(k, s);
  }
}
const out = [];
for (const [k, s] of best) out.push(k + "\t" + s);
fs.writeFileSync(outPath, out.join("\n"), "utf8");
console.log(out.length);

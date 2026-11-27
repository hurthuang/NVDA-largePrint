"""產生大字注音用的資料檔（globalPlugins/largePrint/zhuyin/）。

用法：
  node tools/export_mcbopomofo.js <McBopomofoWeb> mc_words.tsv
  python tools/build_zhuyin.py <bpmfvs 資料夾> mc_words.tsv <BpmfIansui-Regular.ttf>

<bpmfvs 資料夾> 要有 phonetic/phonic_table_Z.txt、ime/poyin_db.txt（或直接放在資料夾裡）。
需要 fontTools（只有產生資料時用，附加元件執行時不用）。

輸出：
  chars.txt  破音字：字\t讀音（空白分隔，順序＝字型的 IVS 順序）\t第 2 個讀音起的字形編號
  poyin.txt  bpmfvs 的破音詞表：字\t第幾讀音\t詞（* 是這個字）
  words.txt  小麥注音詞庫（含破音字、常用的詞）：詞\t分數\t每個字第幾讀音（. 表示不是破音字或沒資料）
  fourth.txt 第一讀音是第四聲的字（一行）
"""
import os
import re
import sys

from fontTools.ttLib import TTFont

MIN_SCORE = -7.0  # 含破音字的詞
MIN_PLAIN = -5.5  # 不含破音字的詞（只用來斷詞）


def find(base, *names):
	for n in names:
		p = os.path.join(base, n)
		if os.path.exists(p):
			return p
	raise SystemExit("找不到 " + " / ".join(names))


def norm(r):
	"""小麥注音輕聲 ˙ 在後 → bpmfvs 在前"""
	return "˙" + r[:-1] if r.endswith("˙") else r


def main():
	bpmfvs, mcPath, fontPath = sys.argv[1:4]
	out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "globalPlugins", "largePrint", "zhuyin")
	os.makedirs(out, exist_ok=True)

	font = TTFont(fontPath)
	uvs = [t for t in font["cmap"].tables if t.format == 14][0].uvsDict
	gid = {}  # (字, 第幾讀音) → 字形編號；E01E1 是第 2 讀音
	for sel, lst in uvs.items():
		k = sel - 0xE01E0
		for cp, name in lst:
			if k >= 1 and name:
				gid[(chr(cp), k)] = font.getGlyphID(name)

	table = {}
	fourth = []  # 第一讀音是第四聲的字（「一」變調要看下一個字的聲調）
	for line in open(find(bpmfvs, "phonetic/phonic_table_Z.txt", "phonic_table_Z.txt"), encoding="utf-8"):
		t = line.rstrip("\n").split("\t")
		if len(t) >= 4 and "ˋ" in t[3] and len(t[0]) == 1 and t[0] <= "￿":
			fourth.append(t[0])
		if len(t) >= 5:
			c, rs = t[0], t[3:]
			if all((c, k) in gid for k in range(1, len(rs))):
				table[c] = rs
	with open(os.path.join(out, "chars.txt"), "w", encoding="utf-8", newline="\n") as f:
		for c, rs in table.items():
			f.write("%s\t%s\t%s\n" % (c, " ".join(rs), " ".join(str(gid[(c, k)]) for k in range(1, len(rs)))))

	n = 0
	with open(os.path.join(out, "poyin.txt"), "w", encoding="utf-8", newline="\n") as f:
		for line in open(find(bpmfvs, "ime/poyin_db.txt", "poyin_db.txt"), encoding="utf-8"):
			m = re.match(r"\[(.)\] (\S+)\t?(.*)", line.rstrip("\n"))
			if not m or m.group(1) not in table or m.group(2) not in table[m.group(1)]:
				continue
			c, r, pats = m.groups()
			for p in pats.split("/"):
				if "*" in p:
					f.write("%s\t%d\t%s\n" % (c, table[c].index(r), p))
					n += 1

	def idx(w, rs):
		return "".join(str(table[c].index(r)) if c in table and r in table[c] else "." for c, r in zip(w, rs))

	def rank(w, s):
		# 同分時：副詞「…地」的地念輕聲，其他取第一讀音多的
		if len(w) >= 3 and w[-1] == "地" and s[-1] == str(table["地"].index("˙ㄉㄜ")):
			return -1
		return sum(9 if ch == "." else int(ch) for ch in s)

	words = {}
	for line in open(mcPath, encoding="utf-8"):
		w, rs, sc = line.rstrip("\n").split("\t")
		sc = float(sc)
		if any(c in table for c in w):
			if sc <= MIN_SCORE:
				continue
		elif len(w) > 1 and sc <= MIN_PLAIN:
			# 沒有破音字的詞只用來斷詞：單字全收（分數），詞只收常用的
			continue
		s = idx(w, [norm(x) for x in rs.split(" ")])
		old = words.get(w)
		if old is None or old[0] < sc or (old[0] == sc and rank(w, s) < rank(w, old[1])):
			words[w] = (sc, s)
	with open(os.path.join(out, "words.txt"), "w", encoding="utf-8", newline="\n") as f:
		for w, (sc, s) in words.items():
			f.write("%s\t%g\t%s\n" % (w, sc, s))
	with open(os.path.join(out, "fourth.txt"), "w", encoding="utf-8", newline="\n") as f:
		f.write("".join(fourth) + "\n")
	print("破音字", len(table), "詞表", n, "詞庫", len(words), "第四聲", len(fourth))


if __name__ == "__main__":
	main()

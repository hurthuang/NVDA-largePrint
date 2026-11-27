# 大字注音：決定每個破音字的讀音，畫出字型裡對應讀音的字形。
# 字型是注音芫荽（Bpmf Iansui，ButTaiwan 注音 IVS 字型規格，OFL）：每個字右邊直排注音，
#   破音字第 2 讀音起用 IVS 選擇（U+E01E1…）。GDI 的 DrawText 不認 IVS，
#   所以破音字改用 ExtTextOutW＋ETO_GLYPH_INDEX 直接畫字型裡的字形（字形編號在 chars.txt）。
# 讀音：
#   1. 自己補的規則（EXTRA，可以跨詞）
#   2. bpmfvs 的破音詞表 poyin.txt（照《國語一字多音審訂表》），詞要落在斷詞的同一個詞裡
#   3. 小麥注音詞庫斷詞（words.txt，分數是 log10 機率）
#   4. 都沒有：字型的第一讀音
#   之後再套課本的標法：「一」變調（「不」不標）、動詞後的「得」輕聲、連接詞「和」ㄏㄢˋ、副詞的「地」輕聲。
# 資料檔由 tools/build_zhuyin.py 產生。

import ctypes
import math
import os
import re
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_FILE = os.path.join(HERE, "BpmfIansui-Regular.ttf")
FACE = "Bpmf Iansui Regular"
HAN_RUN = re.compile("[㐀-鿿豈-﫿]+")
MAXLEN = 6
UNKNOWN = -12.0
NUMERALS = set("〇零一二三四五六七八九十百千萬億兩幾")
TONE_MARKS = "ˊˇˋ"

# 自己補的規則（課本的標法；可以跨斷詞，優先於詞表）
EXTRA_RULES = [
	("長", "ㄓㄤˇ", "*高/*出/*得/*成/*滿/*胖/*肉/*牙/*葉/*芽/*大"),
	("為", "ㄨㄟˋ", "*他/*她/*你/*我/*牠/*它/*您/*大家/*他們/*她們/*你們/*我們/*了/*什麼/*何/*人民/*國"),
	("吃", "ㄔ", "*著/*了/*飯/*東西"),
	("思", "ㄙ", "意*"),
]
# 「得」在這些詞裡念ㄉㄜˊ，其他不在詞首的「得」念輕聲（覺得、跑得、長得）
DE2 = set("獲得 取得 難得 贏得 所得 求得 不得 免得 只得 博得 習得 自得".split())
# 動詞後面接「得到、得出、得完」的得是輕聲（看得到）；「我得到、幸運得到」照念ㄉㄜˊ
VERBS = set("看找做聽買吃拿辦想猜學走跑搬摸趕用睡說寫讀唱跳飛爬記認分聞抓接救追搶查算數畫拉推搬洗穿等打")

_lock = threading.Lock()
_data = None  # (table, gids, poyin, extra, words)


def _load():
	global _data
	with _lock:
		if _data is not None:
			return _data
		table, gids = {}, {}
		with open(os.path.join(HERE, "chars.txt"), encoding="utf-8") as f:
			for line in f:
				c, rs, gs = line.rstrip("\n").split("\t")
				table[c] = rs.split(" ")
				gids[c] = [None] + [int(g) for g in gs.split(" ")]
		poyin = {}
		with open(os.path.join(HERE, "poyin.txt"), encoding="utf-8") as f:
			for line in f:
				c, i, p = line.rstrip("\n").split("\t")
				poyin.setdefault(c, []).append((p, int(i)))
		extra = {}
		for c, r, pats in EXTRA_RULES:
			if c in table and r in table[c]:
				for p in pats.split("/"):
					extra.setdefault(c, []).append((p, table[c].index(r)))
		words = {}
		with open(os.path.join(HERE, "words.txt"), encoding="utf-8") as f:
			for line in f:
				w, sc, s = line.rstrip("\n").split("\t")
				words[w] = (float(sc), s)
		with open(os.path.join(HERE, "fourth.txt"), encoding="utf-8") as f:
			fourth = set(f.read().strip())
		_data = (table, gids, poyin, extra, words, fourth)
		return _data


def preload():
	"""在背景先讀資料（第一次顯示才讀會頓一下）。"""
	threading.Thread(target=_load, daemon=True).start()


def _tone(r):
	if r.startswith("˙"):
		return 5
	for i, m in enumerate(TONE_MARKS):
		if m in r:
			return i + 2
	return 1


def _segment(run, words):
	n = len(run)
	best = [(-math.inf, 0)] * (n + 1)
	best[0] = (0.0, 0)
	for i in range(n):
		if best[i][0] == -math.inf:
			continue
		for L in range(1, min(MAXLEN, n - i) + 1):
			w = run[i:i + L]
			if w in words:
				sc = words[w][0]
			elif L == 1:
				sc = UNKNOWN
			else:
				continue
			v = best[i][0] + sc
			if v > best[i + L][0]:
				best[i + L] = (v, i)
	out, j = [], n
	while j > 0:
		i = best[j][1]
		out.append((i, run[i:j]))
		j = i
	return out[::-1]


def _match(db, text, pos, lo, hi):
	"""最長的詞符合 → 第幾讀音；詞要落在 [lo, hi) 裡"""
	c = text[pos]
	found = None
	for p, i in db.get(c, ()):
		k = p.index("*")
		start = pos - k
		pat = p.replace("*", c)
		if start >= lo and start + len(pat) <= hi and text.startswith(pat, start):
			if found is None or len(pat) > found[0]:
				found = (len(pat), i)
	return found[1] if found else None


def readings(text):
	"""每個字第幾讀音（0＝第一讀音；不是破音字是 None）。"""
	table, gids, poyin, extra, words, fourth = _load()
	res = [None] * len(text)
	for m in HAN_RUN.finditer(text):
		run, base = m.group(), m.start()
		segs = _segment(run, words)
		for j, (i, w) in enumerate(segs):
			info = words[w][1] if w in words else None
			for k, c in enumerate(w):
				if c not in table:
					continue
				pos = base + i + k
				r = _match(extra, text, pos, 0, len(text))
				if r is None:
					r = _match(poyin, text, pos, base + i, base + i + len(w))
				if r is None and info and len(w) > 1 and info[k] != "." and c not in "一不":
					# 詞庫的「一」「不」是變調後的音；課本「不」標本調，「一」下面另外算
					r = int(info[k])
				res[pos] = r or 0
			# 課本的標法
			for k, c in enumerate(w):
				afterVerb = k == 0 and i > 0 and run[i - 1] in VERBS  # 看得到、找得到
				if c == "得" and (k > 0 or afterVerb or (len(w) == 1 and j > 0)) and w not in DE2 and run[max(0, i + k - 1):i + k + 1] not in DE2:
					res[base + i + k] = table["得"].index("˙ㄉㄜ")
			if w == "和" and 0 < j < len(segs) - 1:
				res[base + i] = table["和"].index("ㄏㄢˋ")
			if w == "地" and 0 < j < len(segs) - 1 and len(segs[j - 1][1]) >= 2:
				res[base + i] = table["地"].index("˙ㄉㄜ")
	# 「一」變調：第四聲前ㄧˊ、其他聲調前ㄧˋ；第一、數字、句尾照本調
	one = table["一"]
	for pos, c in enumerate(text):
		if c != "一" or pos + 1 >= len(text):
			continue
		nxt = text[pos + 1]
		prv = text[pos - 1] if pos else ""
		if not HAN_RUN.match(nxt) or prv == "第" or prv in NUMERALS or nxt in NUMERALS or nxt in "月號":
			continue
		if nxt in table:
			four = _tone(table[nxt][res[pos + 1] or 0]) == 4
		else:
			four = nxt in fourth
		res[pos] = one.index("ㄧˊ") if four else one.index("ㄧˋ")
	return res


def glyphs(text):
	"""要換字形的字：{位置: 字形編號}（第一讀音照一般畫法，不在裡面）。"""
	try:
		_table, gids = _load()[:2]
		out = {}
		for pos, r in enumerate(readings(text)):
			if r:
				out[pos] = gids[text[pos]][r]
		return out
	except Exception:
		return {}


# ---- 字型 ----
_fontLoaded = False


def loadFont():
	"""把附的注音字型載入給 NVDA 自己用（不安裝到系統）。"""
	global _fontLoaded
	if _fontLoaded:
		return True
	try:
		FR_PRIVATE = 0x10
		n = ctypes.windll.gdi32.AddFontResourceExW(FONT_FILE, FR_PRIVATE, None)
		_fontLoaded = n > 0
	except Exception:
		_fontLoaded = False
	return _fontLoaded


def unloadFont():
	global _fontLoaded
	if _fontLoaded:
		try:
			ctypes.windll.gdi32.RemoveFontResourceExW(FONT_FILE, 0x10, None)
		except Exception:
			pass
		_fontLoaded = False


_gdi = ctypes.windll.gdi32
_gdi.ExtTextOutW.argtypes = [
	ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p,
	ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
]
_gdi.SetTextColor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_gdi.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
_gdi.SetTextAlign.argtypes = [ctypes.c_void_p, ctypes.c_uint]
ETO_GLYPH_INDEX = 0x10
TRANSPARENT = 1


def drawGlyph(dc, gid, x, y, colour):
	"""在 wx 的 DC 上用字形編號畫一個字（字型要先用 dc.SetFont 設好）。"""
	hdc = dc.GetHDC()
	if not hdc:
		return False
	r, g, b = colour.Red(), colour.Green(), colour.Blue()
	_gdi.SetTextColor(hdc, r | (g << 8) | (b << 16))
	_gdi.SetBkMode(hdc, TRANSPARENT)
	_gdi.SetTextAlign(hdc, 0)  # TA_LEFT | TA_TOP
	arr = (ctypes.c_ushort * 1)(gid)
	return bool(_gdi.ExtTextOutW(hdc, x, y, ETO_GLYPH_INDEX, None, arr, 1, None))

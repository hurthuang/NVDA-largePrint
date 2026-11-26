# 大字顯示 Large Print
# 把 NVDA 要報讀的句子用大字重新畫出來，放大很多倍也清楚。
# 取句子：包住 speech.extensions.filter_speechSequence.apply，在所有 filter 之前抄下原文
#   （WorldVoice 在 filter 裡就套語音字典，pre_speech 拿到的已經是 DictSwitcher 換過的字，例如記號→記浩），
#   再在 pre_speech（語音模式判斷之前，語音關掉也有）時收進來。
# 全螢幕放大鏡會把這個視窗一起放大（又糊），所以開大字時暫停 NVDA 放大鏡，關掉再恢復。
# 只顯示焦點與內容的文字：ui.message 的提示不收；角色、狀態、位置資訊（getPropertiesSpeech 一項一個字串）
#   從內容分出來，預設放在狀態列（設定可改成不顯示、跟內容一起）。
# 大字開著時，翻頁、字大小的按鍵不停語音（包住 KeyboardInputGesture.speechEffectWhenExecuted）。

import collections
import ctypes
import json
import os
import re
import tempfile
import threading
import time
import urllib.request

import addonHandler
import config
import controlTypes
import globalPluginHandler
import gui
import inputCore
import keyboardHandler
import tones
import speech
import ui
import wx
from gui import guiHelper, nvdaControls
from gui.settingsDialogs import NVDASettingsDialog, SettingsPanel
from logHandler import log
from scriptHandler import script
from speech.commands import CallbackCommand
from speech.extensions import filter_speechSequence, pre_speech

CATEGORY = "大字顯示"

POSITIONS = [
	("auto", "自動（避開焦點，放在上方或下方）"),
	("follow", "跟著焦點浮動（放在焦點下方，放不下就上方）"),
	("top", "固定在上方"),
	("bottom", "固定在下方"),
]
SCHEMES = [
	("yellowOnBlack", "黑底黃字", (0, 0, 0), (255, 255, 0)),
	("whiteOnBlack", "黑底白字", (0, 0, 0), (255, 255, 255)),
	("greenOnBlack", "黑底綠字", (0, 0, 0), (0, 255, 0)),
	("blackOnWhite", "白底黑字", (255, 255, 255), (0, 0, 0)),
	("blackOnYellow", "黃底黑字", (255, 255, 0), (0, 0, 0)),
	("whiteOnBlue", "藍底白字", (0, 0, 128), (255, 255, 255)),
]
DEFAULT_FACE = "Microsoft JhengHei"
# 輸入法選字時大字顯示什麼
CANDIDATES = [
	("list", "候選字和編號"),
	("words", "候選字和相關詞（跟 NVDA 念的一樣）"),
]
# 角色、狀態、位置資訊（標題、表格第幾列第幾欄、清單第幾項…）放哪裡
STRUCTURES = [
	("status", "放在狀態列（大字視窗最上面一行的小字）"),
	("hide", "不顯示"),
	("inline", "跟內容一起顯示"),
]

config.conf.spec["largePrint"] = {
	"fontSize": "integer(default=120, min=16, max=800)",
	"heightPercent": "integer(default=50, min=20, max=100)",
	"position": "string(default=auto)",
	"fontFace": f"string(default={DEFAULT_FACE})",
	"colors": "string(default=yellowOnBlack)",
	"speakOnPage": "boolean(default=True)",
	"showOnStartup": "boolean(default=False)",
	"bold": "boolean(default=False)",
	"opacity": "integer(default=80, min=50, max=100)",
	"structure": "string(default=status)",
	"candidateDisplay": "string(default=list)",
}

# ---- 檢查更新（跟作者其他附加元件同一套做法）----
GITHUB_LATEST_API = "https://api.github.com/repos/hurthuang/NVDA-largePrint/releases/latest"
RELEASES_URL = "https://github.com/hurthuang/NVDA-largePrint/releases"
USER_AGENT = "largePrint-update-check"


def _parseVersion(v):
	nums = [int(p) for p in re.findall(r"\d+", v)]
	nums += [0] * (4 - len(nums))
	return tuple(nums[:4])


def _currentVersion():
	try:
		return addonHandler.getCodeAddon().manifest.get("version", "0")
	except Exception:
		return "0"


def _fromStore():
	"""從 NVDA 附加元件商店安裝的，NVDA 會在 addons 資料夾放「<名稱>.json」；這時更新交給商店，不自己查 GitHub。"""
	try:
		return os.path.isfile(addonHandler.getCodeAddon().path + ".json")
	except Exception:
		return False


def _checkUpdateWorker(silent=False):
	"""silent=True 是啟動時自動檢查：沒有新版、連不上網路都不出聲。"""
	if _fromStore():
		if not silent:
			wx.CallAfter(ui.message, "這個附加元件是從 NVDA 附加元件商店安裝的，請到附加元件商店更新。")
		return
	current = _currentVersion()
	req = urllib.request.Request(
		GITHUB_LATEST_API,
		headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
	)
	try:
		with urllib.request.urlopen(req, timeout=10) as resp:
			data = json.loads(resp.read().decode("utf-8"))
	except Exception as e:
		if not silent:
			wx.CallAfter(ui.message, f"檢查更新失敗：{e}")
		return
	if not data.get("tag_name"):
		# 例如 GitHub 查詢次數超過上限，回的是錯誤訊息，不是版本
		if not silent:
			wx.CallAfter(ui.message, f"檢查更新失敗：{data.get('message', '沒有拿到版本資訊')}")
		return
	latest = data["tag_name"].lstrip("vV")
	if _parseVersion(latest) <= _parseVersion(current):
		if not silent:
			wx.CallAfter(ui.message, f"目前已是最新版本（v{current}）。")
		return
	assetUrl = None
	for asset in data.get("assets", []):
		if asset.get("name", "").endswith(".nvda-addon"):
			assetUrl = asset.get("browser_download_url")
			break
	wx.CallAfter(_promptUpdate, latest, current, assetUrl, data.get("html_url", RELEASES_URL))


def _promptUpdate(latest, current, assetUrl, releaseUrl):
	"""主執行緒問使用者；按是才下載，交給 NVDA 原本的安裝流程。"""
	if not assetUrl:
		ui.browseableMessage(
			f"有新版本可更新：v{latest}（目前使用：v{current}）\n\n下載頁面：\n{releaseUrl}",
			"大字顯示有新版本",
		)
		return
	result = gui.messageBox(
		f"大字顯示有新版本 v{latest}（目前使用：v{current}）。\n\n是否立即下載並安裝？",
		"大字顯示有新版本",
		wx.YES_NO | wx.ICON_QUESTION,
	)
	if result == wx.YES:
		ui.message("正在下載更新…")
		threading.Thread(target=_downloadAndInstallWorker, args=(assetUrl,), daemon=True).start()


def _downloadAndInstallWorker(assetUrl):
	req = urllib.request.Request(assetUrl, headers={"User-Agent": USER_AGENT})
	try:
		with urllib.request.urlopen(req, timeout=30) as resp:
			content = resp.read()
	except Exception as e:
		wx.CallAfter(ui.message, f"下載更新失敗：{e}")
		return
	tmpPath = os.path.join(tempfile.gettempdir(), "largePrint-update.nvda-addon")
	try:
		with open(tmpPath, "wb") as f:
			f.write(content)
	except Exception as e:
		wx.CallAfter(ui.message, f"儲存更新檔失敗：{e}")
		return
	# 交給系統開啟，跑 NVDA 原本的附加元件安裝確認
	wx.CallAfter(os.startfile, tmpPath)


def checkForUpdate(silent=False):
	threading.Thread(target=_checkUpdateWorker, kwargs={"silent": silent}, daemon=True).start()


# 不放在行首的標點（放不下時掛在上一行尾）
NO_LINE_START = set("，。、；：！？）」』〉》】,.;:!?)]}…")
TOKEN_RE = re.compile(r"[A-Za-z0-9À-ɏ'’\-]+|\s+|.", re.S)

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x20
WS_EX_TOOLWINDOW = 0x80
WS_EX_LAYERED = 0x80000
WS_EX_NOACTIVATE = 0x08000000
LWA_ALPHA = 0x2
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x10

user32 = ctypes.windll.user32
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowPos.argtypes = [
	ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]

_plugin = None


def conf():
	return config.conf["largePrint"]


def colors():
	key = conf()["colors"]
	for k, _label, bg, fg in SCHEMES:
		if k == key:
			break
	else:
		_k, _label, bg, fg = SCHEMES[0]
	dim = tuple((a + b) // 2 for a, b in zip(bg, fg))
	return wx.Colour(*bg), wx.Colour(*fg), wx.Colour(*dim)


_PLACEHOLDER_RE = re.compile(r"\{\w*\}|%[sd]")


def _fmtRe(fmt, subs=None):
	"""把翻譯好的格式字串變成正規式：{名稱}、%s、%d 換成 subs 裡的式子，沒給的當數字。"""
	subs = subs or {}
	out, pos = [], 0
	for m in _PLACEHOLDER_RE.finditer(fmt):
		out.append(re.escape(fmt[pos : m.start()]))
		name = m.group(0).strip("{}%") if m.group(0).startswith("{") else m.group(0)
		out.append(subs.get(name, r"\d+"))
		pos = m.end()
	out.append(re.escape(fmt[pos:]))
	return "".join(out)


def _plurals(singular, plural):
	"""ngettext 的單數、複數兩種翻譯。"""
	return {ngettext(singular, plural, 1), ngettext(singular, plural, 2)}  # noqa: F821（NVDA 內建）


def _buildNoise():
	"""角色、狀態、位置資訊的念法（NVDA 目前語言），整個字串相同（或符合句型）才算。
	句型照 NVDA speech.py 的 getControlFieldSpeech、getPropertiesSpeech、getTableInfoSpeech 等。"""
	words = set()
	roleTexts = set()
	for role in controlTypes.Role:
		try:
			words.add(role.displayString)
			roleTexts.add(role.displayString)
		except Exception:
			pass
	try:
		import aria

		landmark = controlTypes.Role.LANDMARK.displayString
		for name in aria.landmarkRoles.values():
			words.add(f"{name} {landmark}")
			roleTexts.add(f"{name} {landmark}")
	except Exception:
		pass
	try:
		for cur in controlTypes.IsCurrent:
			if cur != controlTypes.IsCurrent.NO:
				words.add(cur.displayString)
	except Exception:
		pass
	for s in (_("out of table"), _("has details")):
		words.add(s)
	for state in controlTypes.State:
		# 沒有翻譯的狀態會在 nvda.log 記錯誤，先查有沒有標籤
		try:
			if state not in state._displayStringLabels:
				continue
		except Exception:
			continue
		for attr in ("displayString", "negativeDisplayString"):
			try:
				words.add(getattr(state, attr))
			except Exception:
				pass
	words.discard("")
	roleTexts.discard("")
	roles = "(?:" + "|".join(re.escape(r) for r in sorted(roleTexts, key=len, reverse=True)) + ")"
	rowCount = "(?:" + "|".join(_fmtRe(f) for f in _plurals("{rowCount} row", "{rowCount} rows")) + ")"
	colCount = "(?:" + "|".join(_fmtRe(f) for f in _plurals("{columnCount} column", "{columnCount} columns")) + ")"
	pats = [
		_fmtRe(_("{number} of {total}")),
		_fmtRe(_("level %s")),
		_fmtRe(_("heading level %d")),
		_fmtRe(_("row %s")),
		_fmtRe(_("column %s")),
		_fmtRe(_("through {endRow}")),
		_fmtRe(_("through {endCol}")),
		_fmtRe(_("through row {row} column {column}")),
		_fmtRe(_("column {0} of {1}")),
		_fmtRe(_("with {rowCountTranslation} and {colCountTranslation}"),
			{"rowCountTranslation": rowCount, "colCountTranslation": colCount}),
		_fmtRe(_("table with {columnCountText} and {rowCountText}"),
			{"columnCountText": colCount, "rowCountText": rowCount}),
		# 離開清單、離開 導覽 地標（roleText 用空格接起來）
		_fmtRe(_("out of %s"), {"%s": f"{roles}(?: {roles})*"}),
		_fmtRe(_("has %s"), {"%s": roles}),
	]
	for singular, plural in (
		("with %s column", "with %s columns"),
		("with %s row", "with %s rows"),
		("with %s item", "with %s items"),
		("%s column", "%s columns"),
	):
		pats.extend(_fmtRe(f) for f in _plurals(singular, plural))
	return words, re.compile("^(?:" + "|".join(f"(?:{p})" for p in pats) + ")$")


def locateCaret(text, lines, offset):
	"""原文第 offset 個字在斷好的行裡是第幾行第幾個字 (li, ci)。
	wrapText 會把空白縮成一格、拿掉行首空白，所以逐字對回原文。游標在最後面時 ci＝那一行的長度。"""
	p = 0
	for li, line in enumerate(lines):
		for ci, ch in enumerate(line):
			if ch == " ":
				while p < len(text) and not text[p].isspace():
					p += 1
				idx = p
				p += 1
				# 斷行時縮成一格的空白：後面不是空白就把整串吃掉
				if not (ci + 1 < len(line) and line[ci + 1] == " "):
					while p < len(text) and text[p].isspace() and text[p] != "\n":
						p += 1
			else:
				idx = text.find(ch, p)
				if idx < 0:
					return None
				p = idx + 1
			if idx >= offset or (ch == " " and p > offset):
				return li, ci
		# 游標在這一行最後面（後面還有別行）：畫在行尾
		if li + 1 < len(lines):
			j = p
			while j < len(text) and text[j].isspace():
				j += 1
			if offset < j:
				return li, len(line)
	if not lines:
		return None
	return len(lines) - 1, len(lines[-1])


def wrapText(dc, text, maxWidth, keepSpaces=False):
	"""中文逐字、英文以字為單位斷行。keepSpaces：編輯區照原文保留每一個空白（游標才對得上）。"""
	lines = []
	for para in text.split("\n"):
		line, lineW = "", 0
		first = True
		for tok in TOKEN_RE.findall(para):
			if tok.isspace():
				if keepSpaces:
					if not line and not first:
						continue  # 斷行處的空白不放到下一行開頭
					tok = tok.replace("\t", " ")  # Tab 換成空白；全形空白照留（候選字之間）
				else:
					if not line:
						continue
					tok = " "
			first = False
			w = dc.GetTextExtent(tok)[0]
			if lineW + w <= maxWidth or not line:
				if w > maxWidth and len(tok) > 1:
					# 單一個很長的英文字：逐字拆
					for ch in tok:
						cw = dc.GetTextExtent(ch)[0]
						if lineW + cw > maxWidth and line:
							lines.append(line)
							line, lineW = "", 0
						line += ch
						lineW += cw
					continue
				line += tok
				lineW += w
			elif tok in NO_LINE_START:
				line += tok
				lines.append(line)
				line, lineW = "", 0
			else:
				lines.append(line.rstrip())
				if tok.isspace():
					line, lineW = "", 0
				else:
					line, lineW = tok, w
		lines.append(line if keepSpaces else line.rstrip())
	if keepSpaces:
		# 編輯區的空白行也要顯示（游標畫在那一行）
		return lines or [""]
	while lines and not lines[-1].strip():
		lines.pop()
	return lines


def makeFont(px):
	bold = conf()["bold"]
	font = wx.Font(wx.FontInfo(wx.Size(0, px)).FaceName(conf()["fontFace"]).Bold(bold))
	if not font.IsOk():
		font = wx.Font(wx.FontInfo(wx.Size(0, px)).FaceName(DEFAULT_FACE).Bold(bold))
	return font


def _isCandidate(obj):
	try:
		from NVDAObjects.behaviors import CandidateItem

		return isinstance(obj, CandidateItem)
	except Exception:
		return False


def _composing():
	"""輸入法正在組字或選字（焦點是 InputComposition 或候選字）。"""
	try:
		import api
		from NVDAObjects.inputComposition import InputComposition

		focus = api.getFocusObject()
		return isinstance(focus, InputComposition) or _isCandidate(focus)
	except Exception:
		return False


def focusSpan():
	"""游標（或焦點）在螢幕上的上下範圍 (top, bottom)，拿不到就 None。"""
	import api
	import textInfos

	try:
		ti = api.getCaretObject().makeTextInfo(textInfos.POSITION_CARET)
		try:
			ti.expand(textInfos.UNIT_CHARACTER)
			rects = ti.boundingRects
			if rects:
				return min(r.top for r in rects), max(r.bottom for r in rects)
		except Exception:
			pass
		p = ti.pointAtStart
		return p.y, p.y + 24
	except Exception:
		pass
	try:
		loc = api.getFocusObject().location
		if loc:
			return loc.top, loc.top + loc.height
	except Exception:
		pass
	return None


class BigTextFrame(wx.Frame):
	def __init__(self, plugin):
		super().__init__(
			None,
			title="大字顯示",
			style=wx.STAY_ON_TOP | wx.FRAME_NO_TASKBAR | wx.FRAME_TOOL_WINDOW | wx.BORDER_NONE,
		)
		self.plugin = plugin
		self.pageCount = 1
		self.pageLines = []
		self.status = ""
		self.info = ""
		self.caretPos = None  # (這一頁第幾行, 第幾個字)
		self.compMarks = []
		self.selMarks = []
		self._lastY = None
		self.statusPx = 16
		self.lineH = 0
		self.margin = 8
		self.statusH = 0
		self.fontPx = 0
		self.atTop = False
		self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
		self.Bind(wx.EVT_PAINT, self.onPaint)
		hwnd = self.GetHandle()
		ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
		ex |= WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE
		user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)
		user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
		self._alpha = 255

	def _place(self, geo, h):
		"""依設定決定視窗的 y。"""
		mode = conf()["position"]
		top, bottom = geo.y, geo.y + geo.height - h
		if mode == "top":
			return top
		if mode == "bottom":
			return bottom
		if _composing() and self._lastY is not None:
			# 輸入法組字時游標位置不準，視窗留在原地不跳
			return min(max(top, self._lastY), bottom)
		span = focusSpan()
		if mode == "follow" and span:
			fTop, fBottom = span
			gap = max(4, self.margin // 2)
			if fBottom + gap + h <= geo.y + geo.height:
				return max(top, fBottom + gap)
			if fTop - gap - h >= geo.y:
				return fTop - gap - h
			# 上下都放不下：放在空間比較大的那邊
			return top if fTop - geo.y > geo.y + geo.height - fBottom else bottom
		if span:
			self.atTop = (span[0] + span[1]) // 2 > geo.y + geo.height // 2
		return top if self.atTop else bottom

	@staticmethod
	def _marks(text, lines, span, perPage, page):
		marks = []
		if not span or span[1] <= span[0]:
			return marks
		a = locateCaret(text, lines, span[0])
		b = locateCaret(text, lines, span[1])
		if not (a and b):
			return marks
		for li in range(a[0], b[0] + 1):
			if li // perPage != page:
				continue
			c0 = a[1] if li == a[0] else 0
			c1 = b[1] if li == b[0] else len(lines[li])
			if c1 > c0:
				marks.append((li - page * perPage, c0, c1))
		return marks

	def update(self):
		"""重新排版、決定大小與位置，然後重畫。"""
		text, sentStatus, info, caret = self.plugin.currentView()
		self.info = info if conf()["structure"] == "status" else ""
		if not text and caret is None:
			self.Hide()
			return
		geo = wx.Display(0).GetGeometry()
		size = conf()["fontSize"]
		self.fontPx = size
		self.margin = margin = max(8, size // 8)
		dc = wx.ScreenDC()
		dc.SetFont(makeFont(size))
		self.lineH = int(dc.GetTextExtent("國")[1] * 1.15)
		compSpan = selSpan = None
		if isinstance(caret, tuple):
			parts = caret
			caret, compStart, compEnd = parts[:3]
			compSpan = (compStart, compEnd)
			selSpan = parts[3] if len(parts) > 3 else None
			cands = parts[4] if len(parts) > 4 else None
			if cands:
				# 候選字：只放選到的字附近、一行放得下的那幾個，選字移動時跟著捲動
				labels, si, offset = cands
				sep = "　"
				maxW = geo.width - 2 * margin

				def width(a, b):
					return dc.GetTextExtent(sep.join(labels[a:b]))[0]

				start = 0
				while start < si and width(start, si + 1) > maxW:
					start += 1
				end = si + 1
				while end < len(labels) and width(start, end + 1) <= maxW:
					end += 1
				text = text[:offset] + sep.join(labels[start:end])
				s0 = offset + len(sep.join(labels[start:si])) + (1 if si > start else 0)
				selSpan = (s0, s0 + len(labels[si]))
		lines = wrapText(dc, text, geo.width - 2 * margin, keepSpaces=caret is not None)
		self.statusPx = max(16, size // 3)
		dc.SetFont(makeFont(self.statusPx))
		statusLineH = dc.GetTextExtent("國")[1]
		maxH = geo.height * conf()["heightPercent"] // 100
		perPage = max(1, (maxH - 2 * margin - statusLineH) // self.lineH)
		pages = [lines[i : i + perPage] for i in range(0, len(lines), perPage)] or [[]]
		self.pageCount = len(pages)
		page = self.plugin.pageIndex
		loc = locateCaret(text, lines, caret) if caret is not None else None
		if loc and self.plugin._jumpToCaret:
			# 編輯區那一行很長時，翻到游標所在的那一頁
			page = loc[0] // perPage
		self.plugin._jumpToCaret = False
		if page < 0 or page >= len(pages):
			page = len(pages) - 1 if page < 0 else 0
		self.plugin.pageIndex = page
		self.pageLines = pages[page]
		self.caretPos = None
		if loc and loc[0] // perPage == page:
			self.caretPos = (loc[0] - page * perPage, loc[1])
		# 組字中的字畫底線、選到的候選字反白：[(這一頁第幾行, 從第幾個字, 到第幾個字)]
		self.compMarks = self._marks(text, lines, compSpan, perPage, page)
		self.selMarks = self._marks(text, lines, selSpan, perPage, page)
		parts = [s for s in (sentStatus, f"第 {page + 1}／{len(pages)} 頁" if len(pages) > 1 else "") if s]
		self.status = "　".join(parts)
		# 狀態列一直留著：組字的注音、角色資訊一下有一下沒有時，視窗高度才不會跟著變
		self.statusH = statusLineH
		# 同一句各頁高度一樣，翻頁時視窗不跳
		h = min(len(lines), perPage) * self.lineH + 2 * margin + self.statusH
		y = self._lastY = self._place(geo, h)
		alpha = 255 * min(100, max(50, conf()["opacity"])) // 100
		if alpha != self._alpha:
			# 整個視窗（底色和字）一起半透明，看得到後面的畫面
			user32.SetLayeredWindowAttributes(self.GetHandle(), 0, alpha, LWA_ALPHA)
			self._alpha = alpha
		user32.SetWindowPos(self.GetHandle(), HWND_TOPMOST, geo.x, y, geo.width, h, SWP_NOACTIVATE)
		if not self.IsShown():
			self.ShowWithoutActivating()
		self.Refresh()

	def onPaint(self, evt):
		dc = wx.AutoBufferedPaintDC(self)
		bg, fg, dim = colors()
		w, h = self.GetClientSize()
		dc.SetBackground(wx.Brush(bg))
		dc.Clear()
		m = self.margin
		if self.status or self.info:
			dc.SetFont(makeFont(self.statusPx))
			dc.SetTextForeground(dim)
			sw = 0
			if self.status:
				sw = dc.GetTextExtent(self.status)[0]
				dc.DrawText(self.status, w - m - sw, m // 2)
			if self.info:
				# 左邊放角色狀態位置資訊，太長就截掉
				room = w - 2 * m - (sw + self.statusPx if sw else 0)
				info = self.info
				if dc.GetTextExtent(info)[0] > room:
					while info and dc.GetTextExtent(info + "…")[0] > room:
						info = info[:-1]
					info += "…"
				dc.DrawText(info, m, m // 2)
		dc.SetFont(makeFont(self.fontPx))
		dc.SetTextForeground(fg)
		y = m + self.statusH
		for i, line in enumerate(self.pageLines):
			dc.DrawText(line, m, y)
			for li, c0, c1 in self.selMarks:
				if li == i:
					# 選到的候選字：反白
					x0 = m + dc.GetTextExtent(line[:c0])[0]
					seg = line[c0:c1]
					sw, sh = dc.GetTextExtent(seg)
					dc.SetPen(wx.TRANSPARENT_PEN)
					dc.SetBrush(wx.Brush(fg))
					dc.DrawRectangle(x0, y, sw, sh)
					dc.SetTextForeground(bg)
					dc.DrawText(seg, x0, y)
					dc.SetTextForeground(fg)
			for li, c0, c1 in self.compMarks:
				if li == i:
					# 組字中（還沒送出）的字畫底線
					x0 = m + dc.GetTextExtent(line[:c0])[0]
					x1 = m + dc.GetTextExtent(line[:c1])[0]
					th = max(2, self.fontPx // 16)
					dc.SetPen(wx.TRANSPARENT_PEN)
					dc.SetBrush(wx.Brush(fg))
					dc.DrawRectangle(x0, y + self.lineH - th, x1 - x0, th)
			if self.caretPos and self.caretPos[0] == i:
				# 游標的字：反白（底色、字色對調）；游標在行尾就畫一條直線
				ci = self.caretPos[1]
				x = m + dc.GetTextExtent(line[:ci])[0]
				if ci < len(line):
					ch = line[ci]
					cw, chH = dc.GetTextExtent(ch)
					dc.SetPen(wx.TRANSPARENT_PEN)
					dc.SetBrush(wx.Brush(fg))
					dc.DrawRectangle(x, y, max(cw, self.fontPx // 3), chH)
					dc.SetTextForeground(bg)
					dc.DrawText(ch, x, y)
					dc.SetTextForeground(fg)
				else:
					dc.SetPen(wx.TRANSPARENT_PEN)
					dc.SetBrush(wx.Brush(fg))
					dc.DrawRectangle(x + 2, y, max(3, self.fontPx // 12), self.lineH)
			y += self.lineH


class BigTextSettingsPanel(SettingsPanel):
	title = "大字顯示"

	def makeSettings(self, settingsSizer):
		h = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		c = conf()
		self.position = h.addLabeledControl("位置(&P):", wx.Choice, choices=[label for _k, label in POSITIONS])
		keys = [k for k, _label in POSITIONS]
		self.position.SetSelection(keys.index(c["position"]) if c["position"] in keys else 0)
		self.height = h.addLabeledControl(
			"最高佔螢幕的百分比(&H):", nvdaControls.SelectOnFocusSpinCtrl,
			min=20, max=100, initial=c["heightPercent"],
		)
		self.size = h.addLabeledControl(
			"字的大小（像素）(&S):", nvdaControls.SelectOnFocusSpinCtrl,
			min=16, max=800, initial=c["fontSize"],
		)
		faces = sorted({f for f in wx.FontEnumerator.GetFacenames() if not f.startswith("@")}, key=str.lower)
		if c["fontFace"] not in faces:
			faces.insert(0, c["fontFace"])
		self.faces = faces
		self.face = h.addLabeledControl("字型(&F):", wx.Choice, choices=faces)
		self.face.SetSelection(faces.index(c["fontFace"]))
		self.bold = h.addItem(wx.CheckBox(self, label="粗體(&B)"))
		self.bold.SetValue(c["bold"])
		self.scheme = h.addLabeledControl("配色(&C):", wx.Choice, choices=[s[1] for s in SCHEMES])
		skeys = [s[0] for s in SCHEMES]
		self.scheme.SetSelection(skeys.index(c["colors"]) if c["colors"] in skeys else 0)
		self.opacity = h.addLabeledControl(
			"不透明度（百分比，100＝不透明）(&T):", nvdaControls.SelectOnFocusSpinCtrl,
			min=50, max=100, initial=c["opacity"],
		)
		self.structure = h.addLabeledControl(
			"標題層級、表格欄列等位置資訊(&I):", wx.Choice, choices=[label for _k, label in STRUCTURES],
		)
		tkeys = [k for k, _label in STRUCTURES]
		self.structure.SetSelection(tkeys.index(c["structure"]) if c["structure"] in tkeys else 0)
		self.candidate = h.addLabeledControl(
			"輸入法選字時顯示(&W):", wx.Choice, choices=[label for _k, label in CANDIDATES],
		)
		ckeys = [k for k, _label in CANDIDATES]
		self.candidate.SetSelection(ckeys.index(c["candidateDisplay"]) if c["candidateDisplay"] in ckeys else 0)
		self.speakOnPage = h.addItem(wx.CheckBox(self, label="翻頁時語音跟著念這一頁(&R)"))
		self.speakOnPage.SetValue(c["speakOnPage"])
		self.showOnStartup = h.addItem(wx.CheckBox(self, label="NVDA 啟動時開啟大字顯示(&O)"))
		self.showOnStartup.SetValue(c["showOnStartup"])
		updateButton = h.addItem(wx.Button(self, label="檢查更新(&U)"))
		updateButton.Bind(wx.EVT_BUTTON, lambda evt: checkForUpdate(silent=False))

	def onSave(self):
		c = conf()
		c["position"] = POSITIONS[self.position.GetSelection()][0]
		c["heightPercent"] = self.height.GetValue()
		c["fontSize"] = self.size.GetValue()
		c["fontFace"] = self.faces[self.face.GetSelection()]
		c["colors"] = SCHEMES[self.scheme.GetSelection()][0]
		c["bold"] = self.bold.GetValue()
		c["opacity"] = self.opacity.GetValue()
		c["structure"] = STRUCTURES[self.structure.GetSelection()][0]
		c["candidateDisplay"] = CANDIDATES[self.candidate.GetSelection()][0]
		c["speakOnPage"] = self.speakOnPage.GetValue()
		c["showOnStartup"] = self.showOnStartup.GetValue()
		if _plugin:
			wx.CallAfter(_plugin._refresh)


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	scriptCategory = CATEGORY

	# 只有大字開著時才有的熱鍵（字大小沿用放大鏡的放大縮小鍵，放大鏡此時暫停）
	_shownGestures = {
		"kb:NVDA+shift+=": "fontBigger",
		"kb:NVDA+shift+-": "fontSmaller",
		"kb:NVDA+shift+pageUp": "previous",
		"kb:NVDA+shift+pageDown": "next",
	}

	def __init__(self):
		global _plugin
		super().__init__()
		_plugin = self
		self.history = collections.deque(maxlen=200)
		self.browseIndex = None  # None＝跟著最新的句子
		self.pageIndex = 0  # -1＝最後一頁
		self.frame = None
		self.shown = False
		self._suppress = 0
		self._magWasOn = False
		self._raw = None
		self._lineTimer = None
		self._pendingInfo = ""
		self._pendingFallback = ""
		self._lastLine = None  # 上一次顯示的編輯區那一行（TextInfo），判斷是不是同一行
		self._jumpToCaret = False  # 下次排版時翻到游標那一頁
		self._editLine = None  # 最近一次顯示的編輯區那一行 (文字, 游標)，選字時放在上面
		self._candEntry = False  # 最新一句是選字的顯示
		self._focusAt = 0.0
		self._roleWords = set()
		for role in controlTypes.Role:
			try:
				self._roleWords.add(role.displayString)
			except Exception:
				pass
		self._roleWords.discard("")
		self._quietIds = {inputCore.normalizeGestureIdentifier(g) for g in self._shownGestures}
		try:
			self._noiseWords, self._noiseRe = _buildNoise()
		except Exception:
			log.debugWarning("largePrint: 角色狀態清單建立失敗", exc_info=True)
			self._noiseWords, self._noiseRe = set(), re.compile(r"(?!)")

		# NVDA 的提示訊息不收進大字
		self._origUiMessage = ui.message

		def message(*args, **kwargs):
			self._suppress += 1
			try:
				return self._origUiMessage(*args, **kwargs)
			finally:
				self._suppress -= 1

		ui.message = message

		# 在所有 filter（含 WorldVoice 套語音字典）之前抄下原文
		self._origApply = filter_speechSequence.apply

		def apply(value, **kwargs):
			if not isinstance(value, list):
				value = list(value)
			if not self._suppress:
				try:
					value = self._markSegments(value)
				except Exception:
					log.debugWarning("largePrint: 分段失敗", exc_info=True)
			return self._origApply(value, **kwargs)

		try:
			filter_speechSequence.apply = apply
		except Exception:
			log.debugWarning("largePrint: 無法在 filter 之前取原文", exc_info=True)

		# 大字開著時，翻頁、字大小的按鍵不停語音
		cls = keyboardHandler.KeyboardInputGesture
		self._origSpeechEffect = cls.__dict__.get("speechEffectWhenExecuted")
		origProp = cls.speechEffectWhenExecuted

		def speechEffect(gesture):
			try:
				if self.shown:
					if self._quietIds.intersection(gesture.normalizedIdentifiers):
						return None
					# NVDA+Shift+翻頁：按下 NVDA 鍵、Shift 時先不停語音，等後面的鍵決定
					# （不是翻頁鍵的話，那個鍵照樣會停語音；單按 Ctrl 仍然可以停語音）
					if gesture.isNVDAModifierKey or gesture.vkCode in (0x10, 0xA0, 0xA1):
						return None
			except Exception:
				pass
			return origProp.fget(gesture)

		try:
			cls.speechEffectWhenExecuted = property(speechEffect)
		except Exception:
			log.debugWarning("largePrint: 無法讓翻頁鍵不停語音", exc_info=True)

		pre_speech.register(self.onPreSpeech)
		NVDASettingsDialog.categoryClasses.append(BigTextSettingsPanel)
		# 啟動後背景檢查一次更新（延遲幾秒，不搶 NVDA 啟動）；有新版才問，沒有就不出聲
		wx.CallLater(5000, checkForUpdate, True)
		# 啟動時開啟：等 NVDA 啟動完成（放大鏡已經啟動）才開，才能暫停放大鏡
		if conf()["showOnStartup"]:
			try:
				import core

				core.postNvdaStartup.register(self._showOnStartup)
			except Exception:
				wx.CallLater(3000, self._showOnStartup)

	def _showOnStartup(self):
		try:
			import core

			core.postNvdaStartup.unregister(self._showOnStartup)
		except Exception:
			pass
		if not self.shown:
			self._show()

	def terminate(self):
		global _plugin
		pre_speech.unregister(self.onPreSpeech)
		if self._lineTimer:
			self._lineTimer.Stop()
			self._lineTimer = None
		ui.message = self._origUiMessage
		try:
			del filter_speechSequence.apply
		except AttributeError:
			pass
		cls = keyboardHandler.KeyboardInputGesture
		if self._origSpeechEffect is not None:
			cls.speechEffectWhenExecuted = self._origSpeechEffect
		else:
			try:
				del cls.speechEffectWhenExecuted
			except AttributeError:
				pass
		try:
			NVDASettingsDialog.categoryClasses.remove(BigTextSettingsPanel)
		except ValueError:
			pass
		if self.frame:
			self.frame.Destroy()
			self.frame = None
		_plugin = None
		super().terminate()

	# ---- 取句子 ----
	def _markSegments(self, seq):
		"""用原文（還沒經過任何 filter）切段：遇到 CallbackCommand（讀出全部每一塊開頭的 lineReached）就分段。
		每段前面插一個自己的 CallbackCommand，語音念到那裡才換大字，讀出全部時才會跟著跑。"""
		mode = conf()["structure"]
		segments = []  # (插入位置, 內容, 角色狀態位置資訊)
		parts, infos = [], []
		start = 0

		def flush():
			if parts or (infos and mode == "status"):
				segments.append((start, " ".join(parts), " ".join(infos)))

		for i, item in enumerate(seq):
			if isinstance(item, CallbackCommand):
				flush()
				parts, infos = [], []
				start = i + 1
			elif isinstance(item, str):
				s = item.strip()
				if not s:
					continue
				if mode != "inline" and (s in self._noiseWords or self._noiseRe.match(s)):
					infos.append(s)
				else:
					parts.append(s)
		flush()
		self._raw = [(t, info) for _i, t, info in segments]
		if not segments:
			return seq
		out = list(seq)
		for pos, text, info in reversed(segments):
			out.insert(pos, CallbackCommand(lambda t=text, f=info: self._spoken(t, f), name="largePrint:segment"))
		return out

	def _speechIsAudible(self):
		"""語音真的會念出來、而且語音引擎會回報念到哪裡。"""
		try:
			if speech.getState().speechMode != speech.SpeechMode.talk:
				return False
			import synthDriverHandler

			synth = synthDriverHandler.getSynth()
			return synthDriverHandler.synthIndexReached in synth.supportedNotifications
		except Exception:
			return False

	def onPreSpeech(self, speechSequence=None, **kwargs):
		segments, self._raw = self._raw, None
		if self._suppress or not segments:
			return
		if self._speechIsAudible():
			return  # 念到每一段時由 CallbackCommand 換大字
		# 語音關掉、隨選模式、語音引擎不回報進度：直接顯示
		if self._editLineMode() and not self._isFocusSpeech(segments[-1][1]):
			text, info = segments[-1]
			self._scheduleCaretLine(info, text)
			return
		for text, info in segments:
			self._addText(text, info)
		if self.shown:
			wx.CallAfter(self._refresh)

	def _spoken(self, text, info=""):
		if self._suppress:
			return
		if self._editLineMode() and not self._isFocusSpeech(info):
			# 編輯區（打字、輸入法送字、方向鍵）：不顯示念的那一個字，改顯示游標那一行
			self._scheduleCaretLine(info, text)
			return
		self._addText(text, info)
		if self.shown:
			self._refresh()

	def _addText(self, text, info="", caret=None):
		"""history 每一項是 (內容, 角色狀態位置資訊, 游標位置或 None)。"""
		if not text:
			# 只有資訊（例如按空白鍵後的「未勾選」）：是在講目前這一項，更新最新一句的狀態列
			if self.history:
				t, _info, c = self.history[-1]
				self.history[-1] = (t, info, c)
				self.browseIndex = None
			return
		self._lastLine = None
		self._candEntry = False
		if self.history and self.history[-1][0] == text and self.browseIndex is None:
			self.history[-1] = (text, info or self.history[-1][1], caret)
		else:
			self.history.append((text, info, caret))
		self.browseIndex = None
		self.pageIndex = 0

	# ---- 編輯區：顯示游標那一行 ----
	def _editFocus(self):
		"""目前在編輯區（不是瀏覽模式、不是密碼欄、不在讀出全部）就傳回編輯區物件，不然 None。
		輸入法組字時焦點是 InputComposition，用它的 parent（文件本身）。"""
		try:
			import api

			if api.isTypingProtected():
				return None
			try:
				from speech.sayAll import SayAllHandler

				if SayAllHandler.isRunning():
					return None  # 讀出全部照原本逐段跟著
			except Exception:
				pass
			focus = api.getFocusObject()
			try:
				from NVDAObjects.inputComposition import InputComposition

				if isinstance(focus, InputComposition):
					focus = focus.parent
			except Exception:
				pass
			ti = focus.treeInterceptor
			if ti is not None and not getattr(ti, "passThrough", True):
				return None
			# Word 文件的角色是「文件」、沒有「可編輯」狀態，所以也看 NVDA 的 EditableText 類別
			try:
				from editableText import EditableText

				if isinstance(focus, EditableText):
					return focus
			except Exception:
				pass
			if controlTypes.State.EDITABLE in focus.states or focus.role == controlTypes.Role.EDITABLETEXT:
				return focus
		except Exception:
			pass
		return None

	def _editLineMode(self):
		return self._editFocus() is not None

	def _isFocusSpeech(self, info):
		"""有角色（「編輯區」「按鈕」…）的是換焦點時念的，照原本顯示（看得到欄位名稱）。"""
		return bool(info) and any(w in self._roleWords for w in info.split())

	def event_gainFocus(self, obj, nextHandler):
		if _isCandidate(obj):
			# 選字：NVDA 念的字詞解釋不收，改顯示文件那一行＋這一頁的候選字
			self._suppress += 1
			try:
				nextHandler()
			finally:
				self._suppress -= 1
			try:
				self._showCandidates(obj)
			except Exception:
				log.debugWarning("largePrint: 顯示候選字失敗", exc_info=True)
			return
		wasCandidate = self._candEntry
		self._focusAt = time.time()
		if not wasCandidate:
			self._lastLine = None
			nextHandler()
		else:
			# 選好字回到文件時 NVDA 可能再念一次文件名稱，不收
			self._suppress += 1
			try:
				nextHandler()
			finally:
				self._suppress -= 1
		if wasCandidate and self._editLineMode():
			# 選好字回到文件：顯示送出後的那一行
			self._scheduleCaretLine()

	def event_foreground(self, obj, nextHandler):
		# 選字框出現時 NVDA 會念輸入法視窗的名稱（「Windows 輸入體驗」），不收
		if _isCandidate(obj):
			self._suppress += 1
			try:
				nextHandler()
			finally:
				self._suppress -= 1
			return
		nextHandler()

	def _showCandidates(self, obj):
		mode = conf()["candidateDisplay"]
		info = ""
		sel = None
		cands = None
		if mode == "words":
			second = obj.name or ""
			if obj.description:
				second += ", " + obj.description
			sel = None
		else:
			# 這一頁看得到的候選字：1家　2加　3佳…（照 NVDA 的 visibleCandidateItemsText 走法）
			before, after = [], []
			o = obj.previous
			while _isCandidate(o) and isinstance(o.candidateNumber, int) and controlTypes.State.INVISIBLE not in o.states:
				before.append(o)
				o = o.previous
			o = obj.next
			while _isCandidate(o) and isinstance(o.candidateNumber, int) and controlTypes.State.INVISIBLE not in o.states:
				after.append(o)
				o = o.next
			items = list(reversed(before)) + [obj] + after
			parts = []
			pos = 0
			for i, it in enumerate(items):
				label = f"{it.candidateNumber}{it.basicText}" if isinstance(it.candidateNumber, int) else it.basicText
				if it is obj:
					sel = (pos, pos + len(label))
					selIndex = i
				parts.append(label)
				pos += len(label) + 1
			second = "\u3000".join(parts)
			cands = (parts, selIndex)
			# 選到的字的第一個相關詞（例如「加油的加」）放狀態列，分辨同音字
			info = self._firstDescription(obj.basicText)
		# 上面一行：文件那一行（組字畫底線）；下面一行：候選字
		first, caretValue = self._editLine if self._editLine else ("", None)
		if first.strip() or caretValue is not None:
			text = first + "\n" + second
			offset = len(first) + 1
		else:
			text, offset = second, 0
			caretValue = None
		if isinstance(caretValue, tuple):
			c, cs, ce = caretValue[:3]
		elif caretValue is not None:
			c, cs, ce = caretValue, 0, 0
		else:
			c, cs, ce = len(text), 0, 0
		selSpan = (sel[0] + offset, sel[1] + offset) if sel else None
		# 候選字一行放不下時，由排版只取選到的字附近放得下的那幾個（跟著捲動，文件那一行一直留著）
		value = (c, cs, ce, selSpan, (cands[0], cands[1], offset) if cands else None)
		if self._candEntry or (self._lastLine is not None and self.history and self.browseIndex is None):
			self.history[-1] = (text, info, value)
		elif self.history and self.browseIndex is None and self._editLine and self.history[-1][0] == first:
			self.history[-1] = (text, info, value)
		else:
			self.history.append((text, info, value))
		self._candEntry = True
		self.browseIndex = None
		self.pageIndex = 0
		self._jumpToCaret = False
		self._refresh()

	@staticmethod
	def _firstDescription(candidate):
		if not candidate or len(candidate) != 1:
			return ""
		try:
			import characterProcessing

			descs = characterProcessing.getCharacterDescription(speech.getCurrentLanguage(), candidate) or []
		except Exception:
			return ""
		if not descs:
			return ""
		d = descs[0]
		if d.startswith("(") and d.endswith(")"):
			d = d[1:-1]
		return d

	def _scheduleCaretLine(self, info="", fallback=""):
		"""等程式把字放進去、游標移好再讀那一行；連續的只讀最後一次。"""
		if info:
			self._pendingInfo = info
		if fallback:
			self._pendingFallback = fallback
		try:
			if self._lineTimer:
				self._lineTimer.Stop()
			self._lineTimer = wx.CallLater(80, self._showCaretLine)
		except Exception:
			log.debugWarning("largePrint: 排游標行失敗", exc_info=True)

	def event_typedCharacter(self, obj, nextHandler, ch):
		nextHandler()
		if self._editLineMode():
			self._scheduleCaretLine()

	def event_caret(self, obj, nextHandler):
		# 打字、輸入法送字、方向鍵都會移動游標（打字回音、游標朗讀關掉時也跟著）
		nextHandler()
		# 剛換焦點時的游標事件不算，保留 NVDA 念的欄位名稱
		if time.time() - self._focusAt > 0.5 and self._editLineMode():
			self._scheduleCaretLine()

	def _showCaretLine(self):
		self._lineTimer = None
		info, fallback = self._pendingInfo, self._pendingFallback
		self._pendingInfo = self._pendingFallback = ""
		obj = self._editFocus()
		if obj is None:
			if fallback:
				self._addText(fallback, info)
				self._refresh()
			return
		try:
			import textInfos

			caret = obj.makeTextInfo(textInfos.POSITION_CARET)
			line = caret.copy()
			line.expand(textInfos.UNIT_LINE)
			text = line.text.rstrip("\r\n")
			before = line.copy()
			before.setEndPoint(caret, "endToStart")
			offset = len(before.text)
		except Exception:
			log.debugWarning("largePrint: 讀游標那一行失敗", exc_info=True)
			if fallback:
				self._addText(fallback, info)
				self._refresh()
			return
		# 輸入法組字中的注音、字（跟點字一樣）放進大字：
		# Word 這類會把組字放進文件裡的，標出那一段；組字在另外小視窗的，插在游標的位置
		caretValue = offset
		try:
			import api
			from NVDAObjects.inputComposition import InputComposition

			comp = api.getFocusObject()
			if isinstance(comp, InputComposition):
				s = comp.readingString if comp.isReading else comp.compositionString
				s = s.strip("\u3000")
				if s:
					lo = max(0, offset - len(s))
					k = text.find(s, lo, offset + len(s))
					if k >= 0:
						start = k
					else:
						text = text[:offset] + s + text[offset:]
						start = offset
					sel = comp.readingSelectionOffsets if comp.isReading else comp.compositionSelectionOffsets
					pos = sel[0] if 0 < sel[0] <= len(s) else len(s)
					caretValue = (start + pos, start, start + len(s))
		except Exception:
			log.debugWarning("largePrint: 讀組字失敗", exc_info=True)
		self._editLine = (text, caretValue)
		if not text.strip():
			# 空白行（例如剛按 Enter）：大字顯示空的一行和游標，NVDA 念的「空白」放狀態列
			info = f"{info} {fallback}".strip() if fallback else (info or "空白行")
		# 同一行（打字、左右移動）只更新最新一句，不灌滿回顧；換行才算新的一句
		sameLine = False
		if self._lastLine is not None and self.history and self.browseIndex is None:
			try:
				sameLine = line.compareEndPoints(self._lastLine, "startToStart") == 0
			except Exception:
				sameLine = False
		if sameLine or (self._candEntry and self.history and self.browseIndex is None):
			self.history[-1] = (text, info, caretValue)
		else:
			self.history.append((text, info, caretValue))
		self._lastLine = line
		self._candEntry = False
		self.browseIndex = None
		self.pageIndex = 0
		self._jumpToCaret = True
		self._refresh()

	def currentView(self):
		"""(內容, 句數, 角色狀態位置資訊, 游標位置)"""
		if not self.history:
			return "", "", "", None
		if self.browseIndex is None:
			text, info, caret = self.history[-1]
			return text, "", info, caret
		text, info, caret = self.history[self.browseIndex]
		return text, f"第 {self.browseIndex + 1}／{len(self.history)} 句", info, caret

	def _refresh(self):
		if self.frame and self.shown:
			self.frame.update()

	def _message(self, text):
		"""自己的提示（ui.message 已經不收進大字）。"""
		ui.message(text)

	# ---- NVDA 放大鏡 ----
	def _pauseMagnifier(self):
		self._magWasOn = False
		try:
			import _magnifier

			if _magnifier.isActive():
				_magnifier.stop(persist=False)
				self._magWasOn = True
		except Exception:
			log.debugWarning("largePrint: 暫停放大鏡失敗", exc_info=True)

	def _resumeMagnifier(self):
		if not self._magWasOn:
			return
		self._magWasOn = False
		try:
			import _magnifier

			_magnifier.start()
		except Exception:
			log.debugWarning("largePrint: 恢復放大鏡失敗", exc_info=True)

	# ---- 開關 ----
	def _show(self):
		self._pauseMagnifier()
		if not self.frame:
			self.frame = BigTextFrame(self)
		self.browseIndex = None
		self.pageIndex = 0
		self.shown = True
		self.frame.update()
		for g, s in self._shownGestures.items():
			self.bindGesture(g, s)

	def _hide(self):
		self.shown = False
		if self.frame:
			self.frame.Hide()
		for g in self._shownGestures:
			try:
				self.removeGestureBinding(g)
			except LookupError:
				pass
		self._resumeMagnifier()

	@script(description="開關大字顯示", gesture="kb:NVDA+shift+z")
	def script_toggle(self, gesture):
		if self.shown:
			self._hide()
			self._message("大字關")
		else:
			self._show()
			self._message("大字開")

	@script(description="大字顯示：檢查更新")
	def script_checkUpdate(self, gesture):
		checkForUpdate(silent=False)

	@script(description="大字顯示：字放大")
	def script_fontBigger(self, gesture):
		self._resize(1.15)

	@script(description="大字顯示：字縮小")
	def script_fontSmaller(self, gesture):
		self._resize(1 / 1.15)

	def _resize(self, factor):
		size = int(round(conf()["fontSize"] * factor))
		size = min(800, max(16, size))
		conf()["fontSize"] = size
		self.pageIndex = 0
		self._refresh()

	@script(description="大字顯示：往前（上一頁，到第一頁再到上一句）")
	def script_previous(self, gesture):
		if not self.history:
			return
		if self.pageIndex > 0:
			self.pageIndex -= 1
			self._turned()
			return
		cur = len(self.history) - 1 if self.browseIndex is None else self.browseIndex
		if cur == 0:
			tones.beep(220, 60)
			return
		self.browseIndex = cur - 1
		self.pageIndex = -1
		self._turned()

	@script(description="大字顯示：往後（下一頁，到最後一頁再到下一句）")
	def script_next(self, gesture):
		if not self.history:
			return
		pageCount = self.frame.pageCount if self.frame else 1
		if self.pageIndex < pageCount - 1:
			self.pageIndex += 1
			self._turned()
			return
		if self.browseIndex is None:
			# 已經是最新一句：語音跟著大字時，讓 NVDA 往下讀文件的下一行
			if not (conf()["speakOnPage"] and self._readNextLine()):
				tones.beep(220, 60)
			return
		self.browseIndex += 1
		if self.browseIndex >= len(self.history) - 1:
			self.browseIndex = None
		self.pageIndex = 0
		self._turned()

	def _readNextLine(self):
		"""在文件裡（瀏覽模式、編輯區）把游標移到下一行並念出來，大字跟著更新。
		不模擬下方向鍵：使用者還按著 Shift，編輯區會變成選取文字。"""
		try:
			import api
			import cursorManager
			import textInfos

			focus = api.getFocusObject()
			ti = focus.treeInterceptor
			inDoc = (
				(ti is not None and not getattr(ti, "passThrough", True))
				or controlTypes.State.EDITABLE in focus.states
				or focus.role in (controlTypes.Role.DOCUMENT, controlTypes.Role.EDITABLETEXT)
			)
			if not inDoc:
				return False
			obj = api.getCaretObject()
			info = obj.makeTextInfo(textInfos.POSITION_CARET)
			info.collapse()
			if not info.move(textInfos.UNIT_LINE, 1):
				tones.beep(220, 60)  # 已經是最後一行
				return True
			# UIA（Word、新版記事本）的 move 會把範圍撐成一整行，要收合，不然 updateCaret 會選取整行
			info.collapse()
			if isinstance(obj, cursorManager.CursorManager):
				obj.selection = info
			else:
				info.updateCaret()
			line = info.copy()
			line.expand(textInfos.UNIT_LINE)
			speech.cancelSpeech()
			speech.speakTextInfo(line, unit=textInfos.UNIT_LINE, reason=controlTypes.OutputReason.CARET)
			return True
		except Exception:
			log.debugWarning("largePrint: 往下讀失敗", exc_info=True)
			return False

	def _turned(self):
		"""翻頁或換句之後：重畫；設定開著時語音改念這一頁。"""
		self._refresh()
		if not conf()["speakOnPage"] or not self.frame:
			return
		text = ""
		for line in self.frame.pageLines:
			# 英文在行尾斷開的地方補回空格
			if text and text[-1].isascii() and text[-1].isalnum() and line[:1].isascii() and line[:1].isalnum():
				text += " "
			text += line
		if not text:
			return
		self._suppress += 1
		try:
			speech.cancelSpeech()
			speech.speakText(text)
		finally:
			self._suppress -= 1

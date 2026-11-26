# 大字顯示 Large Print
# 把 NVDA 要報讀的句子用大字重新畫出來，放大很多倍也清楚。
# 取句子：包住 speech.extensions.filter_speechSequence.apply，在所有 filter 之前抄下原文
#   （WorldVoice 在 filter 裡就套語音字典，pre_speech 拿到的已經是 DictSwitcher 換過的字，例如記號→記浩），
#   再在 pre_speech（語音模式判斷之前，語音關掉也有）時收進來。
# 全螢幕放大鏡會把這個視窗一起放大（又糊），所以開大字時暫停 NVDA 放大鏡，關掉再恢復。
# 只顯示焦點與內容的文字：ui.message 的提示不收；角色、狀態、位置資訊（getPropertiesSpeech 一項一個字串）濾掉。
# 大字開著時，翻頁、字大小的按鍵不停語音（包住 KeyboardInputGesture.speechEffectWhenExecuted）。

import collections
import ctypes
import re

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

config.conf.spec["largePrint"] = {
	"fontSize": "integer(default=120, min=16, max=800)",
	"heightPercent": "integer(default=50, min=20, max=100)",
	"position": "string(default=auto)",
	"fontFace": f"string(default={DEFAULT_FACE})",
	"colors": "string(default=yellowOnBlack)",
	"speakOnPage": "boolean(default=True)",
	"showOnStartup": "boolean(default=False)",
}


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


def _buildNoise():
	"""角色、狀態的念法（NVDA 目前語言），整個字串相同才濾掉。"""
	words = set()
	for role in controlTypes.Role:
		try:
			words.add(role.displayString)
		except Exception:
			pass
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
	pats = []
	for fmt, keys in ((_("{number} of {total}"), ("{number}", "{total}")), (_("level %s"), ("%s",))):
		p = re.escape(fmt)
		for k in keys:
			p = p.replace(re.escape(k), r"\d+")
		pats.append(p)
	return words, re.compile("^(?:" + "|".join(pats) + ")$")


def wrapText(dc, text, maxWidth):
	"""中文逐字、英文以字為單位斷行。"""
	lines = []
	for para in text.split("\n"):
		line, lineW = "", 0
		for tok in TOKEN_RE.findall(para):
			if tok.isspace():
				if not line:
					continue
				tok = " "
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
				if tok == " ":
					line, lineW = "", 0
				else:
					line, lineW = tok, w
		lines.append(line.rstrip())
	while lines and not lines[-1]:
		lines.pop()
	return lines


def makeFont(px):
	font = wx.Font(wx.FontInfo(wx.Size(0, px)).FaceName(conf()["fontFace"]))
	if not font.IsOk():
		font = wx.Font(wx.FontInfo(wx.Size(0, px)).FaceName(DEFAULT_FACE))
	return font


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

	def _place(self, geo, h):
		"""依設定決定視窗的 y。"""
		mode = conf()["position"]
		top, bottom = geo.y, geo.y + geo.height - h
		if mode == "top":
			return top
		if mode == "bottom":
			return bottom
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

	def update(self):
		"""重新排版、決定大小與位置，然後重畫。"""
		text, sentStatus = self.plugin.currentView()
		if not text:
			self.Hide()
			return
		geo = wx.Display(0).GetGeometry()
		size = conf()["fontSize"]
		self.fontPx = size
		self.margin = margin = max(8, size // 8)
		dc = wx.ScreenDC()
		dc.SetFont(makeFont(size))
		self.lineH = int(dc.GetTextExtent("國")[1] * 1.15)
		lines = wrapText(dc, text, geo.width - 2 * margin)
		self.statusPx = max(16, size // 3)
		dc.SetFont(makeFont(self.statusPx))
		statusLineH = dc.GetTextExtent("國")[1]
		maxH = geo.height * conf()["heightPercent"] // 100
		perPage = max(1, (maxH - 2 * margin - statusLineH) // self.lineH)
		pages = [lines[i : i + perPage] for i in range(0, len(lines), perPage)] or [[]]
		self.pageCount = len(pages)
		page = self.plugin.pageIndex
		if page < 0 or page >= len(pages):
			page = len(pages) - 1 if page < 0 else 0
		self.plugin.pageIndex = page
		self.pageLines = pages[page]
		parts = [s for s in (sentStatus, f"第 {page + 1}／{len(pages)} 頁" if len(pages) > 1 else "") if s]
		self.status = "　".join(parts)
		self.statusH = statusLineH if self.status else 0
		# 同一句各頁高度一樣，翻頁時視窗不跳
		h = min(len(lines), perPage) * self.lineH + 2 * margin + self.statusH
		y = self._place(geo, h)
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
		if self.status:
			dc.SetFont(makeFont(self.statusPx))
			dc.SetTextForeground(dim)
			sw = dc.GetTextExtent(self.status)[0]
			dc.DrawText(self.status, w - m - sw, m // 2)
		dc.SetFont(makeFont(self.fontPx))
		dc.SetTextForeground(fg)
		y = m + self.statusH
		for line in self.pageLines:
			dc.DrawText(line, m, y)
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
		self.scheme = h.addLabeledControl("配色(&C):", wx.Choice, choices=[s[1] for s in SCHEMES])
		skeys = [s[0] for s in SCHEMES]
		self.scheme.SetSelection(skeys.index(c["colors"]) if c["colors"] in skeys else 0)
		self.speakOnPage = h.addItem(wx.CheckBox(self, label="翻頁時語音跟著念這一頁(&R)"))
		self.speakOnPage.SetValue(c["speakOnPage"])
		self.showOnStartup = h.addItem(wx.CheckBox(self, label="NVDA 啟動時開啟大字顯示(&O)"))
		self.showOnStartup.SetValue(c["showOnStartup"])

	def onSave(self):
		c = conf()
		c["position"] = POSITIONS[self.position.GetSelection()][0]
		c["heightPercent"] = self.height.GetValue()
		c["fontSize"] = self.size.GetValue()
		c["fontFace"] = self.faces[self.face.GetSelection()]
		c["colors"] = SCHEMES[self.scheme.GetSelection()][0]
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
		segments = []  # (插入位置, 文字)
		parts = []
		start = 0
		for i, item in enumerate(seq):
			if isinstance(item, CallbackCommand):
				if parts:
					segments.append((start, " ".join(parts)))
				parts = []
				start = i + 1
			elif isinstance(item, str):
				s = item.strip()
				if s and s not in self._noiseWords and not self._noiseRe.match(s):
					parts.append(s)
		if parts:
			segments.append((start, " ".join(parts)))
		self._raw = [t for _i, t in segments]
		if not segments:
			return seq
		out = list(seq)
		for pos, text in reversed(segments):
			out.insert(pos, CallbackCommand(lambda t=text: self._spoken(t), name="largePrint:segment"))
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
		for text in segments:
			self._addText(text)
		if self.shown:
			wx.CallAfter(self._refresh)

	def _spoken(self, text):
		if self._suppress:
			return
		self._addText(text)
		if self.shown:
			self._refresh()

	def _addText(self, text):
		if not (self.history and self.history[-1] == text and self.browseIndex is None):
			self.history.append(text)
		self.browseIndex = None
		self.pageIndex = 0

	def currentView(self):
		if not self.history:
			return "", ""
		if self.browseIndex is None:
			return self.history[-1], ""
		return self.history[self.browseIndex], f"第 {self.browseIndex + 1}／{len(self.history)} 句"

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

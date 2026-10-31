# -*- coding: utf-8 -*-
# The ElevenLabs TTS category in NVDA's settings, and shortcuts for the ElevenLabs synthesizer:
#   NVDA+Shift+U: report the credit usage for the current period.
#   NVDA+Shift+V: check again which voices and models this account can use,
#     e.g. after changing plan.
#   NVDA+Shift+T: switch the synthesizer for navigation on and off.
# All can be changed in Input Gestures, under "ElevenLabs speech synthesizer".

import json
import locale
import threading
import time
from urllib import request as urlrequest

import globalPluginHandler
import globalVars
import queueHandler
import synthDriverHandler
import ui
import wx
import gui
from gui import guiHelper
from gui.settingsDialogs import NVDASettingsDialog, SettingsDialog, SettingsPanel
from logHandler import log
from scriptHandler import script
from synthDriverHandler import getSynth
from synthDrivers import _elevenlabsNavigation as _navigation
from synthDrivers import _elevenlabsSettings as _settings

try:
	import addonHandler

	# Installs _ for the add-on's own translations in locale.
	addonHandler.initTranslation()
except Exception:
	log.debugWarning("ElevenLabs: translations unavailable", exc_info=True)

# Translators: category of the add-on's commands in Input Gestures.
CATEGORY = _("ElevenLabs speech synthesizer")
# Translators: title of the dialog for the API key, and of messages about the key.
KEY_TITLE = _("ElevenLabs API Key")


def _sayFromThread(text):
	"""Thread safe reporting: ui.message must run on the main thread."""
	queueHandler.queueFunction(queueHandler.eventQueue, ui.message, text)


def _formatNumber(value):
	"""Groups digits the way the user's locale does."""
	try:
		return locale.format_string("%d", value, grouping=True)
	except Exception:
		return str(value)


def _elevenLabs():
	"""The ElevenLabs driver if it is NVDA's synthesizer, else None."""
	synth = getSynth()
	return synth if synth is not None and synth.name == "elevenlabs" else None


def _setNavigationSynth(name):
	"""Saves name as the synthesizer for navigation (None for none) and loads it
	if ElevenLabs is running. False if it couldn't be loaded."""
	with _settings.editing() as settings:
		if name:
			settings["navigationSynth"] = name
			settings["lastNavigationSynth"] = name
		else:
			settings.pop("navigationSynth", None)
	synth = _elevenLabs()
	return synth.useNavigationSynth(name) if synth is not None else True


# MessageDialog was added in NVDA 2025.1; older versions use gui.messageBox.
try:
	from gui.message import DefaultButton, DialogType, MessageDialog, ReturnCode
except ImportError:
	MessageDialog = None


def _showKeyError(parent, message):
	if MessageDialog is not None:
		MessageDialog(
			parent, message, KEY_TITLE, dialogType=DialogType.ERROR, buttons=(DefaultButton.OK,)
		).ShowModal()
	else:
		gui.messageBox(message, KEY_TITLE, wx.OK | wx.ICON_ERROR, parent)


def _confirm(parent, message):
	"""True if the user answers yes to message."""
	if MessageDialog is not None:
		return MessageDialog.confirm(message, KEY_TITLE, parent=parent) == ReturnCode.OK
	return gui.messageBox(message, KEY_TITLE, wx.YES_NO | wx.ICON_QUESTION, parent) == wx.YES


class _ApiKeyDialog(SettingsDialog):
	"""Asks for the API key and only accepts one ElevenLabs takes."""

	title = KEY_TITLE

	def makeSettings(self, settingsSizer):
		helper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		self._keyEdit = helper.addLabeledControl(
			# Translators: label of the text box the API key is pasted into.
			_("&API key:"),
			wx.TextCtrl,
			style=wx.TE_PASSWORD,
		)
		# Shown here, as NVDA's progress dialog moves focus away when it closes.
		self._status = helper.addItem(
			# Translators: shown and spoken while ElevenLabs checks the API key.
			wx.StaticText(self, label=_("Checking the API key with ElevenLabs...")),
		)
		self._status.Hide()
		self._checking = False
		self._cancelled = False

	def postInit(self):
		self._keyEdit.SetFocus()

	def onOk(self, evt):
		if self._checking:
			# Enter pressed again; the running check decides.
			return
		key = self._keyEdit.GetValue().strip()
		if not key:
			# Translators: shown when OK is pressed without an API key.
			self._showError(_("Paste your ElevenLabs API key first."))
			return
		self._checking = True
		self._showStatus(True)
		ui.message(self._status.GetLabel())
		# The check goes over the network, so NVDA must not wait for it.
		threading.Thread(
			target=self._check,
			args=(key,),
			name="ElevenLabsKeyCheck",
			daemon=True,
		).start()

	def _check(self, key):
		try:
			from synthDrivers.elevenlabs import checkApiKey

			error = checkApiKey(key)
		except Exception:
			log.error("ElevenLabs: could not check the API key", exc_info=True)
			# Translators: shown when checking the API key fails unexpectedly.
			error = _("The API key could not be checked. See the NVDA log for details.")
		wx.CallAfter(self._onChecked, key, error)

	def onCancel(self, evt):
		# Destroyed later, so a check still running must not save the key.
		self._cancelled = True
		super().onCancel(evt)

	def _onChecked(self, key, error):
		if not self or self._cancelled:
			# Cancelled meanwhile, so the key isn't saved.
			return
		self._checking = False
		self._showStatus(False)
		if error is None and not _settings.setApiKey(key):
			# Translators: shown when a working API key can't be saved.
			error = _("The API key could not be saved. See the NVDA log for details.")
		if error is not None:
			self._showError(error)
			return
		synth = _elevenLabs()
		if synth is not None:
			# A new key may be what ElevenLabs was missing when it was given up.
			synth.retryAfterFallBack()
		super().onOk(None)

	def _showStatus(self, show):
		self._status.Show(show)
		self.Layout()
		self.Fit()

	def _showError(self, message):
		_showKeyError(self, message)
		self._keyEdit.SetFocus()
		self._keyEdit.SelectAll()


class ElevenLabsSettingsPanel(SettingsPanel):
	# Translators: name of the add-on's category in NVDA's settings.
	title = _("ElevenLabs TTS")

	def makeSettings(self, settingsSizer):
		helper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		self._keyButton = helper.addItem(wx.Button(self))
		self._keyButton.Bind(wx.EVT_BUTTON, self._onKeyButton)
		self._updateKeyButton()
		saved = _settings.get("navigationSynth")
		# None first: ElevenLabs speaks everything. NVDA's own list, so only
		# synthesizers that can run here are offered.
		synths = [
			(name, description) for name, description in synthDriverHandler.getSynthList()
			if name != "elevenlabs"
		]
		self._navigationNames = [None] + [name for name, description in synths]
		labels = [
			# Translators: the choice in the ElevenLabs TTS settings for using no other synthesizer.
			_("None (ElevenLabs speaks everything)"),
		] + [description for name, description in synths]
		if saved and saved not in self._navigationNames:
			# E.g. uninstalled since; kept rather than silently dropped.
			self._navigationNames.append(saved)
			labels.append(saved)
		self._navigationList = helper.addLabeledControl(
			# Translators: label of the box in the ElevenLabs TTS settings choosing the synthesizer
			# that speaks everything except say all, so only say all uses ElevenLabs credits.
			_("&Synthesizer for navigation:"),
			wx.Choice,
			choices=labels,
		)
		self._navigationList.SetSelection(self._navigationNames.index(saved) if saved else 0)

	def _updateKeyButton(self):
		if _settings.getApiKey() is not None:
			# Translators: button in the ElevenLabs TTS settings that removes the API key.
			label = _("Clear &API key")
		else:
			# Translators: button in the ElevenLabs TTS settings that opens the API key dialog.
			label = _("Set &API key...")
		self._keyButton.SetLabel(label)
		self.Layout()

	def _onKeyButton(self, evt):
		if _settings.getApiKey() is None:
			self._setKey()
		else:
			self._clearKey()

	def _setKey(self):
		if _ApiKeyDialog(self).ShowModal() == wx.ID_OK:
			self._updateKeyButton()

	def _clearKey(self):
		if not _confirm(
			self,
			# Translators: asked before the API key is removed.
			_("Clear the ElevenLabs API key? ElevenLabs can't speak until a new key is set."),
		):
			return
		if not _settings.clearApiKey():
			# Translators: shown when the API key can't be removed.
			_showKeyError(self, _("The API key could not be cleared. See the NVDA log for details."))
			return
		self._updateKeyButton()
		synth = getSynth()
		if synth is not None and synth.name == "elevenlabs":
			# Without a key it can't speak, so NVDA mustn't be left silent.
			synth._fallBack("the API key was cleared.")

	def onSave(self):
		# Key changes apply at once; only the synthesizer is left to save.
		name = self._navigationNames[self._navigationList.GetSelection()]
		if name != _settings.get("navigationSynth"):
			_setNavigationSynth(name)


class GlobalPlugin(globalPluginHandler.GlobalPlugin):

	def __init__(self):
		super().__init__()
		if not globalVars.appArgs.secure:
			NVDASettingsDialog.categoryClasses.append(ElevenLabsSettingsPanel)

	def terminate(self):
		if ElevenLabsSettingsPanel in NVDASettingsDialog.categoryClasses:
			NVDASettingsDialog.categoryClasses.remove(ElevenLabsSettingsPanel)
		super().terminate()

	@script(
		# Translators: description of a command in Input Gestures.
		description=_("Report ElevenLabs credit usage for the current period"),
		gesture="kb:NVDA+shift+u",
		category=CATEGORY,
	)
	def script_reportUsage(self, gesture):
		try:
			from synthDrivers.elevenlabs import API_BASE
		except ImportError:
			# Translators: spoken when the add-on's driver is missing.
			ui.message(_("Could not find the ElevenLabs driver"))
			return
		key = _settings.getApiKey()
		if not key:
			# Translators: spoken when no API key has been entered.
			ui.message(_("No API key found"))
			return
		# Translators: spoken while the credit usage is fetched.
		ui.message(_("Fetching usage"))
		# The network call must run in a background thread so NVDA never freezes.
		threading.Thread(
			target=self._fetchAndReportUsage,
			args=(API_BASE, key),
			name="ElevenLabsUsageFetch",
			daemon=True,
		).start()

	def _fetchAndReportUsage(self, apiBase, key):
		try:
			req = urlrequest.Request(
				f"{apiBase}/v1/user/subscription",
				headers={
					"xi-api-key": key,
					"Accept": "application/json",
					# The credit count changes with every utterance, so no cache
					# may answer, or one figure repeats for a whole session.
					"Cache-Control": "no-cache, no-store, max-age=0",
					"Pragma": "no-cache",
					"If-None-Match": "",
				},
			)
			# A fresh connection each time, so no keep alive can replay a response.
			opener = urlrequest.build_opener()
			with opener.open(req, timeout=10) as resp:
				data = json.loads(resp.read().decode("utf-8"))
		except Exception:
			log.error("Could not fetch ElevenLabs usage", exc_info=True)
			# Translators: spoken when the credit usage could not be fetched.
			_sayFromThread(_("Could not fetch usage, see the NVDA log"))
			return
		used = data.get("character_count")
		limit = data.get("character_limit")
		if used is None or limit is None:
			# Translators: spoken when ElevenLabs answers with something unexpected.
			_sayFromThread(_("Unexpected response from ElevenLabs"))
			return
		# Logged so a figure that never moves can be told apart from a figure the
		# API itself is repeating.
		log.debug(f"ElevenLabs usage fetched at {time.strftime('%H:%M:%S')}: {used} of {limit}")
		if limit:
			# Translators: credit usage, e.g. "Credits used: 1,200 of 10,000 (12%)."
			parts = [_("Credits used: {used} of {limit} ({percent}%).").format(
				used=_formatNumber(used),
				limit=_formatNumber(limit),
				percent=round(used / limit * 100),
			)]
		else:
			# Translators: credit usage when the plan has no limit.
			parts = [_("Credits used: {used}.").format(used=_formatNumber(used))]
		resetUnix = data.get("next_character_count_reset_unix")
		if resetUnix:
			when = time.strftime("%Y-%m-%d %H:%M", time.localtime(resetUnix))
			# Translators: when the credits reset, e.g. "Resets: 2026-11-01 14:30."
			parts.append(_("Resets: {when}.").format(when=when))
		# The plan decides which audio formats, voices and models are allowed.
		tier = data.get("tier")
		if tier:
			# Translators: the account's plan, e.g. "Plan: creator."
			parts.append(_("Plan: {tier}.").format(tier=tier))
		_sayFromThread(" ".join(parts))

	@script(
		# Translators: description of a command in Input Gestures.
		description=_("Check again which ElevenLabs voices and models this account can use"),
		gesture="kb:NVDA+shift+v",
		category=CATEGORY,
	)
	def script_recheckVoices(self, gesture):
		synth = getSynth()
		if synth is not None and synth.name == "elevenlabs":
			if synth.recheck(onFinished=self._reportCheck):
				# Translators: spoken when a check of voices and models starts.
				ui.message(_("Checking voices and models"))
			else:
				# Translators: spoken when a check is asked for during another one.
				ui.message(_("A check is already running"))
			return
		# Another synthesizer is in use, so there is nothing to check against.
		# Forgetting the recorded results makes selecting ElevenLabs check again.
		if _settings.invalidateChecks():
			# Translators: spoken when another synthesizer is in use.
			ui.message(_("Voices and models will be checked next time you select ElevenLabs"))
		else:
			# Translators: spoken when the settings file could not be written.
			ui.message(_("Could not update the ElevenLabs settings file"))

	@script(
		# Translators: description of a command in Input Gestures.
		description=_("Switches between a separate synthesizer for navigation and ElevenLabs for all speech"),
		gesture="kb:NVDA+shift+t",
		category=CATEGORY,
	)
	def script_toggleNavigationSynth(self, gesture):
		if _settings.get("navigationSynth"):
			name = None
		else:
			name = _settings.get("lastNavigationSynth")
			if not name:
				# Translators: spoken when switching to a navigation synthesizer before one was ever chosen.
				ui.message(_("No navigation synthesizer chosen; choose one in ElevenLabs TTS settings"))
				return
		if not _setNavigationSynth(name):
			# The driver has said what went wrong instead.
			return
		if name is None:
			# Translators: spoken when ElevenLabs goes back to speaking everything.
			ui.message(_("Using ElevenLabs for all speech."))
			return
		synth = _elevenLabs()
		description = synth.navigationSynthDescription if synth is not None else None
		# Translators: spoken when a separate synthesizer takes over everything but say all,
		# e.g. "Using eSpeak NG for navigation."
		ui.message(_("Using {synth} for navigation.").format(synth=description or _navigation.describe(name)))

	@staticmethod
	def _reportCheck(voices, unusableVoices, models, unusableModels):
		"""Called from the checking thread once every voice and model has been tried."""
		# The lists are already up to date; only a settings dialog that was open
		# throughout still shows the old ones.
		_sayFromThread(
			# Translators: result of a check; every placeholder is a count.
			_(
				"Check finished. Usable voices: {voices}. Unusable voices: {unusableVoices}. "
				"Usable models: {models}. Unusable models: {unusableModels}."
			).format(
				voices=voices,
				unusableVoices=unusableVoices,
				models=models,
				unusableModels=unusableModels,
			)
		)

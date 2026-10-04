# -*- coding: utf-8 -*-
# Shortcuts for the ElevenLabs synthesizer:
#   NVDA+Shift+U: report the credit usage for the current period.
#   NVDA+Shift+V: check again which voices and models this account can use,
#     e.g. after changing plan.
# Both can be changed in Input Gestures, under "ElevenLabs speech synthesizer".

import json
import locale
import threading
import time
from urllib import request as urlrequest

import globalPluginHandler
import queueHandler
import ui
from logHandler import log
from scriptHandler import script

try:
	import addonHandler

	# Installs _ for the add-on's own translations in locale.
	addonHandler.initTranslation()
except Exception:
	log.debugWarning("ElevenLabs: translations unavailable", exc_info=True)

# Translators: category of the add-on's commands in Input Gestures.
CATEGORY = _("ElevenLabs speech synthesizer")


def _sayFromThread(text):
	"""Thread safe reporting: ui.message must run on the main thread."""
	queueHandler.queueFunction(queueHandler.eventQueue, ui.message, text)


def _formatNumber(value):
	"""Groups digits the way the user's locale does."""
	try:
		return locale.format_string("%d", value, grouping=True)
	except Exception:
		return str(value)


class GlobalPlugin(globalPluginHandler.GlobalPlugin):

	def __init__(self):
		super().__init__()
		# Runs on every start, whichever synthesizer is in use, so the file for
		# the API key is there even for a copied add-on or a deleted file.
		try:
			from synthDrivers.elevenlabs import _ensureSettingsFile

			_ensureSettingsFile()
		except Exception:
			# Never let this stop NVDA from starting.
			log.error("ElevenLabs: could not prepare the settings file", exc_info=True)

	@script(
		# Translators: description of a command in Input Gestures.
		description=_("Report ElevenLabs credit usage for the current period"),
		gesture="kb:NVDA+shift+u",
		category=CATEGORY,
	)
	def script_reportUsage(self, gesture):
		try:
			from synthDrivers.elevenlabs import API_BASE, _readApiKey
		except ImportError:
			# Translators: spoken when the add-on's driver is missing.
			ui.message(_("Could not find the ElevenLabs driver"))
			return
		key = _readApiKey()
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
		import synthDriverHandler

		synth = synthDriverHandler.getSynth()
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
		try:
			from synthDrivers.elevenlabs import invalidateChecks
		except ImportError:
			# Translators: spoken when the add-on's driver is missing.
			ui.message(_("Could not find the ElevenLabs driver"))
			return
		if invalidateChecks():
			# Translators: spoken when another synthesizer is in use.
			ui.message(_("Voices and models will be checked next time you select ElevenLabs"))
		else:
			# Translators: spoken when the settings file could not be written.
			ui.message(_("Could not update the ElevenLabs settings file"))

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

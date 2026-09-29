# -*- coding: utf-8 -*-
# Shortcuts for the ElevenLabs synthesizer:
#   NVDA+Shift+U: report the credit usage for the current period.
#   NVDA+Shift+V: check again which voices and models this account can use,
#     e.g. after changing plan.
# Both can be changed in Input Gestures, under "ElevenLabs speech synthesizer".

import json
import threading
import time
from urllib import request as urlrequest

import globalPluginHandler
import queueHandler
import ui
from logHandler import log
from scriptHandler import script

CATEGORY = "ElevenLabs speech synthesizer"


def _sayFromThread(text):
	"""Thread safe reporting: ui.message must run on the main thread."""
	queueHandler.queueFunction(queueHandler.eventQueue, ui.message, text)


def _formatNumber(value):
	return f"{value:,}"


def _describeCheck(usable, unusable, noun):
	if unusable:
		return f"{usable} {noun} usable, {unusable} unusable"
	return f"all {usable} {noun} usable"


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
		description="Report ElevenLabs credit usage for the current period",
		gesture="kb:NVDA+shift+u",
		category=CATEGORY,
	)
	def script_reportUsage(self, gesture):
		try:
			from synthDrivers.elevenlabs import API_BASE, _readApiKey
		except ImportError:
			ui.message("Could not find the ElevenLabs driver")
			return
		key = _readApiKey()
		if not key:
			ui.message("No API key found")
			return
		ui.message("Fetching usage")
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
			_sayFromThread("Could not fetch usage, see the NVDA log")
			return
		used = data.get("character_count")
		limit = data.get("character_limit")
		if used is None or limit is None:
			_sayFromThread("Unexpected response from ElevenLabs")
			return
		# Logged so a figure that never moves can be told apart from a figure the
		# API itself is repeating.
		log.debug(f"ElevenLabs usage fetched at {time.strftime('%H:%M:%S')}: {used} of {limit}")
		parts = [f"Used {_formatNumber(used)} of {_formatNumber(limit)} credits"]
		if limit:
			parts.append(f"{round(used / limit * 100)} percent")
		resetUnix = data.get("next_character_count_reset_unix")
		if resetUnix:
			resetText = time.strftime("%Y-%m-%d at %H:%M", time.localtime(resetUnix))
			parts.append(f"resets {resetText}")
		# The plan decides which audio formats, voices and models are allowed.
		tier = data.get("tier")
		if tier:
			parts.append(f"plan {tier}")
		_sayFromThread(", ".join(parts))

	@script(
		description="Check again which ElevenLabs voices and models this account can use",
		gesture="kb:NVDA+shift+v",
		category=CATEGORY,
	)
	def script_recheckVoices(self, gesture):
		import synthDriverHandler

		synth = synthDriverHandler.getSynth()
		if synth is not None and synth.name == "elevenlabs":
			if synth.recheck(onFinished=self._reportCheck):
				ui.message("Checking voices and models")
			else:
				ui.message("A check is already running")
			return
		# Another synthesizer is in use, so there is nothing to check against.
		# Forgetting the recorded results makes selecting ElevenLabs check again.
		try:
			from synthDrivers.elevenlabs import invalidateChecks
		except ImportError:
			ui.message("Could not find the ElevenLabs driver")
			return
		if invalidateChecks():
			ui.message("Voices and models will be checked next time you select ElevenLabs")
		else:
			ui.message("Could not update the ElevenLabs settings file")

	@staticmethod
	def _reportCheck(voices, unusableVoices, models, unusableModels):
		"""Called from the checking thread once every voice and model has been tried."""
		# The lists are already up to date; only a settings dialog that was open
		# throughout still shows the old ones.
		_sayFromThread(
			f"Check finished: {_describeCheck(voices, unusableVoices, 'voices')}; "
			f"{_describeCheck(models, unusableModels, 'models')}"
		)

# -*- coding: utf-8 -*-
# Shortcuts for the ElevenLabs synthesizer:
#   NVDA+Shift+U: report the credit usage for the current period.
#   NVDA+Shift+V: check again which voices this account can use, e.g. after
#     changing plan.
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


class GlobalPlugin(globalPluginHandler.GlobalPlugin):

	def __init__(self):
		super().__init__()
		# Runs on every NVDA start, whichever synthesizer is in use, so the file
		# to paste the API key into is always there. The add-on's install task
		# also creates it, but only at install time, which misses an add-on
		# folder copied in by hand, a portable copy, or a file deleted since.
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
					# The credit count changes with every utterance, so nothing
					# between NVDA and the API may answer from a cache. Without
					# this the same figure can be repeated for a whole session.
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
		# The plan decides which audio formats and voices are allowed.
		tier = data.get("tier")
		if tier:
			parts.append(f"plan {tier}")
		_sayFromThread(", ".join(parts))

	@script(
		description="Check again which ElevenLabs voices this account can use",
		gesture="kb:NVDA+shift+v",
		category=CATEGORY,
	)
	def script_recheckVoices(self, gesture):
		import synthDriverHandler

		synth = synthDriverHandler.getSynth()
		if synth is not None and synth.name == "elevenlabs":
			if synth.recheckVoices(onFinished=self._reportVoiceCheck):
				ui.message("Checking voices")
			else:
				ui.message("A voice check is already running")
			return
		# Another synthesizer is in use, so there is no voice list to check
		# against. Forgetting the recorded result is enough: selecting ElevenLabs
		# checks every voice whenever that record is missing.
		try:
			from synthDrivers.elevenlabs import invalidateVoiceCheck
		except ImportError:
			ui.message("Could not find the ElevenLabs driver")
			return
		if invalidateVoiceCheck():
			ui.message("Voices will be checked next time you select ElevenLabs")
		else:
			ui.message("Could not update the ElevenLabs settings file")

	@staticmethod
	def _reportVoiceCheck(usable, unusable):
		"""Called from the checking thread once every voice has been tried."""
		if unusable:
			text = f"Voice check finished, {usable} usable, {unusable} unusable"
		else:
			text = f"Voice check finished, all {usable} voices usable"
		# The list itself is already up to date. Only a settings dialog that was
		# open throughout keeps the old one, since its panel is built on opening.
		_sayFromThread(f"{text}")

# -*- coding: utf-8 -*-
# The synthesizer that speaks for ElevenLabs outside say all, so moving around
# doesn't use up credits. ElevenLabs stays NVDA's synthesizer throughout and
# hands speech to this one, which is loaded the way NVDA loads its own, with
# the settings NVDA's config has for it, and kept loaded so that switching
# between the two costs nothing.
#
# The leading underscore keeps NVDA from listing this module as a synthesizer.

import config
import synthDriverHandler
from logHandler import log
from synthDriverHandler import synthDoneSpeaking, synthIndexReached

# Speaks instead when the chosen synthesizer can't be loaded.
FALLBACK_SYNTH = "espeak"


def describe(name):
	"""The name NVDA shows for synthesizer name."""
	try:
		for driverName, description in synthDriverHandler.getSynthList():
			if driverName == name:
				return description
	except Exception:
		log.debugWarning("ElevenLabs: could not list synthesizers", exc_info=True)
	return name


class NavigationSynth:
	def __init__(self, owner):
		# The ElevenLabs driver. NVDA's speech manager ignores notifications from
		# any synthesizer but its own, so this one's are passed on as the owner's.
		self._owner = owner
		self.synth = None
		self._registered = False

	def load(self, name):
		"""Loads name, or eSpeak if it can't be; returns the name loaded, or None."""
		self.unload()
		candidates = (name,) if name == FALLBACK_SYNTH else (name, FALLBACK_SYNTH)
		for candidate in candidates:
			try:
				# As NVDA's setSynth does, so the driver reads its settings from config.conf.
				synth = synthDriverHandler.getSynthInstance(candidate)
			except Exception:
				log.error(f"ElevenLabs: could not load {candidate} for navigation", exc_info=True)
				continue
			self.synth = synth
			self._register()
			log.info(f"ElevenLabs: {synth.name} speaks outside say all.")
			return synth.name
		return None

	def unload(self):
		synth, self.synth = self.synth, None
		self._unregister()
		if synth is None:
			return
		try:
			synth.cancel()
			synth.terminate()
		except Exception:
			log.error(f"ElevenLabs: could not unload {synth.name}", exc_info=True)

	def _register(self):
		if self._registered:
			return
		synthIndexReached.register(self._onIndexReached)
		synthDoneSpeaking.register(self._onDoneSpeaking)
		config.post_configProfileSwitch.register(self._onProfileSwitch)
		self._registered = True

	def _unregister(self):
		if not self._registered:
			return
		synthIndexReached.unregister(self._onIndexReached)
		synthDoneSpeaking.unregister(self._onDoneSpeaking)
		config.post_configProfileSwitch.unregister(self._onProfileSwitch)
		self._registered = False

	def _onIndexReached(self, synth=None, index=None):
		if synth is not None and synth is self.synth:
			synthIndexReached.notify(synth=self._owner, index=index)

	def _onDoneSpeaking(self, synth=None):
		if synth is not None and synth is self.synth:
			synthDoneSpeaking.notify(synth=self._owner)

	def _onProfileSwitch(self):
		# NVDA only applies a new profile's voice settings to its own synthesizer.
		synth = self.synth
		if synth is None:
			return
		try:
			synth.loadSettings(onlyChanged=True)
		except Exception:
			log.error(f"ElevenLabs: could not apply the profile's settings to {synth.name}", exc_info=True)

# -*- coding: utf-8 -*-
# The synthesizer that speaks for ElevenLabs outside say all, so moving around
# doesn't use up credits. ElevenLabs stays NVDA's synthesizer throughout and
# hands speech to this one, which is loaded the way NVDA loads its own, with
# the settings NVDA's config has for it, and kept loaded so that switching
# between the two costs nothing. NVDA's settings ring then holds both
# synthesizers' settings.
#
# The leading underscore keeps NVDA from listing this module as a synthesizer.

import config
import globalVars
import queueHandler
import speechDictHandler
import synthDriverHandler
from autoSettingsUtils.driverSetting import BooleanDriverSetting, NumericDriverSetting
from logHandler import log
from synthDriverHandler import synthDoneSpeaking, synthIndexReached
from synthSettingsRing import BooleanSynthSetting, StringSynthSetting, SynthSetting, SynthSettingsRing

try:
	import addonHandler

	# Installs _ for the add-on's own translations in locale.
	addonHandler.initTranslation()
except Exception:
	log.debugWarning("ElevenLabs: translations unavailable", exc_info=True)

# Speaks instead when the chosen synthesizer can't be loaded.
FALLBACK_SYNTH = "espeak"


class _CombinedSettingsRing(SynthSettingsRing):
	"""NVDA's settings ring with ElevenLabs' settings and then the navigation
	synthesizer's, each named after its synthesizer.

	NVDA keeps a single ring and rebuilds it for whichever synthesizer changes
	voice, which every synthesizer does as it loads, so this takes the ring's
	place: every rebuild, whoever asks for it, comes here. Without a navigation
	synthesizer it is NVDA's ring as usual, so switching synthesizers just works."""

	def __init__(self, navigation):
		self._navigation = navigation
		self._current = None
		self._combined = False
		self.updateSupportedSettings(navigation.owner)

	def updateSupportedSettings(self, synth):
		owner = self._navigation.owner
		navigation = self._navigation.synth
		if navigation is None:
			self._combined = False
			super().updateSupportedSettings(synth)
			return
		# Kept by synthesizer as well as setting, as both have a rate, for example.
		previous = None
		if self._combined and self._current is not None and self.settings:
			previous = self._key(self.settings[self._current])
		settings = [
			self._ringSetting(driver, setting)
			for driver in (owner, navigation)
			for setting in driver.supportedSettings
			if setting.availableInSettingsRing
		]
		self._combined = True
		self.settings = settings or None
		keys = [self._key(setting) for setting in settings]
		if previous in keys:
			self._current = keys.index(previous)
		elif settings:
			# What is heard while adjusting is the navigation synthesizer.
			navigationKeys = [key for key in keys if key[0] is navigation]
			start = (navigation, "rate") if (navigation, "rate") in keys else (navigationKeys or keys)[0]
			self._current = keys.index(start)
		else:
			self._current = None
		if synth is owner:
			# A new ElevenLabs voice makes NVDA load its voice dictionary right after
			# this, but the navigation synthesizer's applies while it is loaded.
			queueHandler.queueFunction(queueHandler.eventQueue, _loadVoiceDict, navigation)

	def _get_currentSettingName(self):
		name = super()._get_currentSettingName()
		if not self._combined or name is None:
			return name
		# Translators: a setting in the settings ring while a synthesizer for navigation is
		# loaded, so either synthesizer's can be told apart, e.g. "eSpeak NG rate".
		return _("{synth} {setting}").format(
			synth=self.settings[self._current].synth.description,
			setting=name,
		)

	@staticmethod
	def _key(ringSetting):
		return (ringSetting.synth, ringSetting.setting.id)

	@staticmethod
	def _ringSetting(synth, setting):
		# As NVDA's own ring picks them.
		if isinstance(setting, NumericDriverSetting):
			return SynthSetting(synth, setting)
		if isinstance(setting, BooleanDriverSetting):
			return BooleanSynthSetting(synth, setting)
		return StringSynthSetting(synth, setting)


def _loadVoiceDict(synth):
	try:
		speechDictHandler.loadVoiceDict(synth)
	except Exception:
		log.error(f"ElevenLabs: could not load the voice dictionary for {synth.name}", exc_info=True)


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
		self.owner = owner
		self.synth = None
		self._registered = False

	def load(self, name):
		"""Loads name, or eSpeak if it can't be; returns the name loaded, or None."""
		self.unload(restore=False)
		candidates = (name,) if name == FALLBACK_SYNTH else (name, FALLBACK_SYNTH)
		for candidate in candidates:
			try:
				# As NVDA's setSynth does, so the driver reads its settings from config.conf.
				# Loading rebuilds NVDA's settings ring for this synthesizer alone.
				synth = synthDriverHandler.getSynthInstance(candidate)
			except Exception:
				log.error(f"ElevenLabs: could not load {candidate} for navigation", exc_info=True)
				continue
			self.synth = synth
			self._register()
			globalVars.settingsRing = _CombinedSettingsRing(self)
			log.info(f"ElevenLabs: {synth.name} speaks outside say all.")
			return synth.name
		self._restoreOwner()
		return None

	def unload(self, restore=True):
		"""Unloads the synthesizer; restore gives ElevenLabs back its settings ring
		and voice dictionary, which isn't wanted while ElevenLabs itself unloads."""
		synth, self.synth = self.synth, None
		self._unregister()
		if synth is None:
			return
		try:
			synth.cancel()
			synth.terminate()
		except Exception:
			log.error(f"ElevenLabs: could not unload {synth.name}", exc_info=True)
		if restore:
			self._restoreOwner()

	def _restoreOwner(self):
		ring = globalVars.settingsRing
		if isinstance(ring, _CombinedSettingsRing):
			ring.updateSupportedSettings(self.owner)
		_loadVoiceDict(self.owner)

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
			synthIndexReached.notify(synth=self.owner, index=index)

	def _onDoneSpeaking(self, synth=None):
		if synth is not None and synth is self.synth:
			synthDoneSpeaking.notify(synth=self.owner)

	def _onProfileSwitch(self):
		# NVDA only applies a new profile's voice settings to its own synthesizer.
		synth = self.synth
		if synth is None:
			return
		try:
			synth.loadSettings(onlyChanged=True)
		except Exception:
			log.error(f"ElevenLabs: could not apply the profile's settings to {synth.name}", exc_info=True)

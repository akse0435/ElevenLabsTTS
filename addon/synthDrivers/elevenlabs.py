# -*- coding: utf-8 -*-
# ElevenLabs speech synthesizer driver for NVDA.
#
# Setup:
#   1. Installing the add-on creates "elevenlabs_settings.txt" in your NVDA
#      configuration folder (usually %APPDATA%\nvda). Open it and put your
#      ElevenLabs API key after the equals sign on the api_key line.
#   2. Restart NVDA and select "ElevenLabs" as the synthesizer.
#
# The same file holds every other setting this driver keeps outside NVDA's own
# configuration, and the driver writes back to it. A key saved in the older
# "elevenlabs_api_key.txt" is moved into it automatically.
#
# Speeds above what the API itself offers are produced locally with sonic, the
# time stretcher NVDA ships from 2025.1 onwards, hence the minimum version.
#
# Note: all speech is sent to ElevenLabs' servers and every character counts
# towards your API usage. The streaming endpoint is used for the lowest possible
# latency, but expect noticeable delay compared to a local synthesizer.

import array
import hashlib
import json
import os
import queue
import re
import struct
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from ctypes import c_short
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

import config
import globalVars
import nvwave
import queueHandler
from autoSettingsUtils.driverSetting import (
	BooleanDriverSetting,
	DriverSetting,
	NumericDriverSetting,
)
from autoSettingsUtils.utils import StringParameterInfo
from logHandler import log
from speech.commands import IndexCommand, LangChangeCommand
from synthDriverHandler import (
	SynthDriver as BaseSynthDriver,
	VoiceInfo,
	synthDoneSpeaking,
	synthIndexReached,
)

API_BASE = "https://api.elevenlabs.io"
# Flash v2.5 is the fastest model and supports Danish.
MODEL_ID = "eleven_flash_v2_5"
CHUNK_SIZE = 4096
REQUEST_TIMEOUT = 15
# Checks run while the synthesizer loads. NVDA is waiting during these, so the
# timeout is what bounds how long start up can stall before the driver gives up
# and reports failure, letting NVDA fall back to another synthesizer.
INIT_TIMEOUT = 8
# Voices are checked by asking for the shortest possible sample. One character
# costs one credit, and the result is cached, so this is paid once per account
# state rather than on every start.
PROBE_TEXT = "."
PROBE_TIMEOUT = 10
PROBE_WORKERS = 4
# A guard against accounts with very large voice libraries, where checking every
# voice would mean a long wait and a lot of requests.
MAX_PROBED_VOICES = 100
# Bumped whenever the check itself changes, so results recorded by an older
# version are worked out again rather than trusted.
VOICE_CHECK_VERSION = 2
# How many voices are tried at start up before giving up. Each one is a request,
# so this bounds how long NVDA can be held back by an account whose voices have
# stopped working.
MAX_STARTUP_TRIES = 8
# An upper bound on the whole voice check, so an account with many voices or a
# slow connection can never hold NVDA back indefinitely.
VOICE_CHECK_DEADLINE = 20

# One retry is enough for a dropped connection or a busy server; more would
# stack up delay on speech the user is waiting for.
MAX_RETRIES = 1
RETRY_DELAY = 0.5
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)

# Audio formats tried in order, best first, as (output_format, sample rate).
# The account's plan decides which are allowed: PCM at 44.1 kHz needs the Pro
# plan and the lower PCM rates need Creator, so a request for a format above the
# plan is rejected with HTTP 403. The driver then steps down this list until one
# is accepted; ulaw_8000 is available on every plan but sounds noticeably worse.
OUTPUT_FORMATS = (
	("pcm_44100", 44100),
	("pcm_24000", 24000),
	("pcm_22050", 22050),
	("pcm_16000", 16000),
	("ulaw_8000", 8000),
)

# Speech rate. The API's own speed parameter only covers 0.7 to 1.2, which is
# far too slow for practised screen reader users, so the rate is split in two:
# the API is asked for as much of it as it accepts and sonic stretches the audio
# for the rest. Normal speed sits at rate 50 so the slider still behaves.
API_SPEED_RANGE = (0.7, 1.2)
SPEED_AT_MIN_RATE = 0.5
SPEED_AT_RATE_50 = 1.0
SPEED_AT_MAX_RATE = 3.0
# Rate boost stretches the top of the slider further, as NVDA's own drivers do.
SPEED_AT_MAX_RATE_BOOSTED = 6.0

# The model's own voice settings, each exposed as a slider. NVDA works in whole
# numbers from 0 to 100 while the API takes 0.0 to 1.0, so the values are simply
# divided by 100. The defaults are the ones ElevenLabs recommends, and are what
# this driver has always sent, so speech sounds the same until they are changed.
DEFAULT_STABILITY = 50  # stability 0.5
DEFAULT_CLARITY = 75  # similarity_boost 0.75
DEFAULT_STYLE = 0  # style 0.0
DEFAULT_SPEAKER_BOOST = True
# Below this much stretching sonic is skipped, since it can only add artefacts.
SONIC_THRESHOLD = 0.01

SETTINGS_FILENAME = "elevenlabs_settings.txt"
# Files used before the settings file existed; read once and then migrated.
LEGACY_KEY_FILENAME = "elevenlabs_api_key.txt"
LEGACY_FORMAT_FILENAME = "elevenlabs_output_format.txt"
SETTINGS_HEADER = (
	"# Settings for the ElevenLabs NVDA add-on, one name = value per line.",
	"# api_key: your ElevenLabs API key. Paste it after the equals sign below.",
	"# output_format: audio format the driver negotiated with your plan.",
	"# unusable_voices, voice_check: voices this account cannot generate with,",
	"# and a fingerprint of the account state they were worked out for.",
	"# check_voices = no turns the voice check off.",
	"# Remove a line to work that setting out again.",
)

# Fallback voice (Rachel), used until the real voice list arrives.
FALLBACK_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"

# The id of the language setting shown in the voice settings dialog.
# NVDA derives the available-values attribute via id.capitalize(), so the id
# must be a single lowercase word (here: _get_availableElvlanguages).
LANGUAGE_SETTING_ID = "elvlanguage"

# Languages supported by eleven_flash_v2_5 (ISO 639-1).
# "auto" omits language_code, letting the model detect the language itself.
LANGUAGES = OrderedDict((
	("auto", "Automatic (detect language)"),
	("da", "Danish"),
	("en", "English"),
	("de", "German"),
	("sv", "Swedish"),
	("no", "Norwegian"),
	("nl", "Dutch"),
	("fr", "French"),
	("es", "Spanish"),
	("it", "Italian"),
	("pt", "Portuguese"),
	("pl", "Polish"),
	("fi", "Finnish"),
	("cs", "Czech"),
	("sk", "Slovak"),
	("hu", "Hungarian"),
	("ro", "Romanian"),
	("bg", "Bulgarian"),
	("hr", "Croatian"),
	("el", "Greek"),
	("uk", "Ukrainian"),
	("ru", "Russian"),
	("tr", "Turkish"),
	("ar", "Arabic"),
	("hi", "Hindi"),
	("ta", "Tamil"),
	("id", "Indonesian"),
	("ms", "Malay"),
	("fil", "Filipino"),
	("vi", "Vietnamese"),
	("ja", "Japanese"),
	("ko", "Korean"),
	("zh", "Chinese"),
))

DEFAULT_LANGUAGE = "da"

# Request ids differ on every call, so they are stripped before an error is
# compared with the last one to decide whether it is worth logging again.
_REQUEST_ID_PATTERN = re.compile(r'"request_id"\s*:\s*"[^"]*"')


def _buildUlawTable():
	"""Lookup table mapping each mu-law byte to a little endian 16 bit sample."""
	table = []
	for byte in range(256):
		value = ~byte & 0xFF
		sign = value & 0x80
		exponent = (value >> 4) & 0x07
		mantissa = value & 0x0F
		sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
		if sign:
			sample = -sample
		table.append(struct.pack("<h", max(-32768, min(32767, sample))))
	return tuple(table)


_ULAW_TABLE = _buildUlawTable()


def _decodeUlaw(data):
	"""Converts mu-law bytes to the 16 bit PCM the player expects."""
	return b"".join(_ULAW_TABLE[byte] for byte in data)


_sonicStreamClass = None
_sonicUnavailable = False


def _sonicStreamFactory():
	"""Loads NVDA's sonic time stretcher on first use, or None if it's missing."""
	global _sonicStreamClass, _sonicUnavailable
	if _sonicStreamClass is None and not _sonicUnavailable:
		try:
			from synthDrivers._sonic import SonicStream, initialize
			initialize()
			_sonicStreamClass = SonicStream
		except Exception:
			_sonicUnavailable = True
			log.error(
				"ElevenLabs: sonic is unavailable, so the rate is limited to the speeds "
				"the API itself offers. NVDA 2025.1 or later is required.",
				exc_info=True,
			)
	return _sonicStreamClass


def _sonicWrite(stream, data):
	"""Pushes 16 bit PCM through sonic and returns whatever is ready to play."""
	count = len(data) // 2
	if count:
		stream.writeShort((c_short * count).from_buffer_copy(data), count)
	return bytes(stream.readShort())


def _sonicDrain(stream):
	"""Empties sonic; returns the tail of the utterance, or b"" when discarding."""
	stream.flush()
	return bytes(stream.readShort())


def _scaleVolume(data, gain):
	"""Quietens 16 bit PCM by the given gain, for players without a volume control.

	NVDA only runs on Windows, so array's native order is the little endian the
	API sends. The gain is never above 1, so samples cannot clip.
	"""
	samples = array.array("h")
	samples.frombytes(data)
	for index in range(len(samples)):
		samples[index] = int(samples[index] * gain)
	return samples.tobytes()


def _isOutputFormatRejection(detail):
	"""True if a 403 body complains about the requested audio format.

	The API has used both "output_format" and "output format" in these errors.
	"""
	detail = detail.lower()
	return "output_format" in detail or "output format" in detail


def _configFilePath(fileName):
	return os.path.join(globalVars.appArgs.configPath, fileName)


def _readLines(fileName):
	try:
		with open(_configFilePath(fileName), "r", encoding="utf-8-sig") as f:
			return f.read().splitlines()
	except OSError:
		return None


def _readSettings():
	"""Reads the settings file into a dict of lower case names to values."""
	settings = {}
	for line in _readLines(SETTINGS_FILENAME) or ():
		line = line.strip()
		if not line or line.startswith("#"):
			continue
		if "=" in line:
			name, _sep, value = line.partition("=")
			settings[name.strip().lower()] = value.strip()
		elif "api_key" not in settings:
			# A line with no name is taken as the key, so the old key file still
			# works if it is simply renamed.
			settings["api_key"] = line
	return settings


def _writeSetting(name, value):
	"""Rewrites one line of the settings file, leaving the rest untouched."""
	lines = _readLines(SETTINGS_FILENAME)
	if lines is None:
		lines = list(SETTINGS_HEADER)
	newLine = f"{name} = {value}"
	for index, line in enumerate(lines):
		if "=" in line and not line.strip().startswith("#") \
				and line.partition("=")[0].strip().lower() == name:
			lines[index] = newLine
			break
	else:
		lines.append(newLine)
	try:
		with open(_configFilePath(SETTINGS_FILENAME), "w", encoding="utf-8") as f:
			f.write("\n".join(lines) + "\n")
		return True
	except OSError:
		log.debugWarning(f"Could not save {name} to {SETTINGS_FILENAME}", exc_info=True)
		return False


def _ensureSettingsFile():
	"""Makes sure the settings file exists and has a line ready for the key.

	The add-on's install task does this when the add-on is installed, but that
	only runs once. This is called on every start as well, so the file is also
	there for an add-on folder copied in by hand, a portable copy, or a file
	deleted since. An existing file is never overwritten, only added to.
	"""
	path = _configFilePath(SETTINGS_FILENAME)
	lines = _readLines(SETTINGS_FILENAME)
	if lines is None:
		try:
			with open(path, "w", encoding="utf-8") as f:
				# The trailing space is deliberate, so the key can be typed or
				# pasted straight after it without adding one first.
				f.write("\n".join(SETTINGS_HEADER) + "\napi_key = \n")
			log.info(f"ElevenLabs: created {path}; add your API key to the api_key line.")
		except OSError:
			log.error(f"ElevenLabs: could not create {path}", exc_info=True)
		return path
	if "api_key" not in _readSettings():
		# The file is there but has nothing to paste a key into, so give it one.
		_writeSetting("api_key", "")
	return path


def _readApiKey():
	key = _readSettings().get("api_key")
	if key:
		return key
	# Falls back to the old file if the settings file has no key yet, or if it
	# could not be written when the key was migrated.
	for line in _readLines(LEGACY_KEY_FILENAME) or ():
		line = line.strip()
		if line:
			return line
	return None


def invalidateVoiceCheck():
	"""Forgets the recorded voice check, so the next load works it out again.

	Used when the synthesizer is not running and there is therefore nothing to
	check against. Selecting the synthesizer afterwards does the full check.
	"""
	return _writeSetting("voice_check", "")


def _migrateLegacyFiles():
	"""Moves the old separate key and format files into the settings file."""
	settings = _readSettings()
	migrated = []
	if "api_key" not in settings:
		for line in _readLines(LEGACY_KEY_FILENAME) or ():
			line = line.strip()
			if line and _writeSetting("api_key", line):
				migrated.append(LEGACY_KEY_FILENAME)
			break
	if "output_format" not in settings:
		for line in _readLines(LEGACY_FORMAT_FILENAME) or ():
			line = line.strip()
			if line and _writeSetting("output_format", line):
				migrated.append(LEGACY_FORMAT_FILENAME)
			break
	if migrated:
		log.info(
			f"ElevenLabs: moved {', '.join(migrated)} into {SETTINGS_FILENAME}; "
			"the old file(s) are no longer used and can be deleted."
		)


class ElevenLabsUnavailable(Exception):
	"""The synthesizer cannot be used, so NVDA should fall back to another one."""


# Outcomes of trying a voice: usable, definitely not usable with this account,
# or no clear answer, in which case the voice is left alone. A voice is only
# ever hidden on a definite answer, so a network hiccup can't make voices
# disappear.
PROBE_OK = "ok"
PROBE_UNUSABLE = "unusable"
PROBE_UNKNOWN = "unknown"
# Something wrong with the account rather than the voice, e.g. an exhausted
# quota or a rejected key. Every voice would fail, so checking stops.
PROBE_ACCOUNT = "account"

# Statuses that mean the request never got a real answer. Nothing is concluded
# about a voice from these.
TRANSIENT_STATUS_CODES = (408, 425, 429)

# Wording that points at the account rather than any one voice. These would make
# every voice fail, so the check stops instead of emptying the list.
_ACCOUNT_REJECTION_TERMS = (
	"quota_exceeded",
	"quota exceeded",
	"insufficient",
	"invalid_api_key",
	"missing_api_key",
	"api key",
	"api_key",
	"unusual activity",
	"detected_unusual_activity",
	"account",
	"disabled",
	"banned",
	"blocked",
)
# Wording that clearly blames the voice, which outranks the account terms above,
# since these messages often mention the subscription as well.
_VOICE_REJECTION_TERMS = (
	"voice",
	"voice_id",
	"model",
)


def _classifyVoiceFailure(status, detail):
	"""Decides whether a failed request means the voice or the account is at fault.

	By the time a voice is checked the key has already worked, the audio format
	has been settled and the text is a single character, so a refusal that names
	the voice, or any other refusal of the request itself, is the voice's doing.
	Only clearly transient answers are treated as no answer.
	"""
	detail = detail.lower()
	if status in TRANSIENT_STATUS_CODES or status >= 500:
		return PROBE_UNKNOWN
	blamesVoice = any(term in detail for term in _VOICE_REJECTION_TERMS)
	if not blamesVoice and any(term in detail for term in _ACCOUNT_REJECTION_TERMS):
		return PROBE_ACCOUNT
	if 400 <= status < 500:
		return PROBE_UNUSABLE
	return PROBE_UNKNOWN


def _fetchJson(url, key, timeout):
	"""GETs a JSON document, raising ElevenLabsUnavailable with a readable reason."""
	req = urlrequest.Request(url, headers={
		"xi-api-key": key,
		"Accept": "application/json",
		# Figures like the credit count change constantly, so no cache along the
		# way may answer on the server's behalf.
		"Cache-Control": "no-cache, no-store, max-age=0",
		"Pragma": "no-cache",
	})
	try:
		with urlrequest.urlopen(req, timeout=timeout) as resp:
			return json.loads(resp.read().decode("utf-8"))
	except HTTPError as e:
		detail = ""
		try:
			detail = e.read().decode("utf-8", "replace")[:300]
		except Exception:
			pass
		if e.code in (401, 403):
			raise ElevenLabsUnavailable(
				f"ElevenLabs rejected the API key (HTTP {e.code}). Check the api_key line "
				f"in {SETTINGS_FILENAME}. {detail}"
			)
		raise ElevenLabsUnavailable(f"ElevenLabs returned HTTP {e.code} for {url}. {detail}")
	except (URLError, OSError) as e:
		raise ElevenLabsUnavailable(f"Could not reach ElevenLabs: {e}")
	except ValueError as e:
		raise ElevenLabsUnavailable(f"ElevenLabs sent a response that could not be read: {e}")


def _makePlayer(sampleRate):
	"""Creates a WavePlayer, coping with differences between NVDA versions."""
	kwargs = dict(channels=1, samplesPerSec=sampleRate, bitsPerSample=16)
	# Device selection moved from the speech to the audio section in newer NVDA.
	device = None
	try:
		device = config.conf["audio"]["outputDevice"]
	except KeyError:
		try:
			device = config.conf["speech"]["outputDevice"]
		except KeyError:
			pass
	if device is not None:
		try:
			return nvwave.WavePlayer(outputDevice=device, **kwargs)
		except TypeError:
			pass
	return nvwave.WavePlayer(**kwargs)


class _SpeechWorker(threading.Thread):
	"""Background thread: fetches audio from the API and feeds the player."""

	daemon = True

	def __init__(self, driver):
		super().__init__(name="ElevenLabsSpeechWorker")
		self.driver = driver
		self.queue = queue.Queue()
		self.generation = 0  # incremented on cancel so stale jobs are dropped
		self._lock = threading.Lock()
		self._running = True
		self.start()

	def enqueue(self, text, indexes, langOverride=None):
		with self._lock:
			gen = self.generation
		self.queue.put((gen, text, indexes, langOverride))

	def cancelAll(self):
		with self._lock:
			self.generation += 1
		# Empty the queue.
		try:
			while True:
				self.queue.get_nowait()
		except queue.Empty:
			pass

	def stop(self):
		self._running = False
		self.cancelAll()
		self.queue.put(None)

	def _isCurrent(self, gen):
		with self._lock:
			return gen == self.generation and self._running

	def run(self):
		while True:
			item = self.queue.get()
			if item is None:
				return
			gen, text, indexes, langOverride = item
			if not self._isCurrent(gen):
				continue
			completed = False
			try:
				completed = self._synthesize(gen, text, langOverride)
			except Exception:
				log.error("Unexpected error in the ElevenLabs driver", exc_info=True)
			if self._isCurrent(gen):
				# Indexes are reported even if synthesis failed, since NVDA's
				# speech queue waits for them before moving on.
				for index in indexes:
					synthIndexReached.notify(synth=self.driver, index=index)
				if self.queue.empty():
					player = self.driver._player
					if completed and player is not None:
						try:
							player.idle()
						except Exception:
							pass
					synthDoneSpeaking.notify(synth=self.driver)

	def _synthesize(self, gen, text, langOverride=None):
		driver = self.driver
		key = driver._getApiKey()
		if not key:
			driver.reportError(
				"nokey",
				f"The ElevenLabs API key is no longer readable. Check the api_key line in "
				f"{SETTINGS_FILENAME} in NVDA's configuration folder.",
			)
			return False
		body = {"text": text, "model_id": MODEL_ID}
		# Language priority: NVDA's language markup > the language setting > auto.
		lang = langOverride or driver._language
		if lang and lang != "auto":
			body["language_code"] = lang
		retries = 0
		while True:
			outputFormat = driver.outputFormat
			isUlaw = outputFormat.startswith("ulaw")
			body["voice_settings"] = driver.voiceSettings
			req = urlrequest.Request(
				f"{API_BASE}/v1/text-to-speech/{driver.synthesisVoice}/stream"
				f"?output_format={outputFormat}",
				data=json.dumps(body).encode("utf-8"),
				headers={
					"xi-api-key": key,
					"Content-Type": "application/json",
					"Accept": "*/*",
				},
				method="POST",
			)
			fedAudio = False
			carry = b""  # odd trailing byte held back so samples stay aligned
			try:
				with urlrequest.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
					player = driver._getPlayer()
					# None when the audio needs no stretching, which is the case
					# for any rate the API can deliver on its own.
					sonic = driver._prepareSonic()
					while True:
						if not self._isCurrent(gen):
							return False
						chunk = resp.read(CHUNK_SIZE)
						if not chunk:
							break
						if isUlaw:
							chunk = _decodeUlaw(chunk)
						chunk = carry + chunk
						if len(chunk) % 2:
							carry, chunk = chunk[-1:], chunk[:-1]
						else:
							carry = b""
						gain = driver.softwareGain
						if sonic is not None:
							# Sonic scales the volume too, in C rather than Python.
							sonic.volume = gain
							chunk = _sonicWrite(sonic, chunk)
						elif gain < 1.0:
							chunk = _scaleVolume(chunk, gain)
						if not chunk:
							continue
						# feed blocks until there is room in the playback buffer
						player.feed(chunk)
						fedAudio = True
					if sonic is not None:
						chunk = _sonicDrain(sonic)
						if chunk:
							player.feed(chunk)
							fedAudio = True
			except HTTPError as e:
				detail = _REQUEST_ID_PATTERN.sub("", self._readErrorDetail(e))
				# A rejected format means the account doesn't allow it, so the
				# same utterance is retried with the next one down.
				if e.code == 403 and _isOutputFormatRejection(detail) \
						and driver._useNextOutputFormat():
					continue
				if e.code == 401:
					# The key may have been edited; read it again next time.
					driver._forgetApiKey()
				if e.code in RETRY_STATUS_CODES and not fedAudio and retries < MAX_RETRIES:
					retries += 1
					if self._waitBeforeRetry(gen):
						continue
					return False
				# A voice that stops working mid-session, e.g. after a downgrade
				# in another window, is taken out of the list and the utterance
				# is tried once more with a voice that does work.
				if not fedAudio and _classifyVoiceFailure(e.code, detail) == PROBE_UNUSABLE:
					voiceId = driver.synthesisVoice
					driver._markVoiceUnusable(voiceId, detail.strip())
					if driver.synthesisVoice != voiceId:
						continue
				driver.reportError(f"http{e.code}", f"ElevenLabs API error {e.code}: {detail}")
				return False
			except (URLError, OSError) as e:
				# Timeouts and dropped connections; only safe to retry while no
				# audio of this utterance has been played yet.
				if not fedAudio and retries < MAX_RETRIES:
					retries += 1
					if self._waitBeforeRetry(gen):
						continue
					return False
				driver.reportError("network", f"ElevenLabs network error: {e}")
				return False
			driver._onSynthesisSucceeded()
			return True

	def _waitBeforeRetry(self, gen):
		time.sleep(RETRY_DELAY)
		return self._isCurrent(gen)

	@staticmethod
	def _readErrorDetail(error):
		try:
			return error.read().decode("utf-8", "replace")
		except Exception:
			return ""


class SynthDriver(BaseSynthDriver):
	name = "elevenlabs"
	description = "ElevenLabs"

	supportedSettings = (
		BaseSynthDriver.VoiceSetting(),
		DriverSetting(LANGUAGE_SETTING_ID, "&Language", availableInSettingsRing=True),
		BaseSynthDriver.RateSetting(),
		BaseSynthDriver.RateBoostSetting(),
		BaseSynthDriver.VolumeSetting(),
		NumericDriverSetting(
			"stability",
			"&Stability",
			availableInSettingsRing=True,
			defaultVal=DEFAULT_STABILITY,
			displayName="Stability",
		),
		NumericDriverSetting(
			"clarity",
			"&Clarity",
			availableInSettingsRing=True,
			defaultVal=DEFAULT_CLARITY,
			displayName="Clarity",
		),
		NumericDriverSetting(
			"style",
			"St&yle",
			availableInSettingsRing=True,
			defaultVal=DEFAULT_STYLE,
			displayName="Style",
		),
		BooleanDriverSetting(
			"speakerBoost",
			"Speaker &boost",
			availableInSettingsRing=True,
			defaultVal=DEFAULT_SPEAKER_BOOST,
			displayName="Speaker boost",
		),
	)
	supportedCommands = {IndexCommand, LangChangeCommand}
	supportedNotifications = {synthIndexReached, synthDoneSpeaking}

	@classmethod
	def check(cls):
		# Always listed, so a user who hasn't set up a key yet gets a clear
		# explanation on selecting it rather than a missing entry.
		return True

	def __init__(self):
		super().__init__()
		self._voiceId = None
		self._language = DEFAULT_LANGUAGE
		self._rate = 50
		self._rateBoost = False
		self._volume = 100
		self._stability = DEFAULT_STABILITY
		self._clarity = DEFAULT_CLARITY
		self._style = DEFAULT_STYLE
		self._speakerBoost = DEFAULT_SPEAKER_BOOST
		self._allVoices = OrderedDict()
		self._voiceCategories = {}
		self._voicesCache = OrderedDict()
		self._unusableVoices = set()
		self._voicesChecked = False
		self._tier = ""
		self._formatIndex = 0
		self._apiKey = None
		self._loggedErrors = {}
		# What the settings file already says, so it is only rewritten on a change.
		self._recordedFormat = None
		self._voicesLock = threading.Lock()
		self._playerLock = threading.Lock()
		# Created on first use, since its sample rate depends on the format the
		# account turns out to allow.
		self._player = None
		self._playerHasVolume = False
		self._sonic = None
		self._worker = None
		self._probeThread = None
		self._probeStop = threading.Event()
		_migrateLegacyFiles()
		self._loadOutputFormat()
		self._migrateLegacyVariantSetting()
		# Everything below can fail, and failing here is the point: NVDA catches
		# it, tells the user and falls back to a working synthesizer instead of
		# leaving them with a synthesizer that is silently mute.
		# Nothing has been started yet, so there is nothing to clean up on the
		# way out; terminate() is never called when a driver raises here.
		self._loadAccountState()
		self._worker = _SpeechWorker(self)
		try:
			self._checkVoicesAtStartup()
		except Exception:
			# Started above, so it has to be stopped before the exception leaves.
			self._worker.stop()
			raise
		hidden = len(self._unusableVoices)
		log.info(
			f"ElevenLabs ready: {len(self._voicesCache)} voice(s)"
			f"{f', {hidden} unusable' if hidden else ''}, "
			f"plan {self._tier or 'unknown'}, audio {self.outputFormat}."
		)

	def terminate(self):
		self.cancel()
		self._probeStop.set()
		if self._worker is not None:
			self._worker.stop()
		self._closePlayer()
		# Saves the current settings and unregisters the config save action.
		super().terminate()

	def speak(self, speechSequence):
		textParts = []
		indexes = []
		langOverride = None
		for item in speechSequence:
			if isinstance(item, str):
				textParts.append(item)
			elif isinstance(item, IndexCommand):
				indexes.append(item.index)
			elif isinstance(item, LangChangeCommand):
				# NVDA sends e.g. "en_US" or "da_DK"; keep only the language part.
				if item.lang:
					code = item.lang.split("_")[0].lower()
					if code in LANGUAGES and code != "auto":
						langOverride = code
		text = " ".join(textParts).strip()
		if not text:
			# No text, but indexes must still be reported as reached.
			for index in indexes:
				synthIndexReached.notify(synth=self, index=index)
			synthDoneSpeaking.notify(synth=self)
			return
		self._worker.enqueue(text, indexes, langOverride)

	def cancel(self):
		self._worker.cancelAll()
		player = self._player
		if player is not None:
			try:
				player.stop()
			except Exception:
				pass

	def pause(self, switch):
		player = self._player
		if player is not None:
			try:
				player.pause(switch)
			except Exception:
				pass

	# --- API key ---

	def _getApiKey(self):
		"""Returns the cached key, reading the settings file only when needed."""
		if self._apiKey is None:
			self._apiKey = _readApiKey()
		return self._apiKey

	def _forgetApiKey(self):
		self._apiKey = None

	# --- Error reporting ---

	def reportError(self, kind, message):
		"""Logs an error, but not once per utterance while it keeps happening."""
		if self._loggedErrors.get(kind) == message:
			return
		self._loggedErrors[kind] = message
		log.error(message)

	# --- Audio format, speed and player ---

	def _get_outputFormat(self):
		return OUTPUT_FORMATS[self._formatIndex][0]

	def _get_speechSpeed(self):
		"""The speed the user asked for: NVDA's 0-100 rate, normal speed at 50."""
		fastest = SPEED_AT_MAX_RATE_BOOSTED if self._rateBoost else SPEED_AT_MAX_RATE
		if self._rate <= 50:
			speed = SPEED_AT_MIN_RATE + (self._rate / 50.0) * (SPEED_AT_RATE_50 - SPEED_AT_MIN_RATE)
		else:
			speed = SPEED_AT_RATE_50 + ((self._rate - 50) / 50.0) * (fastest - SPEED_AT_RATE_50)
		return round(speed, 2)

	def _get_apiSpeed(self):
		"""The part of the speed the API can deliver on its own."""
		lowest, highest = API_SPEED_RANGE
		return round(min(max(self.speechSpeed, lowest), highest), 2)

	def _get_sonicSpeed(self):
		"""The rest of the speed, left for sonic to stretch locally."""
		return round(self.speechSpeed / self.apiSpeed, 3)

	def _get_softwareGain(self):
		"""The gain the worker must apply when the player can't do it itself."""
		return 1.0 if self._playerHasVolume else self._volume / 100.0

	def _getPlayer(self):
		"""Returns the player for the current format, creating it if needed."""
		with self._playerLock:
			if self._player is None:
				sampleRate = OUTPUT_FORMATS[self._formatIndex][1]
				self._player = _makePlayer(sampleRate)
				self._playerHasVolume = self._applyPlayerVolume(self._player)
				# Sonic works on the same audio, so it follows the sample rate.
				streamClass = _sonicStreamFactory()
				if streamClass is not None:
					try:
						self._sonic = streamClass(sampleRate, 1)
					except Exception:
						log.error("ElevenLabs: could not create the sonic stream", exc_info=True)
						self._sonic = None
			return self._player

	def _applyPlayerVolume(self, player):
		"""Uses the player's own volume control where NVDA provides one."""
		setVolume = getattr(player, "setVolume", None)
		if setVolume is None:
			return False
		try:
			setVolume(all=self._volume / 100.0)
			return True
		except Exception:
			log.debugWarning("ElevenLabs: player volume unavailable", exc_info=True)
			return False

	def _prepareSonic(self):
		"""Returns a sonic stream set up for this utterance, or None if unneeded."""
		speed = self.sonicSpeed
		if abs(speed - 1.0) <= SONIC_THRESHOLD:
			return None
		with self._playerLock:
			stream = self._sonic
		if stream is None:
			return None
		# Anything left from a cancelled utterance must go, or it would be
		# played at the start of this one.
		_sonicDrain(stream)
		stream.speed = speed
		return stream

	def _closePlayer(self):
		with self._playerLock:
			player, self._player = self._player, None
			self._sonic = None
		if player is not None:
			try:
				player.close()
			except Exception:
				pass

	def _useNextOutputFormat(self):
		"""Steps down to the next audio format; False if none are left to try."""
		if self._formatIndex + 1 >= len(OUTPUT_FORMATS):
			self.reportError(
				"format",
				f"ElevenLabs: the plan for this API key allows none of the supported "
				f"audio formats ({', '.join(f[0] for f in OUTPUT_FORMATS)}).",
			)
			return False
		self._formatIndex += 1
		log.debug(
			f"ElevenLabs: the plan does not allow "
			f"{OUTPUT_FORMATS[self._formatIndex - 1][0]}; trying {self.outputFormat} instead."
		)
		# The sample rate changed, so the player has to be rebuilt.
		self._closePlayer()
		return True

	def _onSynthesisSucceeded(self):
		"""Records the format that worked, so it isn't worked out again."""
		self._loggedErrors.clear()
		if self._recordedFormat == self.outputFormat:
			# Nothing new, so the settings file isn't touched for every utterance.
			return
		if _readSettings().get("output_format") != self.outputFormat:
			_writeSetting("output_format", self.outputFormat)
		self._recordedFormat = self.outputFormat

	def _loadOutputFormat(self):
		"""Restores the audio format worked out in an earlier session."""
		savedFormat = _readSettings().get("output_format")
		for index, (formatName, sampleRate) in enumerate(OUTPUT_FORMATS):
			if formatName == savedFormat:
				self._formatIndex = index
				self._recordedFormat = savedFormat
				break
		else:
			if savedFormat:
				log.warning(f"ElevenLabs: ignoring unknown output_format {savedFormat!r} in {SETTINGS_FILENAME}")

	# --- Voices ---
	# The list NVDA shows must only hold voices this account can actually
	# generate with. Voices stay on an account after a downgrade but stop
	# working, and some library voices are barred on the free plan, and neither
	# is visible in the voice list, so each voice is tried once and the result
	# is cached against a fingerprint of the account state.

	def _loadAccountState(self):
		"""Checks the account and builds the voice list; raises if unusable."""
		key = self._getApiKey()
		if not key:
			path = _ensureSettingsFile()
			raise ElevenLabsUnavailable(
				f"No ElevenLabs API key yet. Open {path} and put your key after the equals "
				"sign on the api_key line, then select the synthesizer again."
			)
		data = _fetchJson(f"{API_BASE}/v1/voices", key, INIT_TIMEOUT)
		voices = OrderedDict()
		categories = {}
		for v in data.get("voices", ()):
			voiceId = v.get("voice_id")
			if voiceId:
				voices[voiceId] = v.get("name") or voiceId
				categories[voiceId] = (v.get("category") or "").lower()
		if not voices:
			raise ElevenLabsUnavailable(
				"This ElevenLabs account has no voices. Add at least one voice to the "
				"account and reselect the synthesizer."
			)
		self._allVoices = voices
		self._voiceCategories = categories
		# The plan decides which voices work, so it is part of the fingerprint.
		try:
			subscription = _fetchJson(f"{API_BASE}/v1/user/subscription", key, INIT_TIMEOUT)
			self._tier = str(subscription.get("tier") or "")
		except ElevenLabsUnavailable as e:
			# Not fatal: the tier only sharpens the cache, it isn't needed to speak.
			log.debugWarning(f"ElevenLabs: could not read the subscription: {e}")
			self._tier = ""
		self._loadUnusableVoices()
		self._rebuildVoiceList()

	def _accountFingerprint(self):
		"""Identifies the plan and voice list, so cached results are only reused
		while both are unchanged."""
		digest = hashlib.md5("\n".join(sorted(self._allVoices)).encode("utf-8")).hexdigest()[:12]
		return f"v{VOICE_CHECK_VERSION}:{self._tier or 'unknown'}:{len(self._allVoices)}:{digest}"

	def _loadUnusableVoices(self):
		"""Restores the previously checked result, if it still applies."""
		settings = _readSettings()
		if settings.get("voice_check") != self._accountFingerprint():
			# The plan or the voice list changed, so the old result means nothing.
			self._unusableVoices = set()
			self._voicesChecked = False
			return
		saved = settings.get("unusable_voices", "")
		self._unusableVoices = {v.strip() for v in saved.split(",") if v.strip()}
		# The result is only recorded once every voice has an answer, so a
		# matching fingerprint means the whole list is already known good.
		self._voicesChecked = True

	def _saveUnusableVoices(self):
		_writeSetting("unusable_voices", ",".join(sorted(self._unusableVoices)))
		_writeSetting("voice_check", self._accountFingerprint())

	def _rebuildVoiceList(self):
		"""Rebuilds the list NVDA sees, leaving out voices known to be unusable."""
		with self._voicesLock:
			usable = OrderedDict(
				(voiceId, VoiceInfo(voiceId, name))
				for voiceId, name in self._allVoices.items()
				if voiceId not in self._unusableVoices
			)
			self._voicesCache = usable
		return usable

	def _checkVoicesAtStartup(self):
		"""Settles the voice list before NVDA reads it.

		This runs while NVDA waits, on purpose. NVDA builds the voice list in the
		settings dialog once per session and keeps it, so a voice removed later
		would stay on screen and simply not work when picked. Doing it here means
		the list NVDA sees is right from the start. The answer is cached, so the
		wait is paid once per plan and voice list rather than on every start.
		"""
		if not self._voicesCache:
			raise ElevenLabsUnavailable(
				"None of the voices on this ElevenLabs account can be used with the current "
				f"plan. Remove the unusable_voices line from {SETTINGS_FILENAME} to check "
				"them again, or upgrade the plan."
			)
		# The chosen voice is checked first, since that request also proves the
		# key, the plan and the audio format work end to end.
		self._validateStartupVoice()
		if self._voicesChecked:
			return
		if _readSettings().get("check_voices", "").lower() in ("no", "false", "0", "off"):
			log.debug(f"ElevenLabs: the voice check is switched off in {SETTINGS_FILENAME}.")
			return
		pending = [v for v in self._voicesCache if v != self._voiceId]
		if pending:
			self._checkVoices(pending)

	def _checkVoices(self, pending):
		"""Tries each voice once and takes the ones that fail out of the list."""
		if len(pending) > MAX_PROBED_VOICES:
			log.warning(
				f"ElevenLabs: only checking {MAX_PROBED_VOICES} of {len(pending)} voices; "
				"any others that turn out to be unusable are removed when first used."
			)
			pending = pending[:MAX_PROBED_VOICES]
		started = time.time()
		deadline = started + VOICE_CHECK_DEADLINE
		unusable, unanswered, succeeded, lastReason, accountFailure = self._runProbes(
			pending, deadline
		)
		# One more try for the ones that gave no answer, one at a time, so a
		# burst of rate limiting doesn't cost the whole cached result.
		if unanswered and not accountFailure and time.time() < deadline:
			retryUnusable, unanswered, retryOk, retryReason, accountFailure = self._runProbes(
				unanswered, deadline, workers=1
			)
			unusable += retryUnusable
			succeeded += retryOk
			lastReason = retryReason or lastReason
		if self._probeStop.is_set():
			return
		if accountFailure:
			# The account stopped answering part way through, so what was learned
			# is unreliable. Nothing is hidden and nothing is cached; the check
			# simply runs again next time.
			log.warning(
				"ElevenLabs: the voice check could not finish because the account stopped "
				f"accepting requests, so the voice list is unchanged. Reason: {lastReason or 'see above'}"
			)
			return
		# A voice that already spoke proves the key, plan, model and format are
		# fine, so the rest failing really is about those voices. Without such
		# proof, everything failing points at the account instead, and hiding the
		# whole list would be the wrong conclusion.
		provenVoice = self._voiceId is not None and self._voiceId not in pending
		if not provenVoice and not succeeded and len(pending) > 1 and unusable:
			log.error(
				"ElevenLabs: every voice was refused, so this looks like an account problem "
				f"rather than {len(pending)} unusable voices. The voice list is left alone. "
				f"The last reason given was: {lastReason}"
			)
			return
		if unusable:
			self._unusableVoices.update(unusable)
			if self._voiceId in self._unusableVoices:
				self._voiceId = None
			self._rebuildVoiceList()
			names = ", ".join(sorted(self._allVoices.get(v, v) for v in unusable))
			log.warning(
				f"ElevenLabs: {len(unusable)} voice(s) cannot be used with this account and "
				f"have been left out of the voice list: {names}"
			)
		if unanswered:
			# Nothing is cached while any voice is still unaccounted for, so it
			# gets another chance next time rather than being wrongly trusted.
			log.warning(
				f"ElevenLabs: {len(unanswered)} voice(s) gave no answer, so the check will "
				"run again next time the synthesizer is loaded."
			)
			return
		self._voicesChecked = True
		self._saveUnusableVoices()
		log.debug(
			f"ElevenLabs: checked {len(pending)} voices in {time.time() - started:.1f} s."
		)

	def _runProbes(self, pending, deadline, workers=PROBE_WORKERS):
		"""Tries a batch of voices, returning what was learned about each."""
		unusable, unanswered, succeeded, lastReason = [], [], 0, ""
		# Set as soon as the account itself refuses, so the voices still queued
		# give up instead of hammering an account that cannot answer.
		accountFailure = []

		def probe(voiceId):
			if accountFailure or self._probeStop.is_set() or time.time() > deadline:
				return PROBE_UNKNOWN, "check abandoned"
			result, detail = self._probeVoice(voiceId)
			if result == PROBE_ACCOUNT:
				accountFailure.append(detail)
			return result, detail

		try:
			with ThreadPoolExecutor(max_workers=workers) as pool:
				results = list(zip(pending, pool.map(probe, pending)))
		except Exception:
			log.error("ElevenLabs: the voice check failed", exc_info=True)
			return unusable, list(pending), succeeded, lastReason, True
		for voiceId, (result, detail) in results:
			if result == PROBE_UNUSABLE:
				unusable.append(voiceId)
				lastReason = detail
				log.debug(f"ElevenLabs: voice {voiceId} refused: {detail}")
			elif result == PROBE_OK:
				succeeded += 1
			else:
				# Includes the voice that hit the account error itself, so it is
				# never silently treated as answered.
				unanswered.append(voiceId)
		return unusable, unanswered, succeeded, lastReason, bool(accountFailure)

	def recheckVoices(self, onFinished=None):
		"""Runs the voice check again from scratch, e.g. after a plan change.

		@param onFinished: called with the number of usable and unusable voices
			once the check is done. It runs on the checking thread, so anything
			touching NVDA's interface must be queued onto the main thread.
		"""
		if self._probeThread is not None and self._probeThread.is_alive():
			return False
		# Off the main thread this time: NVDA is already running, so it must not
		# be frozen for a check the user asked for while working.
		self._probeThread = threading.Thread(
			target=self._recheckVoicesNow,
			args=(onFinished,),
			name="ElevenLabsVoiceCheck",
			daemon=True,
		)
		self._probeThread.start()
		return True

	def _recheckVoicesNow(self, onFinished=None):
		self._unusableVoices = set()
		self._voicesChecked = False
		self._rebuildVoiceList()
		try:
			self._checkVoices(list(self._allVoices))
			self.refreshVoiceListInGui()
		finally:
			if onFinished is not None and not self._probeStop.is_set():
				try:
					onFinished(len(self._voicesCache), len(self._unusableVoices))
				except Exception:
					log.error("ElevenLabs: could not report the voice check result", exc_info=True)

	def _startupCandidates(self):
		"""Voices to try at start up, likeliest to work first.

		The saved choice comes first. After that come the voices ElevenLabs
		supplies with every plan, since cloned voices are exactly the ones that
		stop working after a downgrade, and trying those first would mean a slow
		start up for an account with many of them.
		"""
		with self._voicesLock:
			usable = list(self._voicesCache)
		ordered = [v for v in (self._voiceId,) if v in usable]
		ordered += [v for v in usable if v not in ordered and self._voiceCategories.get(v) == "premade"]
		ordered += [v for v in usable if v not in ordered]
		return ordered[:MAX_STARTUP_TRIES]

	def _validateStartupVoice(self):
		"""Tries voices until one speaks, or gives up so NVDA can fall back."""
		removed = []
		for voiceId in self._startupCandidates():
			if voiceId in self._unusableVoices:
				continue
			result, detail = self._probeVoice(voiceId)
			if result == PROBE_OK:
				self._voiceId = voiceId
				if removed:
					# Only now is it clear the account itself works, so the
					# removals are worth remembering.
					names = ", ".join(sorted(self._allVoices.get(v, v) for v in removed))
					log.warning(
						f"ElevenLabs: {len(removed)} voice(s) cannot be used with this account "
						f"and have been left out of the voice list: {names}"
					)
					self._saveUnusableVoices()
				return
			if result == PROBE_ACCOUNT:
				raise ElevenLabsUnavailable(f"This ElevenLabs account cannot be used: {detail}")
			if result == PROBE_UNKNOWN:
				raise ElevenLabsUnavailable(f"ElevenLabs could not be reached: {detail}")
			log.debug(f"ElevenLabs: voice {voiceId} refused at start up: {detail}")
			self._unusableVoices.add(voiceId)
			removed.append(voiceId)
			self._voiceId = None
			if not self._rebuildVoiceList():
				raise ElevenLabsUnavailable(
					"None of the voices on this ElevenLabs account can be used with the "
					"current plan."
				)
		raise ElevenLabsUnavailable(
			f"None of the first {MAX_STARTUP_TRIES} voices tried could be used with this "
			"ElevenLabs account."
		)

	def _probeVoice(self, voiceId):
		"""Asks for the shortest possible sample to see whether a voice works."""
		key = self._getApiKey()
		if not key:
			return PROBE_ACCOUNT, "no API key"
		while True:
			body = {
				"text": PROBE_TEXT,
				"model_id": MODEL_ID,
				# Deliberately plain rather than the user's settings: this only
				# checks whether the voice may be used at all, and style in
				# particular costs extra latency that would slow start up.
				"voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
			}
			if self._language and self._language != "auto":
				body["language_code"] = self._language
			req = urlrequest.Request(
				f"{API_BASE}/v1/text-to-speech/{voiceId}/stream"
				f"?output_format={self.outputFormat}",
				data=json.dumps(body).encode("utf-8"),
				headers={
					"xi-api-key": key,
					"Content-Type": "application/json",
					"Accept": "*/*",
				},
				method="POST",
			)
			try:
				with urlrequest.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
					resp.read()
				return PROBE_OK, ""
			except HTTPError as e:
				detail = _REQUEST_ID_PATTERN.sub("", self._readProbeDetail(e))
				# The audio format is settled here too, on the first request of
				# the session, so speech never has to discover it later.
				if e.code == 403 and _isOutputFormatRejection(detail) \
						and self._useNextOutputFormat():
					continue
				return _classifyVoiceFailure(e.code, detail), detail.strip()
			except (URLError, OSError) as e:
				return PROBE_UNKNOWN, str(e)

	@staticmethod
	def _readProbeDetail(error):
		try:
			return error.read().decode("utf-8", "replace")[:300]
		except Exception:
			return ""

	def refreshVoiceListInGui(self):
		"""Tells NVDA the voice list has changed, without a restart.

		The settings ring holds a SynthSetting object per setting, built when the
		voice last changed, so it is rebuilt through NVDA's own hook. The voice
		settings panel needs nothing: it is destroyed when the dialog closes, so
		reopening it reads the list afresh.
		"""
		def refresh():
			try:
				ring = getattr(globalVars, "settingsRing", None)
				if ring is not None:
					ring.updateSupportedSettings(self)
			except Exception:
				log.debugWarning("ElevenLabs: could not refresh the settings ring", exc_info=True)

		# Touches NVDA's interface, so it has to happen on the main thread.
		queueHandler.queueFunction(queueHandler.eventQueue, refresh)

	def _markVoiceUnusable(self, voiceId, detail):
		"""Drops a voice that fails during use, so it can't be chosen again."""
		if voiceId in self._unusableVoices or voiceId not in self._allVoices:
			return
		self._unusableVoices.add(voiceId)
		log.warning(
			f"ElevenLabs: the voice {self._allVoices.get(voiceId, voiceId)!r} cannot be used "
			f"with this account, so it has been removed from the list. {detail}"
		)
		self._rebuildVoiceList()
		if self._voiceId == voiceId:
			self._voiceId = None
		self._saveUnusableVoices()
		self.refreshVoiceListInGui()

	def _get_availableVoices(self):
		# Overrides the property rather than NVDA's _getAvailableVoices hook, so
		# the list is never cached. Anything that reads it, including a settings
		# panel built after a re-check, sees the current voices.
		with self._voicesLock:
			return OrderedDict(self._voicesCache)

	def _get_voice(self):
		with self._voicesLock:
			voices = self._voicesCache
			if self._voiceId in voices:
				return self._voiceId
			return next(iter(voices)) if voices else FALLBACK_VOICE_ID

	def _set_voice(self, value):
		if not value:
			return
		with self._voicesLock:
			known = value in self._voicesCache
		if known:
			self._voiceId = value
			return
		reason = (
			"cannot be used with this account"
			if value in self._unusableVoices
			else "is not on this account"
		)
		log.debugWarning(f"ElevenLabs: the voice {value!r} {reason}; using another one instead.")
		self._voiceId = None

	def _get_synthesisVoice(self):
		"""The voice id to actually send."""
		return self._get_voice()

	# --- Language ---
	# Shown as the "Language" combo box in NVDA's voice settings and in the
	# settings ring. The setting id is elvlanguage, so NVDA looks up the choices
	# in _get_availableElvlanguages.

	def _get_availableElvlanguages(self):
		return OrderedDict(
			(langId, StringParameterInfo(langId, name))
			for langId, name in LANGUAGES.items()
		)

	def _get_elvlanguage(self):
		return self._language

	def _set_elvlanguage(self, value):
		if value in LANGUAGES:
			self._language = value
		else:
			log.warning(f"ElevenLabs: unknown language {value!r}; using automatic detection.")
			self._language = "auto"

	def _get_language(self):
		# Reported to NVDA so symbol pronunciation follows the chosen language.
		# None means "no opinion", so NVDA keeps using its own language.
		return None if self._language == "auto" else self._language

	def _get_availableLanguages(self):
		return {langId for langId in LANGUAGES if langId != "auto"}

	def _migrateLegacyVariantSetting(self):
		"""Picks up the language from the old variant setting used up to 0.8.0."""
		try:
			legacy = config.conf["speech"][self.name].get("variant")
		except Exception:
			return
		if legacy in LANGUAGES:
			self._language = legacy

	# --- Rate and volume ---

	def _get_rate(self):
		return self._rate

	def _set_rate(self, value):
		self._rate = max(0, min(100, int(value)))

	def _get_voiceSettings(self):
		"""The voice_settings block sent with each utterance."""
		return {
			# Lower stability gives a wider emotional range, higher stability a
			# flatter, more repeatable voice.
			"stability": round(self._stability / 100.0, 2),
			"similarity_boost": round(self._clarity / 100.0, 2),
			"style": round(self._style / 100.0, 2),
			"use_speaker_boost": self._speakerBoost,
			"speed": self.apiSpeed,
		}

	def _get_stability(self):
		return self._stability

	def _set_stability(self, value):
		self._stability = max(0, min(100, int(value)))

	def _get_clarity(self):
		return self._clarity

	def _set_clarity(self, value):
		self._clarity = max(0, min(100, int(value)))

	def _get_style(self):
		return self._style

	def _set_style(self, value):
		self._style = max(0, min(100, int(value)))

	def _get_speakerBoost(self):
		return self._speakerBoost

	def _set_speakerBoost(self, value):
		self._speakerBoost = bool(value)

	def _get_rateBoost(self):
		return self._rateBoost

	def _set_rateBoost(self, enable):
		self._rateBoost = bool(enable)

	def _get_volume(self):
		return self._volume

	def _set_volume(self, value):
		self._volume = max(0, min(100, int(value)))
		player = self._player
		if player is not None and self._playerHasVolume:
			self._applyPlayerVolume(player)

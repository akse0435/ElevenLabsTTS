# -*- coding: utf-8 -*-
# ElevenLabs speech synthesizer driver for NVDA.
#
# Setup: set your ElevenLabs API key in NVDA's settings, ElevenLabs TTS
# category, then select "ElevenLabs" as the synthesizer. The key and what the
# driver learns about the account are kept in elevenlabs_settings.json; see
# _elevenlabsSettings.
#
# All speech is sent to ElevenLabs and counts towards your API usage. Speeds
# the API can't deliver itself are made locally with sonic, which NVDA ships
# from 2025.1 onwards, hence the minimum version.

import array
import hashlib
import http.client
import io
import json
import queue
import re
import select
import struct
import threading
import time
from collections import OrderedDict, namedtuple
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
	getSynth,
	synthDoneSpeaking,
	synthIndexReached,
)
from synthDrivers import _elevenlabsSettings as _settings

try:
	import addonHandler

	# Installs _ and pgettext for the add-on's own translations in locale.
	addonHandler.initTranslation()
except Exception:
	log.debugWarning("ElevenLabs: translations unavailable", exc_info=True)

API_BASE = "https://api.elevenlabs.io"
CHUNK_SIZE = 4096
# Seconds without audio before giving up. Measured worst: ~1 s real-time, ~2.3 s others.
REQUEST_TIMEOUT = 15
REALTIME_REQUEST_TIMEOUT = 8
# Seconds a finished connection is kept for the next request. Reusing one saves
# the TLS handshake before each utterance; reconnecting after a pause is cheap.
CONNECTION_IDLE_TIMEOUT = 60
# One for the part playing and one for the next part's prefetch.
MAX_IDLE_CONNECTIONS = 2
# Timeout for the checks run while NVDA waits for the synthesizer to load. If
# they fail, NVDA falls back to another synthesizer.
INIT_TIMEOUT = 8
# Voices and models are checked with the shortest possible sample, and the
# result is cached, so the few credits it costs are paid once per account state.
PROBE_TEXT = "."
PROBE_TIMEOUT = 10
PROBE_WORKERS = 4
# Keeps the check short for accounts with very large voice libraries.
MAX_PROBED_VOICES = 100
# Bumped whenever a check changes, so results cached by an older version are redone.
VOICE_CHECK_VERSION = 2
MODEL_CHECK_VERSION = 2
# Voices tried at start up before giving up, which bounds how long NVDA waits.
MAX_STARTUP_TRIES = 8
# Upper bounds on each check, so NVDA is never held back indefinitely.
VOICE_CHECK_DEADLINE = 20
MODEL_CHECK_DEADLINE = 15
# Kept low, since the free plan allows few concurrent requests per model.
MODEL_PROBE_WORKERS = 2

# One retry covers a dropped connection or a busy server without much delay.
MAX_RETRIES = 1
RETRY_DELAY = 0.5
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)
# v4 trains a voice the first time it is used with it, which can take well over
# REQUEST_TIMEOUT, so a chosen model's first request with a voice may wait this long.
FIRST_USE_TIMEOUT = 60
# How long new speech waits for a cancelled request for another voice or model to
# end before sending its own, so a burst of cancelled utterances can't pile up. One
# for the same voice and model is waited out, as it may be training that voice.
PREVIOUS_REQUEST_GRACE = 0.5
# How often the speech thread looks up while waiting, to notice a cancel.
POLL_INTERVAL = 0.05
# Silence mid-utterance, in seconds, long enough to log.
GAP_TO_LOG = 0.3

# Long text is split between sentences; see ModelInfo.parts.
_SENTENCE_END = re.compile(r"(?<=[.!?\u2026])\s+|\n+")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")
_WHITESPACE = re.compile(r"\s+")

# Audio formats, best first, as (output_format, sample rate). The plan decides
# which are allowed (PCM at 44.1 kHz needs Pro, the lower PCM rates Creator), so
# on HTTP 403 the driver steps down the list. ulaw_8000 works on every plan.
OUTPUT_FORMATS = (
	("pcm_44100", 44100),
	("pcm_24000", 24000),
	("pcm_22050", 22050),
	("pcm_16000", 16000),
	("ulaw_8000", 8000),
)

# Speech rate. The API's speed only covers 0.7 to 1.2, so the API is asked for
# what it can do and sonic stretches the rest. Normal speed sits at rate 50.
API_SPEED_RANGE = (0.7, 1.2)
SPEED_AT_MIN_RATE = 0.5
SPEED_AT_RATE_50 = 1.0
SPEED_AT_MAX_RATE = 3.0
# Rate boost stretches the top of the slider further, as NVDA's own drivers do.
SPEED_AT_MAX_RATE_BOOSTED = 6.0
# Below this much stretching sonic is skipped, since it can only add artefacts.
SONIC_THRESHOLD = 0.01

# Voice setting sliders run from 0 to 100 and the API from 0.0 to 1.0. The
# defaults are the values ElevenLabs recommends.
DEFAULT_STABILITY = 50  # stability 0.5
DEFAULT_CLARITY = 75  # similarity_boost 0.75
DEFAULT_STYLE = 0  # style 0.0
DEFAULT_SPEAKER_BOOST = True
DEFAULT_NORMALIZATION = "auto"
# Choices for apply_text_normalization; "auto" leaves it out.
NORMALIZATIONS = OrderedDict((
	# Translators: a choice for text normalization.
	("auto", _("Automatic")),
	# Translators: a choice for text normalization.
	("on", _("On")),
	# Translators: a choice for text normalization.
	("off", _("Off")),
))
DEFAULT_SPLIT_TEXT = True
DEFAULT_AUDIO_TAGS = False

# Audio tags. NVDA drops square brackets before speech reaches a driver, so while
# tags are on, those of a tag are swapped for private use characters, which NVDA
# leaves alone, and swapped back in speak.
_TAG_OPEN = "\ue000"
_TAG_CLOSE = "\ue001"
# A tag, or either half of one that a line break split in two.
_TAG_IN_NVDA_TEXT = re.compile(r"\[[^\[\]\n]{1,100}\]|\[[^\[\]\n]{1,100}$|^[^\[\]\n]{1,100}\]")
_TAG = re.compile(r"\[[^\[\]]*\]")
_LEADING_TAG = re.compile(r"\s*\[[^\[\]]*\]")
# The only stability values v3 accepts. Also used for any model that refuses others.
STABILITY_PRESETS = (0.0, 0.5, 1.0)
# Their names in the stability box shown instead of the slider for such models,
# as ElevenLabs names them.
STABILITY_PRESET_NAMES = OrderedDict((
	# Translators: a choice for stability on models that take only three values;
	# the widest emotional range.
	("creative", _("Creative")),
	# Translators: a choice for stability on models that take only three values;
	# balanced and closest to the original voice.
	("natural", _("Natural")),
	# Translators: a choice for stability on models that take only three values;
	# very steady, but less responsive to direction.
	("robust", _("Robust")),
))
STABILITY_PRESET_VALUES = dict(zip(STABILITY_PRESET_NAMES, STABILITY_PRESETS))
DEFAULT_STABILITY_PRESET = "natural"

# What a model accepts: whether it takes language_code, and which voice_settings
# and apply_text_normalization. Turbo takes the same as Flash, which ElevenLabs
# calls its functional equivalent. Fields a model refuses are learned; see _refused.
# realtime: shorter timeout. maxSpeed: measured speed it keeps up with.
# parts: (first, later) part sizes in characters. Only Multilingual v2's wait
# grows much with length; the others split for the limit.
# tags: takes audio tags such as [whispers]. englishOnly: shown as such.
ModelInfo = namedtuple(
	"ModelInfo",
	("name", "languageCode", "settings", "realtime", "maxSpeed", "parts", "tags", "englishOnly"),
	defaults=(True, None, (1000, 1000), False, False),
)
_MULTILINGUAL_SETTINGS = (
	"stability", "similarity_boost", "style", "use_speaker_boost", "speed", "apply_text_normalization")
_FLASH_SETTINGS = ("stability", "similarity_boost", "speed", "apply_text_normalization")
_V3_SETTINGS = ("stability", "apply_text_normalization")
_V4_SETTINGS = ("stability", "similarity_boost", "apply_text_normalization")
# Models not in MODELS are first assumed to take everything.
_ALL_SETTINGS = _MULTILINGUAL_SETTINGS
# Text to speech models in the order they were released, as shown in the model box.
# The account's own list decides which are offered; see _offeredModels.
MODELS = OrderedDict((
	("eleven_multilingual_v2", ModelInfo(
		"Eleven Multilingual v2", False, _MULTILINGUAL_SETTINGS, realtime=False, parts=(150, 400))),
	("eleven_turbo_v2", ModelInfo("Eleven Turbo v2", False, _FLASH_SETTINGS, englishOnly=True)),
	("eleven_turbo_v2_5", ModelInfo("Eleven Turbo v2.5", True, _FLASH_SETTINGS)),
	("eleven_flash_v2", ModelInfo("Eleven Flash v2", False, _FLASH_SETTINGS, englishOnly=True)),
	("eleven_flash_v2_5", ModelInfo("Eleven Flash v2.5", True, _FLASH_SETTINGS)),
	("eleven_v3", ModelInfo("Eleven v3", True, _V3_SETTINGS, realtime=False, maxSpeed=1.4, tags=True)),
	("eleven_v3_conversational", ModelInfo("Eleven v3 Conversational", True, _V3_SETTINGS, tags=True)),
	("eleven_v4", ModelInfo("Eleven v4", True, _V4_SETTINGS, realtime=False, tags=True)),
	("eleven_v4_turbo", ModelInfo("Eleven v4 Turbo", True, _V4_SETTINGS, tags=True)),
))
# The default. Every plan has it, so voices and audio formats are checked with
# it, and it speaks whenever the chosen model refuses to.
DEFAULT_MODEL = "eleven_flash_v2_5"
# Takes over when ElevenLabs can't speak at all, e.g. offline or out of credits.
FALLBACK_SYNTH = "espeak"
# Models older than those in MODELS, so the account's list keeps release order.
_EARLIER_MODELS = ("eleven_monolingual_v1", "eleven_multilingual_v1")
# The models the account offers, in release order, or MODELS if its list can't be read.
_offeredModels = OrderedDict(MODELS)
# Fields each model has refused, saved as refused_<model> in the settings file.
_refused = {}
# Recorded instead of apply_text_normalization when only "on" was refused, which
# Flash and Turbo v2.5 allow only on Enterprise plans; auto and off still work.
_NORMALIZATION_ON_REFUSED = "apply_text_normalization=on"
# Fields a refusal may name; only those a model was sent are recorded.
_LEARNABLE_FIELDS = ("language_code",) + _ALL_SETTINGS + (_NORMALIZATION_ON_REFUSED,)
# Models known to take only the stability presets.
PRESET_STABILITY_MODELS = ("eleven_v3", "eleven_v3_conversational")
# The voice_settings name behind each slider and checkbox. Only those the chosen
# model takes are shown in NVDA.
_SETTING_API_NAMES = {
	"stability": "stability",
	"clarity": "similarity_boost",
	"style": "style",
	"speakerBoost": "use_speaker_boost",
	"normalization": "apply_text_normalization",
}

# Where to set the API key, for log messages.
KEY_LOCATION = "NVDA's settings, ElevenLabs TTS category"

# Fallback voice (Rachel), used until the real voice list arrives.
FALLBACK_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"

# Id of the language box. Not "language", since NVDA uses availableLanguages for
# a set of codes. NVDA finds the choices via id.capitalize(), so the id must be
# a single lowercase word (here: _get_availableElvlanguages).
LANGUAGE_SETTING_ID = "elvlanguage"

# Choices in the language box (ISO 639-1): the languages Flash v2.5 supports.
# "auto" omits language_code, letting the model detect the language itself.
LANGUAGES = OrderedDict((
	("auto", "Automatic (detect language)"),
	("ar", "Arabic"),
	("bg", "Bulgarian"),
	("zh", "Chinese"),
	("hr", "Croatian"),
	("cs", "Czech"),
	("da", "Danish"),
	("nl", "Dutch"),
	("en", "English"),
	("fil", "Filipino"),
	("fi", "Finnish"),
	("fr", "French"),
	("de", "German"),
	("el", "Greek"),
	("hi", "Hindi"),
	("hu", "Hungarian"),
	("id", "Indonesian"),
	("it", "Italian"),
	("ja", "Japanese"),
	("ko", "Korean"),
	("ms", "Malay"),
	("no", "Norwegian"),
	("pl", "Polish"),
	("pt", "Portuguese"),
	("ro", "Romanian"),
	("ru", "Russian"),
	("sk", "Slovak"),
	("es", "Spanish"),
	("sv", "Swedish"),
	("ta", "Tamil"),
	("tr", "Turkish"),
	("uk", "Ukrainian"),
	("vi", "Vietnamese"),
))

DEFAULT_LANGUAGE = "auto"

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

	NVDA only runs on Windows, so array's native order is the API's little endian,
	and a gain of at most 1 cannot clip."""
	samples = array.array("h")
	samples.frombytes(data)
	for index in range(len(samples)):
		samples[index] = int(samples[index] * gain)
	return samples.tobytes()


def _isOutputFormatRejection(detail):
	"""True if a 403 body complains about the requested audio format.

	The API has used both "output_format" and "output format" in these errors."""
	detail = detail.lower()
	return "output_format" in detail or "output format" in detail


class ElevenLabsUnavailable(Exception):
	"""The synthesizer cannot be used, so NVDA should fall back to another one."""


# Outcomes of trying a voice or model: usable, definitely not usable with this
# account, or no clear answer. Only a definite answer hides anything, so a
# network hiccup can't make voices or models disappear.
PROBE_OK = "ok"
PROBE_UNUSABLE = "unusable"
PROBE_UNKNOWN = "unknown"
# Something wrong with the account itself, e.g. an exhausted quota or a
# rejected key. Everything would fail, so checking stops.
PROBE_ACCOUNT = "account"
# The model was refused, not the voice, so nothing is concluded about the voice.
PROBE_MODEL = "model"

# Statuses that mean the request never got a real answer.
TRANSIENT_STATUS_CODES = (408, 425, 429)

# Wording that points at the account rather than any one voice or model.
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
# Account wording that outranks a mention of the model.
_QUOTA_OR_KEY_TERMS = ("quota", "insufficient", "api_key", "api key", "unusual activity", "unusual_activity")
# Wording that blames the voice or model. It outranks the account terms, since
# these messages often mention the subscription as well.
_VOICE_OR_MODEL_TERMS = (
	"voice",
	"voice_id",
	"model",
)


def _classifyRefusal(status, detail):
	"""Decides whether a refused check is down to the voice or model tried, or the account.

	By then the key, audio format and default model are known to work and the text
	is one character, so any refusal of the request itself counts against what was
	tried. Only clearly transient answers count as no answer."""
	detail = detail.lower()
	if status in TRANSIENT_STATUS_CODES or status >= 500:
		return PROBE_UNKNOWN
	# Naming the model and not the voice blames the model; naming both, the voice.
	if 400 <= status < 500 and "model" in detail and "voice" not in detail \
			and not any(term in detail for term in _QUOTA_OR_KEY_TERMS):
		return PROBE_MODEL
	blamesItem = any(term in detail for term in _VOICE_OR_MODEL_TERMS)
	if not blamesItem and any(term in detail for term in _ACCOUNT_REJECTION_TERMS):
		return PROBE_ACCOUNT
	if 400 <= status < 500:
		return PROBE_UNUSABLE
	return PROBE_UNKNOWN


def _isRefusal(status):
	"""True for a 4xx answer that is neither transient nor about the API key."""
	return 400 <= status < 500 and status != 401 and status not in TRANSIENT_STATUS_CODES


def _modelInfo(modelId):
	"""What a model accepts, less the fields it refused; unknown ids count as the default."""
	info = _offeredModels.get(modelId) or MODELS.get(modelId) or MODELS[DEFAULT_MODEL]
	refused = _refused.get(modelId)
	if refused:
		info = info._replace(
			languageCode=info.languageCode and "language_code" not in refused,
			settings=tuple(name for name in info.settings if name not in refused),
		)
	return info


def _withoutDeclined(settings, model):
	"""settings less those the model list says the model can't use.

	Only an explicit false counts, and it can only take settings away, so the
	list can't bring back one the model is known to refuse."""
	declined = {
		field for flag, field in (("can_use_style", "style"), ("can_use_speaker_boost", "use_speaker_boost"))
		if model.get(flag) is False
	}
	return tuple(name for name in settings if name not in declined)


def _modelDisplayName(info):
	if info.englishOnly:
		# Translators: a model that only speaks English; {name} is its name.
		return _("{name} (English only)").format(name=info.name)
	return info.name


def _languageName(langId, englishName):
	"""A language's name in NVDA's language, as NVDA itself names it."""
	if langId == "auto":
		# Translators: the choice that lets ElevenLabs detect the language.
		return _("Automatic (detect language)")
	try:
		import languageHandler

		return languageHandler.getLanguageDescription(langId) or englishName
	except Exception:
		return englishName


def _requestTimeout(modelId):
	return REALTIME_REQUEST_TIMEOUT if _modelInfo(modelId).realtime else REQUEST_TIMEOUT


def _group(pieces, limit, firstLimit=None):
	"""Joins pieces with spaces into parts of at most limit characters."""
	parts, current = [], ""
	for piece in pieces:
		piece = piece.strip()
		if not piece:
			continue
		cap = firstLimit if firstLimit and not parts else limit
		if current and len(current) + 1 + len(piece) > cap:
			parts.append(current)
			current = piece
		else:
			current = f"{current} {piece}" if current else piece
	if current:
		parts.append(current)
	return parts


def _splitLong(text, limit):
	"""Splits text longer than limit at clauses, then words, then anywhere."""
	if len(text) <= limit:
		return [text]
	for pattern in (_CLAUSE_END, _WHITESPACE):
		pieces = pattern.split(text)
		if len(pieces) > 1:
			return [p for piece in _group(pieces, limit) for p in _splitLong(piece, limit)]
	return [text[i:i + limit] for i in range(0, len(text), limit)]


def _splitText(text, first, later):
	"""Splits text between sentences; only a sentence over later is split inside."""
	if len(text) <= first:
		return [text]
	pieces = []
	for sentence in _SENTENCE_END.split(text):
		pieces.extend(_splitLong(sentence.strip(), later))
	return _group(pieces, later, first) or [text]


def _hideTags(text):
	"""Swaps the brackets of audio tags in text for characters NVDA keeps."""
	def hide(match):
		tag = match.group()
		# A lone bracket, e.g. read as a character, isn't a tag.
		if not any(c.isalpha() for c in tag):
			return tag
		return tag.replace("[", _TAG_OPEN).replace("]", _TAG_CLOSE)
	return _TAG_IN_NVDA_TEXT.sub(hide, text)


def _installTagFilter():
	"""Wraps NVDA's processText so audio tags reach the driver when they're on."""
	try:
		import speech.speech as nvdaSpeech
		original = nvdaSpeech.processText
	except (ImportError, AttributeError):
		log.warning("ElevenLabs: audio tags are unavailable in this NVDA version.")
		return
	if getattr(original, "_elevenLabsTags", False):
		return

	def processText(locale, text, *args, **kwargs):
		try:
			synth = getSynth()
			if getattr(synth, "name", None) == "elevenlabs" and synth._tagsActive() \
					and ("[" in text or "]" in text):
				text = _hideTags(text)
		except Exception:
			log.debugWarning("ElevenLabs: could not look for audio tags", exc_info=True)
		return original(locale, text, *args, **kwargs)

	processText._elevenLabsTags = True
	nvdaSpeech.processText = processText


def _languageSetting():
	"""The language box, labelled with NVDA's own translated strings."""
	nvdaSetting = BaseSynthDriver.LanguageSetting()
	return DriverSetting(
		LANGUAGE_SETTING_ID,
		nvdaSetting.displayNameWithAccelerator,
		availableInSettingsRing=True,
		displayName=nvdaSetting.displayName,
	)


def _openVoicePanels():
	"""NVDA's voice settings panels in any open settings dialog."""
	import wx
	from gui.settingsDialogs import VoiceSettingsPanel

	pending = [w for w in wx.GetTopLevelWindows() if not w.IsBeingDeleted()]
	while pending:
		window = pending.pop()
		if isinstance(window, VoiceSettingsPanel):
			yield window
		else:
			pending.extend(window.GetChildren())


def _sizerWindows(item):
	"""The windows of a control or labelled control sizer, in order."""
	import wx

	if isinstance(item, wx.Window):
		return [item]
	windows = []
	for child in item.GetChildren():
		if child.IsWindow():
			windows.append(child.GetWindow())
		elif child.IsSizer():
			windows.extend(_sizerWindows(child.GetSizer()))
	return windows


def _orderVoicePanel(panel, settingIds):
	"""Puts the driver's controls in panel in the order of settingIds, on screen and
	in tab order.

	NVDA adds the control of a setting that becomes supported after the panel was
	built after all the others it has made, and tab order follows creation, so a
	model change would leave e.g. the stability box after audio tags."""
	import wx

	sizer = panel.settingsSizer
	items = [panel.sizerDict[sid] for sid in settingIds if sid in panel.sizerDict]
	positions = {}
	for index, child in enumerate(sizer.GetChildren()):
		positions[id(child.GetWindow() or child.GetSizer())] = index
	if len(items) < 2 or any(id(item) not in positions for item in items):
		return
	# NVDA keeps them together, before its own voice options; keep that block in place.
	start = min(positions[id(item)] for item in items)
	if [positions[id(item)] for item in items] != list(range(start, start + len(items))):
		for index, item in enumerate(items):
			shown = sizer.IsShown(item)
			sizer.Detach(item)
			# As NVDA inserts them.
			sizer.Insert(start + index, item, border=10, flag=wx.BOTTOM)
			sizer.Show(item, shown)
	windows = [w for item in items for w in _sizerWindows(item)]
	siblings = list(windows[0].GetParent().GetChildren())
	first = min(windows, key=siblings.index)
	if first is not windows[0]:
		windows[0].MoveBeforeInTabOrder(first)
	for previous, window in zip(windows, windows[1:]):
		window.MoveAfterInTabOrder(previous)
	sizer.Layout()


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
				f"ElevenLabs rejected the API key (HTTP {e.code}). Set it again in "
				f"{KEY_LOCATION}. {detail}"
			)
		raise ElevenLabsUnavailable(f"ElevenLabs returned HTTP {e.code} for {url}. {detail}")
	except (URLError, OSError) as e:
		raise ElevenLabsUnavailable(f"Could not reach ElevenLabs: {e}")
	except ValueError as e:
		raise ElevenLabsUnavailable(f"ElevenLabs sent a response that could not be read: {e}")


def checkApiKey(key):
	"""Asks ElevenLabs whether key works; None if it does, else a message for the user.

	The voice list is what the driver can't start without, so that is what's asked for.
	Blocks for up to INIT_TIMEOUT, so it must not run on the main thread."""
	if not key.isascii() or not key.isprintable() or " " in key:
		# Can't be sent in a header, and is no API key anyway.
		# Translators: shown when ElevenLabs refuses the API key that was entered.
		return _(
			"ElevenLabs rejected this API key. Check that it was copied in full and "
			"that it is allowed to read voices."
		)
	req = urlrequest.Request(f"{API_BASE}/v1/voices", headers={
		"xi-api-key": key,
		"Accept": "application/json",
	})
	try:
		with urlrequest.urlopen(req, timeout=INIT_TIMEOUT) as resp:
			resp.read()
		return None
	except HTTPError as e:
		log.debug(f"ElevenLabs: the API key was refused (HTTP {e.code}).")
		if e.code in (401, 403):
			# Translators: shown when ElevenLabs refuses the API key that was entered.
			return _(
				"ElevenLabs rejected this API key. Check that it was copied in full and "
				"that it is allowed to read voices."
			)
		# Translators: shown when the API key can't be checked; {code} is an HTTP status code.
		return _("ElevenLabs could not check the key (HTTP {code}). Try again later.").format(code=e.code)
	except (URLError, OSError) as e:
		log.debug(f"ElevenLabs: could not reach ElevenLabs to check the API key: {e}")
		# Translators: shown when ElevenLabs can't be reached to check the API key.
		return _("Could not reach ElevenLabs to check the key. Check your internet connection and try again.")


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


class _ConnectionPool:
	"""Keeps finished HTTPS connections open for a while, so the next request
	skips the TCP and TLS handshake."""

	def __init__(self):
		self._lock = threading.Lock()
		# (host, connection, time.monotonic() when it was last finished), oldest first.
		self._idle = []
		self._timer = None
		self._closed = False
		self._loggedProxy = False

	def request(self, req, timeout):
		"""Sends req; returns (connection, response, reused), or None when a proxy
		is set, which urlopen handles instead."""
		if "https" in urlrequest.getproxies() and not urlrequest.proxy_bypass(req.host):
			if not self._loggedProxy:
				self._loggedProxy = True
				log.debug("ElevenLabs: a proxy is set, so connections are not kept open.")
			return None
		headers = dict(req.header_items())
		# urlopen sends one, and a request without it could be treated as a bot.
		headers.setdefault("User-agent", f"Python-urllib/{urlrequest.__version__}")
		while True:
			conn = self._take(req.host)
			reused = conn is not None
			if reused:
				conn.timeout = timeout
				conn.sock.settimeout(timeout)
			else:
				conn = http.client.HTTPSConnection(req.host, timeout=timeout)
			try:
				conn.request(req.get_method(), req.selector, req.data, headers)
				return conn, conn.getresponse(), reused
			except (http.client.BadStatusLine, ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
				conn.close()
				if not reused:
					raise
				# The server closed it without the check in _take noticing, so the
				# request never arrived and is safe to send again.
				log.debug("ElevenLabs: a kept connection was closed by the server; reconnecting.")
			except BaseException:
				conn.close()
				raise

	def _take(self, host):
		"""The most recently used idle connection to host that is still open, or None."""
		now = time.monotonic()
		with self._lock:
			while self._idle:
				idleHost, conn, since = self._idle.pop()
				if idleHost == host and now - since < CONNECTION_IDLE_TIMEOUT and self._isOpen(conn):
					self._scheduleSweep()
					return conn
				conn.close()
			self._scheduleSweep()
		return None

	@staticmethod
	def _isOpen(conn):
		# An idle connection has nothing to read unless the server closed it.
		if conn.sock is None:
			return False
		try:
			readable, _, _ = select.select([conn.sock], [], [], 0)
		except (OSError, ValueError):
			return False
		return not readable

	def release(self, host, conn, resp):
		"""Keeps conn for the next request if resp was read to the end, else closes it."""
		if not resp.isclosed() or resp.will_close or conn.sock is None:
			conn.close()
			return
		with self._lock:
			if self._closed:
				dropped = [conn]
			else:
				self._idle.append((host, conn, time.monotonic()))
				dropped = [entry[1] for entry in self._idle[:-MAX_IDLE_CONNECTIONS]]
				del self._idle[:-MAX_IDLE_CONNECTIONS]
				self._scheduleSweep()
		for extra in dropped:
			extra.close()

	def _scheduleSweep(self):
		"""Arranges for the oldest idle connection to be closed once it expires.
		Called with the lock held."""
		if self._timer is not None:
			self._timer.cancel()
			self._timer = None
		if not self._idle:
			return
		delay = self._idle[0][2] + CONNECTION_IDLE_TIMEOUT - time.monotonic()
		self._timer = threading.Timer(max(delay, 0), self._sweep)
		self._timer.daemon = True
		self._timer.start()

	def _sweep(self):
		now = time.monotonic()
		with self._lock:
			expired = [entry for entry in self._idle if now - entry[2] >= CONNECTION_IDLE_TIMEOUT]
			self._idle = [entry for entry in self._idle if entry not in expired]
			self._scheduleSweep()
		for _, conn, _ in expired:
			conn.close()
		if expired:
			log.debug(f"ElevenLabs: closed {len(expired)} connection(s) unused for {CONNECTION_IDLE_TIMEOUT} s.")

	def closeAll(self):
		with self._lock:
			self._closed = True
			idle, self._idle = self._idle, []
			if self._timer is not None:
				self._timer.cancel()
				self._timer = None
		for _, conn, _ in idle:
			conn.close()


class _Download(threading.Thread):
	"""One request on a thread of its own, so speech can move on without waiting for it."""

	daemon = True

	def __init__(self, req, timeout, pool=None):
		super().__init__(name="ElevenLabsDownload")
		self._req = req
		self._timeout = timeout
		self._pool = pool
		# Whether the request went over a connection kept from an earlier one.
		self.reused = False
		self._onDone = None
		self._done = False
		self._doneLock = threading.Lock()
		self._wanted = True
		# Matches a prefetched download to its request.
		self.key = (req.full_url, req.data)
		# Audio chunks as they arrive, b"" at the end, or the exception that stopped it.
		self.chunks = queue.Queue()
		self.started = time.time()
		self.firstAudio = None
		self.start()

	def abandon(self):
		"""Stops passing audio on; the connection closes when the server next answers."""
		self._wanted = False

	def whenDone(self, callback):
		"""Calls callback once all audio has arrived, now if it already has."""
		with self._doneLock:
			if not self._done:
				self._onDone = callback
				return
		callback()

	def _finish(self):
		with self._doneLock:
			self._done = True
			callback = self._onDone
		if callback is not None:
			try:
				callback()
			except Exception:
				log.error("ElevenLabs: could not fetch the next part", exc_info=True)

	def run(self):
		conn = resp = None
		try:
			opened = self._pool.request(self._req, self._timeout) if self._pool is not None else None
			if opened is None:
				resp = urlrequest.urlopen(self._req, timeout=self._timeout)
			else:
				conn, resp, self.reused = opened
				if resp.status >= 400:
					# Raised as urlopen would, so errors are handled the same either way.
					# Reading the body first leaves the connection fit to reuse.
					body = resp.read()
					raise HTTPError(self._req.full_url, resp.status, resp.reason, resp.headers, io.BytesIO(body))
			while self._wanted:
				chunk = resp.read(CHUNK_SIZE)
				if not chunk:
					break
				if self.firstAudio is None:
					self.firstAudio = time.time()
				self.chunks.put(chunk)
			else:
				return
		except http.client.HTTPException as e:
			# E.g. a connection cut off mid-answer, which counts as a network error.
			self.chunks.put(URLError(e))
			return
		except Exception as e:
			self.chunks.put(e)
			return
		finally:
			# Kept only if read to the end; an abandoned answer closes the connection.
			if conn is not None:
				self._pool.release(self._req.host, conn, resp)
			elif resp is not None:
				resp.close()
		# Before b"", so the next part starts before this one ends.
		self._finish()
		self.chunks.put(b"")


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
		# (voice, model) pairs a chosen model has spoken with this session.
		self._usedVoices = set()
		# The last download, and the last one for each (voice, model) pair.
		self._download = None
		self._pending = {}
		# The next part's download.
		self._prefetch = None
		# (generation, tag): the last audio tag spoken, until speech is cancelled.
		self._carriedTag = (None, None)
		# When the audio fed so far should end playing; None before the first feed.
		self._playEnd = None
		self._connections = _ConnectionPool()
		self.start()

	def enqueue(self, text, indexes, langOverride=None):
		with self._lock:
			gen = self.generation
		self.queue.put((gen, text, indexes, langOverride))

	def cancelAll(self):
		with self._lock:
			self.generation += 1
		self._dropPrefetch()
		# Empty the queue.
		try:
			while True:
				self.queue.get_nowait()
		except queue.Empty:
			pass

	def stop(self):
		self._running = False
		self.cancelAll()
		self._connections.closeAll()
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
				completed = self._speakParts(gen, text, langOverride)
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

	def _speakParts(self, gen, text, langOverride):
		"""Speaks text, in parts when splitting is on."""
		modelId = self.driver.model
		self.driver._warnIfTooFast(modelId)
		parts = _splitText(text, *_modelInfo(modelId).parts) if self.driver.splitText else [text]
		# Joined screen lines can hold double spaces, which v3 may speak as a breath.
		parts = [p for p in (" ".join(p.split()) for p in parts) if p]
		if self.driver._tagsActive() and _modelInfo(modelId).tags:
			parts = [self._withTag(gen, p) for p in parts]
			# Tags alone are refused (HTTP 400), but still carry over to the next text.
			parts = [p for p in parts if _TAG.sub("", p).strip()]
		self._playEnd = None
		try:
			for i, part in enumerate(parts):
				if not self._synthesize(gen, part, langOverride, parts[i + 1:]):
					return False
			return True
		finally:
			self._dropPrefetch()

	def _buildRequest(self, key, text, langOverride, modelId):
		"""The speech request for text, and its (voice, model) pair."""
		driver = self.driver
		# Language priority: NVDA's language markup > the language setting > auto.
		lang = langOverride or driver._language
		voiceId = driver.synthesisVoice
		body = driver._requestBody(self._textFor(text, modelId), lang, modelId)
		req = urlrequest.Request(
			f"{API_BASE}/v1/text-to-speech/{voiceId}/stream"
			f"?output_format={driver.outputFormat}",
			data=json.dumps(body).encode("utf-8"),
			headers={
				"xi-api-key": key,
				"Content-Type": "application/json",
				"Accept": "*/*",
			},
			method="POST",
		)
		return req, (voiceId, modelId)

	def _withTag(self, gen, text):
		"""Starts text with the tag still in effect and remembers text's last tag.

		NVDA's say all sends text in utterances, which may split a tag from the
		sentences it applies to. Each request stands alone, so the tag is repeated."""
		carriedGen, tag = self._carriedTag
		if tag and carriedGen == gen and not _LEADING_TAG.match(text):
			text = f"{tag} {text}"
		tags = _TAG.findall(text)
		if tags:
			self._carriedTag = (gen, tags[-1])
		return text

	def _textFor(self, text, modelId):
		"""text as sent to modelId: audio tags are left out where they'd be read,
		e.g. when the default model stands in for the chosen one."""
		if self.driver._tagsActive() and not _modelInfo(modelId).tags:
			text = " ".join(_TAG.sub(" ", text).split())
		return text

	def _startDownload(self, req, pair):
		firstUse = pair[1] != DEFAULT_MODEL and pair not in self._usedVoices
		download = _Download(
			req, FIRST_USE_TIMEOUT if firstUse else _requestTimeout(pair[1]), self._connections
		)
		log.debug(f"ElevenLabs: request to {_modelInfo(pair[1]).name}: {json.loads(req.data)['text'][:200]!r}")
		self._download = self._pending[pair] = download
		return download

	def _prefetchNext(self, gen, text, langOverride):
		"""Starts the next part's download while the current part plays."""
		key = self.driver._getApiKey()
		if not key:
			return
		req, pair = self._buildRequest(key, text, langOverride, self.driver.model)
		with self._lock:
			if gen != self.generation or not self._running:
				return
			old = self._prefetch
			self._prefetch = self._startDownload(req, pair)
		if old is not None:
			old.abandon()

	def _takePrefetch(self, req):
		"""The prefetched download for req, or None."""
		with self._lock:
			download, self._prefetch = self._prefetch, None
		if download is None:
			return None
		if download.key == (req.full_url, req.data):
			return download
		# Settings changed since it started.
		download.abandon()
		return None

	def _trackPlayback(self, nbytes, modelId, text):
		"""Logs when the player must have run dry before this chunk arrived."""
		now = time.time()
		seconds = nbytes / (2.0 * OUTPUT_FORMATS[self.driver._formatIndex][1])
		if self._playEnd is not None and now - self._playEnd >= GAP_TO_LOG:
			log.info(
				f"ElevenLabs: {_modelInfo(modelId).name} ran out of audio for "
				f"{now - self._playEnd:.1f} s in: {text[:60]!r}"
			)
		self._playEnd = max(self._playEnd or now, now) + seconds

	def _dropPrefetch(self):
		with self._lock:
			download, self._prefetch = self._prefetch, None
		if download is not None:
			download.abandon()

	def _synthesize(self, gen, text, langOverride=None, following=()):
		driver = self.driver
		key = driver._getApiKey()
		if not key:
			driver.reportError(
				"nokey",
				f"No ElevenLabs API key is set. Set one in {KEY_LOCATION}.",
			)
			return False
		modelId = driver.model
		retries = 0
		# Set once the network has failed this utterance, so it isn't retried again.
		offline = False
		while True:
			# A cancelled utterance is dropped rather than retried or passed on.
			if not self._isCurrent(gen):
				return False
			if not self._textFor(text, modelId):
				# Only audio tags, which this model would read aloud.
				return True
			isUlaw = driver.outputFormat.startswith("ulaw")
			req, pair = self._buildRequest(key, text, langOverride, modelId)
			voiceId = pair[0]
			firstUse = modelId != DEFAULT_MODEL and pair not in self._usedVoices
			download = self._takePrefetch(req)
			if download is None:
				if not self._waitForPrevious(gen, pair):
					return False
				download = self._startDownload(req, pair)
			if following:
				download.whenDone(lambda: self._prefetchNext(gen, following[0], langOverride))
			player = sonic = None
			fedAudio = False
			received = 0  # bytes of 16 bit audio, before sonic
			carry = b""  # odd trailing byte held back so samples stay aligned
			try:
				while True:
					if not self._isCurrent(gen):
						return False
					try:
						chunk = download.chunks.get(timeout=POLL_INTERVAL)
					except queue.Empty:
						continue
					if isinstance(chunk, Exception):
						raise chunk
					if player is None:
						if firstUse:
							self._usedVoices.add(pair)
						if download.firstAudio is not None:
							log.debug(
								f"ElevenLabs: {_modelInfo(modelId).name} first audio after "
								f"{download.firstAudio - download.started:.2f} s, {len(text)} chars, "
								f"{'kept' if download.reused else 'new'} connection"
								f"{', first use of voice ' + voiceId if firstUse else ''}."
							)
						player = driver._getPlayer()
						# None when the API delivers the whole speed on its own.
						sonic = driver._prepareSonic(modelId)
					if not chunk:
						break
					if isUlaw:
						chunk = _decodeUlaw(chunk)
					received += len(chunk)
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
					self._trackPlayback(len(chunk), modelId, text)
					# feed blocks until there is room in the playback buffer
					player.feed(chunk)
					fedAudio = True
				if sonic is not None:
					chunk = _sonicDrain(sonic)
					if chunk:
						self._trackPlayback(len(chunk), modelId, text)
						player.feed(chunk)
						fedAudio = True
			except HTTPError as e:
				detail = _REQUEST_ID_PATTERN.sub("", self._readErrorDetail(e))
				# A format the default model rejects is one the plan doesn't allow,
				# so the same utterance is retried with the next one down.
				if modelId == DEFAULT_MODEL and e.code == 403 and _isOutputFormatRejection(detail) \
						and driver._useNextOutputFormat():
					continue
				if e.code in RETRY_STATUS_CODES and not fedAudio and retries < MAX_RETRIES:
					retries += 1
					if self._waitBeforeRetry(gen):
						continue
					return False
				# Stability the model won't take is snapped to the presets.
				if not fedAudio and _isRefusal(e.code) and "stability" in detail.lower() \
						and driver._usePresetStability(modelId):
					continue
				# A field the model won't take is left out from now on.
				if not fedAudio and _isRefusal(e.code) \
						and driver._refuseFields(modelId, detail, json.loads(req.data)):
					continue
				refusal = _classifyRefusal(e.code, detail) if not fedAudio else None
				# Out of credits, or the key no longer works: no model can speak.
				if not fedAudio and (e.code in (401, 402) or refusal == PROBE_ACCOUNT):
					driver._fallBack(f"the account refused speech (HTTP {e.code}). {detail.strip()}")
					return False
				# A model the account refuses is left out, and the default one speaks.
				if refusal == PROBE_MODEL and driver._rejectModel(modelId, detail.strip()):
					modelId = DEFAULT_MODEL
					continue
				# A voice the chosen model refuses is left out for that model.
				if refusal == PROBE_UNUSABLE and modelId != DEFAULT_MODEL \
						and modelId == driver.model and "voice" in detail.lower():
					driver._markVoiceUnusable(voiceId, detail.strip())
					if driver.synthesisVoice != voiceId:
						continue
				# Anything else the chosen model can't do is spoken with the
				# default model, so NVDA never falls silent over a model.
				if not fedAudio and modelId != DEFAULT_MODEL and e.code != 401:
					driver._reportModelFailure(modelId, f"HTTP {e.code}: {detail.strip()}")
					modelId = DEFAULT_MODEL
					continue
				# A voice that stops working mid-session, e.g. after a downgrade,
				# is taken out of the list and another voice is tried.
				if refusal == PROBE_UNUSABLE and modelId == driver.model:
					driver._markVoiceUnusable(voiceId, detail.strip())
					if driver.synthesisVoice != voiceId:
						continue
				# The service itself keeps failing, even after a retry.
				if not fedAudio and e.code >= 500:
					driver._fallBack(f"ElevenLabs keeps failing (HTTP {e.code}). {detail.strip()}")
					return False
				driver.reportError(f"http{e.code}", f"ElevenLabs API error {e.code}: {detail}")
				return False
			except (URLError, OSError) as e:
				# Timeouts and dropped connections. A chosen model gives way to the
				# default one; otherwise a retry is only safe while no audio of this
				# utterance has been played yet.
				if not fedAudio and modelId != DEFAULT_MODEL:
					driver._reportModelFailure(modelId, str(e))
					modelId = DEFAULT_MODEL
					offline = True
					continue
				if not fedAudio and retries < MAX_RETRIES and not offline:
					retries += 1
					if self._waitBeforeRetry(gen):
						continue
					return False
				# Both tries failed, so ElevenLabs can't be reached.
				if not fedAudio:
					driver._fallBack(f"ElevenLabs could not be reached: {e}")
					return False
				driver.reportError("network", f"ElevenLabs network error: {e}")
				return False
			finally:
				download.abandon()
			log.debug(
				f"ElevenLabs: {_modelInfo(modelId).name} returned "
				f"{received / (2.0 * OUTPUT_FORMATS[driver._formatIndex][1]):.2f} s of audio for {len(text)} chars."
			)
			driver._onSynthesisSucceeded()
			return True

	def _waitBeforeRetry(self, gen):
		time.sleep(RETRY_DELAY)
		return self._isCurrent(gen)

	def _waitForPrevious(self, gen, pair):
		"""Holds a new request back while an abandoned one is still waiting for its
		answer; see PREVIOUS_REQUEST_GRACE. False if this utterance is cancelled."""
		same = self._pending.get(pair)
		while same is not None and same.is_alive():
			if not self._isCurrent(gen):
				return False
			same.join(POLL_INTERVAL)
		previous = self._download
		while previous is not None and previous.is_alive():
			remaining = previous.started + PREVIOUS_REQUEST_GRACE - time.time()
			if remaining <= 0:
				break
			if not self._isCurrent(gen):
				return False
			previous.join(min(remaining, POLL_INTERVAL))
		return True

	@staticmethod
	def _readErrorDetail(error):
		try:
			return error.read().decode("utf-8", "replace")
		except Exception:
			return ""


class SynthDriver(BaseSynthDriver):
	name = "elevenlabs"
	description = "ElevenLabs"

	# Every setting; supportedSettings leaves out those the chosen model doesn't take.
	_allSettings = (
		BaseSynthDriver.VoiceSetting(),
		_languageSetting(),
		BaseSynthDriver.RateSetting(),
		BaseSynthDriver.RateBoostSetting(),
		BaseSynthDriver.VolumeSetting(),
		DriverSetting(
			"model",
			# Translators: label of the model box in voice settings.
			pgettext("label", "Model"),
			availableInSettingsRing=True,
			defaultVal=DEFAULT_MODEL,
			# Translators: name of the model setting in the settings ring.
			displayName=_("Model"),
		),
		NumericDriverSetting(
			"stability",
			# Translators: label of a slider in voice settings.
			pgettext("label", "Stabilit&y"),
			availableInSettingsRing=True,
			defaultVal=DEFAULT_STABILITY,
			# Translators: name of a setting in the settings ring.
			displayName=_("Stability"),
		),
		# Shown instead of the slider for models that take only STABILITY_PRESETS.
		DriverSetting(
			"stabilityPreset",
			# Translators: label of the stability slider, or of the box that replaces it, in voice settings.
			pgettext("label", "Stabilit&y"),
			availableInSettingsRing=True,
			defaultVal=DEFAULT_STABILITY_PRESET,
			# Translators: name of a setting in the settings ring.
			displayName=_("Stability"),
		),
		NumericDriverSetting(
			"clarity",
			# Translators: label of a slider in voice settings.
			pgettext("label", "Clarity"),
			availableInSettingsRing=True,
			defaultVal=DEFAULT_CLARITY,
			# Translators: name of a setting in the settings ring.
			displayName=_("Clarity"),
		),
		NumericDriverSetting(
			"style",
			# Translators: label of a slider in voice settings.
			pgettext("label", "Style"),
			availableInSettingsRing=True,
			defaultVal=DEFAULT_STYLE,
			# Translators: name of a setting in the settings ring.
			displayName=_("Style"),
		),
		BooleanDriverSetting(
			"speakerBoost",
			# Translators: label of a checkbox in voice settings.
			pgettext("label", "Spea&ker boost"),
			availableInSettingsRing=True,
			defaultVal=DEFAULT_SPEAKER_BOOST,
			# Translators: name of a setting in the settings ring.
			displayName=_("Speaker boost"),
		),
		DriverSetting(
			"normalization",
			# Translators: label of a box in voice settings.
			pgettext("label", "Text normalization"),
			availableInSettingsRing=True,
			defaultVal=DEFAULT_NORMALIZATION,
			# Translators: name of a setting in the settings ring.
			displayName=_("Text normalization"),
		),
		BooleanDriverSetting(
			"splitText",
			# Translators: label of a checkbox in voice settings.
			pgettext("label", "Split long text"),
			availableInSettingsRing=True,
			defaultVal=DEFAULT_SPLIT_TEXT,
			# Translators: name of a setting in the settings ring.
			displayName=_("Split long text"),
		),
		BooleanDriverSetting(
			"audioTags",
			# Translators: label of a checkbox in voice settings.
			pgettext("label", "Enable audio tags"),
			availableInSettingsRing=True,
			defaultVal=DEFAULT_AUDIO_TAGS,
			# Translators: name of a setting in the settings ring.
			displayName=_("Audio tags"),
		),
	)
	supportedCommands = {IndexCommand, LangChangeCommand}
	supportedNotifications = {synthIndexReached, synthDoneSpeaking}
	# Above zero while NVDA sets up, loads or saves settings; see _allSettingsVisible.
	_configuring = 0

	@classmethod
	def check(cls):
		# Without a key it can't speak, so it isn't offered until one is set in
		# NVDA's settings. The key is cached, so this is cheap to ask often.
		return cls._getApiKey() is not None

	def __init__(self):
		super().__init__()
		self._voiceId = None
		self._language = DEFAULT_LANGUAGE
		self._modelId = DEFAULT_MODEL
		self._normalization = DEFAULT_NORMALIZATION
		self._rate = 50
		self._rateBoost = False
		self._volume = 100
		self._stability = DEFAULT_STABILITY
		# Kept apart from the slider's value, so each survives the other's model.
		self._stabilityPreset = DEFAULT_STABILITY_PRESET
		self._clarity = DEFAULT_CLARITY
		self._style = DEFAULT_STYLE
		self._speakerBoost = DEFAULT_SPEAKER_BOOST
		self._splitText = DEFAULT_SPLIT_TEXT
		self._audioTags = DEFAULT_AUDIO_TAGS
		self._allVoices = OrderedDict()
		self._voiceCategories = {}
		self._voicesCache = OrderedDict()
		self._unusableVoices = set()
		self._voicesChecked = False
		self._unusableModels = set()
		# Models refused while in use; only for this session.
		self._rejectedModels = set()
		self._modelsChecked = False
		self._presetStabilityModels = set(PRESET_STABILITY_MODELS)
		self._modelFailures = {}
		# Set once speech has been handed to FALLBACK_SYNTH.
		self._fellBack = False
		# Models already warned as too slow for the speed.
		self._speedWarned = set()
		self._tier = ""
		self._formatIndex = 0
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
		self._loadOutputFormat()
		self._migrateLegacyVariantSetting()
		self._loadSavedModel()
		# Everything below may raise on purpose: NVDA then tells the user and falls
		# back to a working synthesizer. terminate() is not called in that case.
		self._loadAccountState()
		_installTagFilter()
		self._worker = _SpeechWorker(self)
		try:
			self._checkVoicesAtStartup()
			self._checkModelsAtStartup()
		except Exception:
			# Started above, so it has to be stopped before the exception leaves.
			self._worker.stop()
			raise
		hidden = len(self._unusableVoices)
		log.info(
			f"ElevenLabs ready: {len(self._voicesCache)} voice(s)"
			f"{f', {hidden} unusable' if hidden else ''}, "
			f"{len(_offeredModels) - len(self._unusableModels)} model(s), "
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
				# NVDA sends e.g. "en_US"; only the language part is used. None,
				# or a language the API lacks, means the language setting.
				code = (item.lang or "").split("_")[0].lower()
				langOverride = code if code in LANGUAGES and code != "auto" else None
		text = " ".join(textParts).replace(_TAG_OPEN, "[").replace(_TAG_CLOSE, "]").strip()
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
		if self._worker is not None:
			# A pause isn't a gap in the audio.
			self._worker._playEnd = None
		player = self._player
		if player is not None:
			try:
				player.pause(switch)
			except Exception:
				pass

	# --- Settings shown in NVDA ---

	def _get_supportedSettings(self):
		if self._configuring:
			return self._allSettings
		info = _modelInfo(self._modelId)
		presets = self._modelId in self._presetStabilityModels
		# Shown only if the model takes them, besides the voice_settings in _SETTING_API_NAMES.
		shown = {
			LANGUAGE_SETTING_ID: info.languageCode,
			"audioTags": info.tags,
			"stability": "stability" in info.settings and not presets,
			"stabilityPreset": "stability" in info.settings and presets,
		}
		return tuple(
			s for s in self._allSettings
			if shown.get(s.id, True)
			and (s.id not in _SETTING_API_NAMES or _SETTING_API_NAMES[s.id] in info.settings)
		)

	def _tagsActive(self):
		"""Whether audio tags are on for the chosen model; its checkbox is hidden for others."""
		return self._audioTags and _modelInfo(self._modelId).tags

	@contextmanager
	def _allSettingsVisible(self):
		"""Lets NVDA's own loading and saving see every setting, so the values of
		those the chosen model hides are kept."""
		self._configuring += 1
		try:
			yield
		finally:
			self._configuring -= 1

	def initSettings(self):
		with self._allSettingsVisible():
			super().initSettings()
		# NVDA may have built the settings ring from the full list meanwhile.
		self._refreshGui(changedSetting="model")

	def loadSettings(self, onlyChanged=False):
		with self._allSettingsVisible():
			super().loadSettings(onlyChanged)
		if not self._configuring:
			self._refreshGui(changedSetting="model")

	def saveSettings(self):
		with self._allSettingsVisible():
			super().saveSettings()

	def _refreshGui(self, changedSetting=None):
		"""Brings NVDA's settings ring up to date after a list or the model changed.

		With changedSetting, an open voice settings panel also shows or hides the
		settings that depend on it, as NVDA does itself when the voice changes."""
		def refresh():
			if getSynth() is not self:
				return
			try:
				ring = getattr(globalVars, "settingsRing", None)
				if ring is not None:
					ring.updateSupportedSettings(self)
				if changedSetting:
					for panel in _openVoicePanels():
						panel.updateDriverSettings(changedSetting=changedSetting)
						_orderVoicePanel(panel, [s.id for s in self._allSettings])
			except Exception:
				log.debugWarning("ElevenLabs: could not refresh NVDA's settings", exc_info=True)

		# Touches NVDA's interface, so it has to happen on the main thread.
		queueHandler.queueFunction(queueHandler.eventQueue, refresh)

	# --- API key ---

	@staticmethod
	def _getApiKey():
		# Read afresh each time, as the settings panel can set or clear it at any moment.
		return _settings.getApiKey()

	# --- Error reporting ---

	def _fallBack(self, reason):
		"""Hands speech to eSpeak for this session, so NVDA is never left silent.

		NVDA's saved synthesizer is kept, so ElevenLabs is used again after a restart."""
		if self._fellBack:
			return
		self._fellBack = True
		log.error(f"ElevenLabs: {reason} Switching to eSpeak until ElevenLabs is selected again.")

		def switch():
			if getSynth() is not self:
				return
			import synthDriverHandler
			import ui
			if synthDriverHandler.setSynth(FALLBACK_SYNTH, isFallback=True):
				# Translators: spoken when NVDA switches to eSpeak.
				ui.message(_("ElevenLabs is unavailable."))

		# Changing synthesizer has to happen on the main thread.
		queueHandler.queueFunction(queueHandler.eventQueue, switch)

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

	def _apiSpeed(self, modelId):
		"""The part of the speed the API delivers; 1.0 for models without speed."""
		if "speed" not in _modelInfo(modelId).settings:
			return 1.0
		lowest, highest = API_SPEED_RANGE
		return round(min(max(self.speechSpeed, lowest), highest), 2)

	def _sonicSpeed(self, modelId):
		"""The rest of the speed, left for sonic to stretch locally."""
		return round(self.speechSpeed / self._apiSpeed(modelId), 3)

	def _warnIfTooFast(self, modelId):
		"""Logs once when the speed goes beyond what the model can generate."""
		maxSpeed = _modelInfo(modelId).maxSpeed
		speed = self._sonicSpeed(modelId)
		if maxSpeed is None or speed <= maxSpeed:
			self._speedWarned.discard(modelId)
			return
		if modelId in self._speedWarned:
			return
		self._speedWarned.add(modelId)
		log.warning(
			f"ElevenLabs: {_modelInfo(modelId).name} generates speech at about {maxSpeed}x "
			f"real time, so at speed {speed:g}x longer text may pause while it catches up."
		)

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

	def _prepareSonic(self, modelId):
		"""Returns a sonic stream set up for this utterance, or None if unneeded."""
		speed = self._sonicSpeed(modelId)
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
		with _settings.editing() as settings:
			settings["outputFormat"] = self.outputFormat
		self._recordedFormat = self.outputFormat

	def _loadOutputFormat(self):
		"""Restores the audio format worked out in an earlier session."""
		savedFormat = _settings.get("outputFormat")
		for index, (formatName, sampleRate) in enumerate(OUTPUT_FORMATS):
			if formatName == savedFormat:
				self._formatIndex = index
				self._recordedFormat = savedFormat
				break
		else:
			if savedFormat:
				log.warning(
					f"ElevenLabs: ignoring unknown outputFormat {savedFormat!r} in {_settings.SETTINGS_FILENAME}"
				)

	# --- Voices ---
	# Only voices this account can generate with are listed. Voices stay on an
	# account after a downgrade but stop working, so each is tried once and the
	# result is cached against a fingerprint of the account state.

	def _loadAccountState(self):
		"""Checks the account and builds the voice list; raises if unusable."""
		key = self._getApiKey()
		if not key:
			raise ElevenLabsUnavailable(
				f"No ElevenLabs API key is set. Set one in {KEY_LOCATION}, then select "
				"the synthesizer again."
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
		# The plan decides which voices and models work, so it is in the fingerprints.
		try:
			subscription = _fetchJson(f"{API_BASE}/v1/user/subscription", key, INIT_TIMEOUT)
			self._tier = str(subscription.get("tier") or "")
		except ElevenLabsUnavailable as e:
			# Not fatal: the tier only sharpens the cache, it isn't needed to speak.
			log.debugWarning(f"ElevenLabs: could not read the subscription: {e}")
			self._tier = ""
		self._loadModels(key)
		self._loadRefusedFields()
		self._loadModelCheck()
		if self._modelId not in _offeredModels or self._modelId in self._unusableModels:
			log.warning(
				f"ElevenLabs: the saved model {self._modelId!r} is not usable with this account; "
				f"using {MODELS[DEFAULT_MODEL].name}."
			)
			self._modelId = DEFAULT_MODEL
		self._loadUnusableVoices()
		self._rebuildVoiceList()

	def _accountFingerprint(self):
		"""Identifies the plan and voice list, so cached results are only reused
		while both are unchanged."""
		digest = hashlib.md5("\n".join(sorted(self._allVoices)).encode("utf-8")).hexdigest()[:12]
		return f"v{VOICE_CHECK_VERSION}:{self._tier or 'unknown'}:{len(self._allVoices)}:{digest}"

	def _loadUnusableVoices(self):
		"""Restores the chosen model's checked result, if it still applies."""
		check = _settings.get("voiceChecks", {}).get(self._modelId) or {}
		if check.get("fingerprint") != self._accountFingerprint():
			# The plan or the voice list changed, so the old result means nothing.
			self._unusableVoices = set()
			self._voicesChecked = False
			return
		self._unusableVoices = {v for v in check.get("unusable", ()) if isinstance(v, str)}
		# A result is only recorded once every voice has an answer, so a matching
		# fingerprint means the whole list is already known good.
		self._voicesChecked = True

	def _saveUnusableVoices(self):
		with _settings.editing() as settings:
			check = settings.setdefault("voiceChecks", {}).setdefault(self._modelId, {})
			check["unusable"] = sorted(self._unusableVoices)
			# The fingerprint says every voice is known, so only then is it written.
			if self._voicesChecked or self._voiceCheckDisabled():
				check["fingerprint"] = self._accountFingerprint()

	@staticmethod
	def _voiceCheckDisabled():
		return _settings.get("checkVoices", True) is False

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

		NVDA waits for this on purpose: the settings dialog keeps the list it first
		reads, so it has to be right from the start. The result is cached."""
		if not self._voicesCache:
			raise ElevenLabsUnavailable(
				"None of the voices on this ElevenLabs account can be used with the current "
				"plan. Select another synthesizer and press NVDA+Shift+V to check them "
				"again, or upgrade the plan."
			)
		# The chosen voice is checked first, since that request also proves the
		# key, the plan and the audio format work end to end.
		self._validateStartupVoice()
		if self._voicesChecked:
			return
		if self._voiceCheckDisabled():
			log.debug(f"ElevenLabs: the voice check is switched off in {_settings.SETTINGS_FILENAME}.")
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
		unusable, unanswered, succeeded, lastReason, accountFailure = self._probeAll(
			pending, started + VOICE_CHECK_DEADLINE, self._probeVoice, PROBE_WORKERS
		)
		if self._probeStop.is_set():
			return
		if accountFailure:
			# What was learned is unreliable, so nothing is hidden or cached and
			# the check simply runs again next time.
			log.warning(
				"ElevenLabs: the voice check could not finish because the account stopped "
				f"accepting requests, so the voice list is unchanged. Reason: {lastReason or 'see above'}"
			)
			return
		# Without a voice that has already spoken, every voice failing points at
		# the account, so hiding the whole list would be the wrong conclusion.
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
			# Nothing is cached while any voice is unaccounted for, so it gets
			# another chance next time rather than being wrongly trusted.
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

	def _probeAll(self, pending, deadline, probeOne, workers):
		"""Tries every voice or model, then once more, one at a time, those that gave
		no answer, so a burst of rate limiting doesn't cost the whole result."""
		unusable, unanswered, succeeded, lastReason, accountFailure = self._runProbes(
			pending, deadline, probeOne, workers
		)
		if unanswered and not accountFailure and time.time() < deadline:
			more, unanswered, ok, reason, accountFailure = self._runProbes(
				unanswered, deadline, probeOne, 1
			)
			unusable += more
			succeeded += ok
			lastReason = reason or lastReason
		return unusable, unanswered, succeeded, lastReason, accountFailure

	def _runProbes(self, pending, deadline, probeOne, workers):
		"""Tries a batch of voices or models, returning what was learned about each."""
		unusable, unanswered, succeeded, lastReason = [], [], 0, ""
		# Set as soon as the account itself refuses, so the rest give up.
		accountFailure = []

		def probe(item):
			if accountFailure or self._probeStop.is_set() or time.time() > deadline:
				return PROBE_UNKNOWN, "check abandoned"
			result, detail = probeOne(item)
			if result in (PROBE_ACCOUNT, PROBE_MODEL):
				accountFailure.append(detail)
			return result, detail

		try:
			with ThreadPoolExecutor(max_workers=workers) as pool:
				results = list(zip(pending, pool.map(probe, pending)))
		except Exception:
			log.error("ElevenLabs: the check failed", exc_info=True)
			return unusable, list(pending), succeeded, lastReason, True
		for item, (result, detail) in results:
			if result == PROBE_UNUSABLE:
				unusable.append(item)
				lastReason = detail
				log.debug(f"ElevenLabs: {item} refused: {detail}")
			elif result == PROBE_OK:
				succeeded += 1
			else:
				# Includes the one that hit the account error, so it is never
				# taken as answered.
				unanswered.append(item)
		return unusable, unanswered, succeeded, lastReason, bool(accountFailure)

	def recheck(self, onFinished=None):
		"""Checks voices and models again from scratch, e.g. after a plan change.

		@param onFinished: called on the checking thread with the numbers of usable
			and unusable voices and models; interface work must go to the main thread.
		"""
		if self._probeThread is not None and self._probeThread.is_alive():
			return False
		# Off the main thread, so NVDA doesn't freeze while the user works.
		self._probeThread = threading.Thread(
			target=self._recheckNow,
			args=(onFinished,),
			name="ElevenLabsCheck",
			daemon=True,
		)
		self._probeThread.start()
		return True

	def _recheckNow(self, onFinished=None):
		self._unusableVoices = set()
		self._voicesChecked = False
		self._rebuildVoiceList()
		try:
			self._checkVoices(list(self._allVoices))
			self._checkModels()
			self._refreshGui()
		finally:
			if onFinished is not None and not self._probeStop.is_set():
				try:
					onFinished(
						len(self._voicesCache),
						len(self._unusableVoices),
						len(_offeredModels) - len(self._unusableModels),
						len(self._unusableModels),
					)
				except Exception:
					log.error("ElevenLabs: could not report the check result", exc_info=True)

	def _startupCandidates(self):
		"""Voices to try at start up, likeliest to work first.

		The saved choice comes first, then ElevenLabs' own voices, which every plan
		has. Cloned voices are the ones that stop working after a downgrade."""
		with self._voicesLock:
			usable = list(self._voicesCache)
		ordered = [v for v in (self._voiceId,) if v in usable]
		ordered += [v for v in usable if v not in ordered and self._voiceCategories.get(v) == "premade"]
		ordered += [v for v in usable if v not in ordered]
		return ordered[:MAX_STARTUP_TRIES]

	def _validateStartupVoice(self):
		"""Tries voices until one speaks, or gives up so NVDA can fall back."""
		removed = []
		candidates = self._startupCandidates()
		while candidates:
			voiceId = candidates.pop(0)
			if voiceId in self._unusableVoices:
				continue
			result, detail = self._probeVoice(voiceId)
			if result == PROBE_MODEL:
				# The chosen model is refused, so the default one is tried instead.
				if self._rejectModel(self._modelId, detail):
					removed = []
					candidates = self._startupCandidates()
					continue
				raise ElevenLabsUnavailable(f"The model {self._modelId} cannot be used: {detail}")
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

	def _probe(self, voiceId, modelId=DEFAULT_MODEL, settleFormat=True, allFields=False):
		"""Asks for the shortest possible sample to see whether a voice or model works.

		allFields sends every field the model may take; refused ones are learned."""
		key = self._getApiKey()
		if not key:
			return PROBE_ACCOUNT, "no API key"
		while True:
			# Neutral values, since this only checks the request is allowed.
			body = self._requestBody(PROBE_TEXT, self._language, modelId, plain=not allFields, probe=allFields)
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
				# The first request of the session also settles the audio format.
				if settleFormat and e.code == 403 and _isOutputFormatRejection(detail) \
						and self._useNextOutputFormat():
					continue
				if _isRefusal(e.code) and self._refuseFields(modelId, detail, body):
					continue
				return _classifyRefusal(e.code, detail), detail.strip()
			except (URLError, OSError) as e:
				return PROBE_UNKNOWN, str(e)

	def _probeVoice(self, voiceId):
		"""Checks a voice with the chosen model."""
		return self._probe(voiceId, self._modelId)

	@staticmethod
	def _readProbeDetail(error):
		try:
			return error.read().decode("utf-8", "replace")[:300]
		except Exception:
			return ""

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
		self._refreshGui()

	def _get_availableVoices(self):
		# Overrides the property rather than NVDA's _getAvailableVoices hook, so
		# the list is never cached and always current.
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

	# --- Models ---
	# The account's own model list is offered, in release order. Which models and
	# fields an account may use has differed by plan, so each model is tried once,
	# like the voices, and the result is cached against the plan.

	def _loadSavedModel(self):
		"""Reads the saved model early, so the start up voice check uses it."""
		# From the profiles, not config.conf: NVDA hasn't registered the setting
		# yet, and asking now would cache it as missing. Newest profile first.
		try:
			profiles = list(config.conf.profiles)
		except Exception:
			return
		for profile in reversed(profiles):
			try:
				saved = profile.get("speech", {}).get(self.name, {}).get("model")
			except Exception:
				continue
			if saved:
				self._modelId = str(saved)
				return

	def _loadModels(self, key):
		"""Fetches the account's text to speech models; MODELS if that fails."""
		try:
			data = _fetchJson(f"{API_BASE}/v1/models", key, INIT_TIMEOUT)
		except ElevenLabsUnavailable as e:
			log.debugWarning(f"ElevenLabs: could not read the model list; using the built-in one. {e}")
			data = None
		listed = OrderedDict()
		for model in data if isinstance(data, list) else ():
			if isinstance(model, dict) and model.get("can_do_text_to_speech") and model.get("model_id"):
				listed[model["model_id"]] = model
		_offeredModels.clear()
		if not listed:
			_offeredModels.update(MODELS)
			return
		# Release order: older unknown ones, MODELS, then new ones, likely newer.
		known = list(_EARLIER_MODELS) + list(MODELS)
		order = [m for m in known if m in listed or m == DEFAULT_MODEL]
		order += [m for m in listed if m not in known]
		for modelId in order:
			model = listed.get(modelId, {})
			info = MODELS.get(modelId)
			if info is None:
				languages = [lang.get("language_id") for lang in model.get("languages") or () if isinstance(lang, dict)]
				info = ModelInfo(
					model.get("name") or modelId, True, _ALL_SETTINGS, realtime=False,
					englishOnly=languages == ["en"],
				)
			_offeredModels[modelId] = info._replace(settings=_withoutDeclined(info.settings, model))

	def _loadRefusedFields(self):
		"""Restores the fields each model has refused."""
		_refused.clear()
		for modelId, saved in (_settings.get("refusedFields") or {}).items():
			fields = {f for f in saved if f in _LEARNABLE_FIELDS} if isinstance(saved, list) else set()
			if fields:
				_refused[modelId] = fields

	def _refuseFields(self, modelId, detail, body):
		"""Records fields a refusal of body names for a model; True if any are new.

		They are left out of later requests, and their settings hidden."""
		info = _modelInfo(modelId)
		sent = set(info.settings) | ({"language_code"} if info.languageCode else set())
		new = {f for f in sent if re.search(rf"\b{f}\b", detail.lower())}
		if "apply_text_normalization" in new and body.get("apply_text_normalization") == "on":
			# Only the value was refused, so just "on" is left out of the box.
			new.discard("apply_text_normalization")
			new.add(_NORMALIZATION_ON_REFUSED)
		new -= _refused.get(modelId, set())
		if not new:
			return False
		fields = _refused.setdefault(modelId, set())
		fields.update(new)
		with _settings.editing() as settings:
			settings.setdefault("refusedFields", {})[modelId] = sorted(fields)
		log.warning(
			f"ElevenLabs: {info.name} does not take {', '.join(sorted(new))}, so it is left out "
			f"from now on. {detail.strip()[:300]}"
		)
		if modelId == self._modelId:
			self._refreshGui(changedSetting="model")
		return True

	def _modelFingerprint(self):
		"""Identifies the plan and model list the model check was done for."""
		digest = hashlib.md5("\n".join(_offeredModels).encode("utf-8")).hexdigest()[:12]
		return f"v{MODEL_CHECK_VERSION}:{self._tier or 'unknown'}:{digest}"

	def _loadModelCheck(self):
		"""Restores the cached model check; False if there is none for this account state."""
		check = _settings.get("modelCheck") or {}
		if check.get("fingerprint") != self._modelFingerprint():
			self._modelsChecked = False
			return False
		saved = check.get("unusable", ())
		self._unusableModels = {m for m in saved if m in _offeredModels and m != DEFAULT_MODEL}
		self._modelsChecked = True
		return True

	def _checkModelsAtStartup(self):
		"""Settles the model list before NVDA reads it, unless a cached result applies."""
		if self._modelsChecked:
			return
		try:
			self._checkModels()
		except Exception:
			# Never fatal, since the default model is known to work.
			log.error("ElevenLabs: the model check failed", exc_info=True)

	def _checkModels(self):
		"""Tries each model once and leaves out those this account can't use.

		Every field a model may take is sent, so those it refuses are learned."""
		# The default model has already spoken, so it needs no check.
		pending = [m for m in _offeredModels if m != DEFAULT_MODEL]
		# Learned again from scratch; the default model's on first refusal.
		with _settings.editing() as settings:
			settings.pop("refusedFields", None)
		_refused.clear()
		voiceId = self._modelProbeVoice()
		unusable, unanswered, succeeded, lastReason, accountFailure = self._probeAll(
			pending,
			time.time() + MODEL_CHECK_DEADLINE,
			lambda modelId: self._probeModel(voiceId, modelId),
			MODEL_PROBE_WORKERS,
		)
		if self._probeStop.is_set():
			return
		if accountFailure:
			log.warning(
				"ElevenLabs: the model check could not finish because the account stopped "
				f"accepting requests, so the model list is unchanged. Reason: {lastReason or 'see above'}"
			)
			return
		if unusable and not succeeded and len(pending) > 1:
			# Every model refused points at the account or voice, not the models.
			log.error(
				"ElevenLabs: every model was refused, so the model list is left alone. "
				f"The last reason given was: {lastReason}"
			)
			return
		self._unusableModels = set(unusable)
		if self._modelId in self._unusableModels:
			self._changeModel(DEFAULT_MODEL)
		if unusable:
			names = ", ".join(_modelInfo(m).name for m in pending if m in self._unusableModels)
			log.warning(
				f"ElevenLabs: {len(unusable)} model(s) cannot be used with this account and "
				f"have been left out of the model list: {names}"
			)
		if unanswered:
			log.warning(
				f"ElevenLabs: {len(unanswered)} model(s) gave no answer, so the check will "
				"run again next time the synthesizer is loaded."
			)
			return
		with _settings.editing() as settings:
			settings["modelCheck"] = {
				"fingerprint": self._modelFingerprint(),
				"unusable": sorted(self._unusableModels),
			}
		self._modelsChecked = True

	def _probeModel(self, voiceId, modelId):
		"""Checks a model, learning which fields it refuses."""
		result, detail = self._probe(voiceId, modelId, settleFormat=False, allFields=True)
		# Here a refused model is simply unusable.
		return (PROBE_UNUSABLE if result == PROBE_MODEL else result), detail

	def _modelProbeVoice(self):
		"""A usable voice to check models with, preferring ElevenLabs' own."""
		with self._voicesLock:
			usable = list(self._voicesCache)
		premade = [v for v in usable if self._voiceCategories.get(v) == "premade"]
		if self._voiceId in premade:
			return self._voiceId
		return premade[0] if premade else self.synthesisVoice

	def _requestBody(self, text, lang, modelId, plain=False, probe=False):
		"""The JSON body of a speech request, shaped for the given model.

		plain and probe are for checks: probe sends every field the model may take."""
		info = _modelInfo(modelId)
		body = {"text": text, "model_id": modelId}
		if lang and lang != "auto" and info.languageCode:
			body["language_code"] = lang
		if "apply_text_normalization" in info.settings:
			if probe:
				body["apply_text_normalization"] = "auto"
			elif not plain and self._normalization != "auto" \
					and self._normalization in self._normalizationsFor(modelId):
				body["apply_text_normalization"] = self._normalization
		body["voice_settings"] = self._voiceSettings(modelId, plain, probe)
		return body

	def _voiceSettings(self, modelId, plain=False, probe=False):
		"""The voice_settings block, limited to what the model accepts.

		plain gives ElevenLabs' defaults for checks, without style's extra latency;
		probe adds neutral values for the rest."""
		if plain or probe:
			values = {"stability": 0.5, "similarity_boost": 0.75}
			if probe:
				values.update(style=0.0, use_speaker_boost=True, speed=1.0)
		else:
			values = {
				# Lower stability gives a wider emotional range, higher a flatter voice.
				"stability": round(self._stability / 100.0, 2),
				"similarity_boost": round(self._clarity / 100.0, 2),
				"style": round(self._style / 100.0, 2),
				"use_speaker_boost": self._speakerBoost,
				"speed": self._apiSpeed(modelId),
			}
		if modelId in self._presetStabilityModels:
			if plain or probe:
				stability = values["stability"]
				values["stability"] = min(STABILITY_PRESETS, key=lambda preset: abs(preset - stability))
			else:
				values["stability"] = STABILITY_PRESET_VALUES[self._stabilityPreset]
		accepted = _modelInfo(modelId).settings
		return {name: value for name, value in values.items() if name in accepted}

	def _usePresetStability(self, modelId):
		"""Switches a model that refused the stability value to the presets.

		False if it already uses them, so the refusal is handled like any other."""
		if modelId in self._presetStabilityModels:
			return False
		self._presetStabilityModels.add(modelId)
		log.debug(f"ElevenLabs: {modelId} refused the stability value; using presets.")
		if modelId == self._modelId:
			# The box replaces the slider, starting where the slider was, so the voice keeps its sound.
			self._stabilityPreset = self._nearestStabilityPreset()
			self._refreshGui(changedSetting="model")
		return True

	def _nearestStabilityPreset(self):
		stability = self._stability / 100.0
		return min(STABILITY_PRESET_VALUES, key=lambda name: abs(STABILITY_PRESET_VALUES[name] - stability))

	def _reportModelFailure(self, modelId, reason):
		"""Logs that a model failed and the default one spoke, once per reason."""
		message = (
			f"ElevenLabs: {_modelInfo(modelId).name} could not speak, so "
			f"{MODELS[DEFAULT_MODEL].name} was used instead. {reason}"
		)
		if self._modelFailures.get(modelId) != message:
			self._modelFailures[modelId] = message
			log.warning(message)

	def _rejectModel(self, modelId, detail):
		"""Leaves out a refused model for this session and uses the default; False if none to fall to."""
		if modelId == DEFAULT_MODEL or modelId in self._rejectedModels:
			return False
		self._rejectedModels.add(modelId)
		log.warning(
			f"ElevenLabs: {_modelInfo(modelId).name} cannot be used with this account, so "
			f"{MODELS[DEFAULT_MODEL].name} is used instead. {detail}"
		)
		if self._modelId == modelId:
			self._changeModel(DEFAULT_MODEL)
		return True

	def _get_availableModels(self):
		return OrderedDict(
			(modelId, StringParameterInfo(modelId, _modelDisplayName(info)))
			for modelId, info in _offeredModels.items()
			if modelId not in self._unusableModels and modelId not in self._rejectedModels
		)

	def _get_model(self):
		return self._modelId

	def _set_model(self, value):
		unusable = self._unusableModels | self._rejectedModels
		if value in _offeredModels and value not in unusable:
			self._changeModel(value)
			return
		reason = "cannot be used with this account" if value in unusable else "is unknown"
		log.warning(f"ElevenLabs: the model {value!r} {reason}; using {MODELS[DEFAULT_MODEL].name}.")
		self._changeModel(DEFAULT_MODEL)

	def _changeModel(self, modelId):
		"""Switches model, with its own voice check, and has NVDA show only the settings it takes."""
		if modelId == self._modelId:
			return
		self._modelId = modelId
		# Without a voice check for this model the whole list is offered; voices
		# that fail are removed on first use, and the next start checks them all.
		self._loadUnusableVoices()
		if not self._rebuildVoiceList():
			self._unusableVoices = set()
			self._voicesChecked = False
			self._rebuildVoiceList()
		with self._voicesLock:
			if self._voiceId not in self._voicesCache:
				self._voiceId = None
		log.debug(f"ElevenLabs: now using {_modelInfo(modelId).name}.")
		# While NVDA loads settings it is refreshed once afterwards instead.
		if not self._configuring:
			self._refreshGui(changedSetting="model")

	# --- Text normalization ---
	# The box is hidden for a model that refuses apply_text_normalization, and
	# "on" left out for one that refused only that.

	@staticmethod
	def _normalizationsFor(modelId):
		refused = _NORMALIZATION_ON_REFUSED in _refused.get(modelId, ())
		return [mode for mode in NORMALIZATIONS if not (refused and mode == "on")]

	def _get_availableNormalizations(self):
		return OrderedDict(
			(mode, StringParameterInfo(mode, NORMALIZATIONS[mode]))
			for mode in self._normalizationsFor(self._modelId)
		)

	def _get_normalization(self):
		# What the model gets, for the box; NVDA saves the choice itself, kept for other models.
		if not self._configuring and self._normalization not in self._normalizationsFor(self._modelId):
			return DEFAULT_NORMALIZATION
		return self._normalization

	def _set_normalization(self, value):
		if value in NORMALIZATIONS:
			self._normalization = value
		else:
			log.warning(f"ElevenLabs: unknown text normalization {value!r}; using automatic.")
			self._normalization = DEFAULT_NORMALIZATION

	# --- Language ---
	# The language box; see LANGUAGE_SETTING_ID for why its id is elvlanguage.

	def _get_availableElvlanguages(self):
		# Named in NVDA's language and sorted by those names, automatic first.
		names = sorted(
			((langId, _languageName(langId, name)) for langId, name in LANGUAGES.items() if langId != "auto"),
			key=lambda item: item[1].lower(),
		)
		names.insert(0, ("auto", _languageName("auto", LANGUAGES["auto"])))
		return OrderedDict((langId, StringParameterInfo(langId, name)) for langId, name in names)

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
		# None means "no opinion", so NVDA keeps using its own language. A model
		# without language_code hides the box, so its choice is no opinion either.
		if self._language == "auto" or not _modelInfo(self._modelId).languageCode:
			return None
		return self._language

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

	# --- Rate, volume and voice settings ---

	def _get_rate(self):
		return self._rate

	def _set_rate(self, value):
		self._rate = max(0, min(100, int(value)))

	def _get_stability(self):
		return self._stability

	def _set_stability(self, value):
		self._stability = max(0, min(100, int(value)))

	def _get_availableStabilitypresets(self):
		return OrderedDict(
			(preset, StringParameterInfo(preset, name))
			for preset, name in STABILITY_PRESET_NAMES.items()
		)

	def _get_stabilityPreset(self):
		return self._stabilityPreset

	def _set_stabilityPreset(self, value):
		if value in STABILITY_PRESET_NAMES:
			self._stabilityPreset = value
		else:
			log.warning(f"ElevenLabs: unknown stability {value!r}; using natural.")
			self._stabilityPreset = DEFAULT_STABILITY_PRESET

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

	def _get_splitText(self):
		return self._splitText

	def _set_splitText(self, value):
		self._splitText = bool(value)

	def _get_audioTags(self):
		return self._audioTags

	def _set_audioTags(self, value):
		self._audioTags = bool(value)

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

# -*- coding: utf-8 -*-
# The ElevenLabs add-on's own settings, kept in elevenlabs_settings.json in
# NVDA's configuration folder. The API key is encrypted with Windows' data
# protection (DPAPI), so only the same Windows user on the same computer can
# read it back; everything else is what the driver has learned about the
# account. NVDA's own config holds the voice settings as usual.
#
# The leading underscore keeps NVDA from listing this module as a synthesizer.

import base64
import copy
import ctypes
import json
import os
import threading
from contextlib import contextmanager
from ctypes import wintypes

import globalVars
from logHandler import log

SETTINGS_FILENAME = "elevenlabs_settings.json"

_lock = threading.RLock()
# The parsed file, loaded on first use. Only this module writes the file, so
# it stays in step with the disk.
_data = None
# The decrypted key, so it isn't decrypted for every utterance; _UNREAD until
# first asked for.
_UNREAD = object()
_apiKey = _UNREAD


def settingsPath():
	return os.path.join(globalVars.appArgs.configPath, SETTINGS_FILENAME)


def _load():
	global _data
	if _data is not None:
		return _data
	path = settingsPath()
	try:
		with open(path, "r", encoding="utf-8") as f:
			data = json.load(f)
		if not isinstance(data, dict):
			raise ValueError("not a JSON object")
	except FileNotFoundError:
		data = {}
	except (OSError, ValueError):
		log.error(f"ElevenLabs: {path} could not be read and is ignored.", exc_info=True)
		# Kept aside rather than overwritten by the next save.
		try:
			os.replace(path, path + ".bad")
		except OSError:
			pass
		data = {}
	_data = data
	return _data


def _save(data):
	"""Writes data to the file in one step, so a crash can't leave half a file."""
	path = settingsPath()
	tempPath = path + ".tmp"
	try:
		with open(tempPath, "w", encoding="utf-8") as f:
			json.dump(data, f, indent="\t", sort_keys=True)
			f.write("\n")
		os.replace(tempPath, path)
		return True
	except OSError:
		log.error(f"ElevenLabs: could not save {path}", exc_info=True)
		return False


def get(name, default=None):
	"""A copy of a top level setting, so changing it changes nothing saved."""
	with _lock:
		return copy.deepcopy(_load().get(name, default))


@contextmanager
def editing():
	"""Yields the settings to change in place; they are saved afterwards.

	Checks run on several threads, so edits take turns."""
	global _data
	with _lock:
		data = copy.deepcopy(_load())
		yield data
		if data != _data and _save(data):
			_data = data


def invalidateChecks():
	"""Forgets the recorded voice and model checks, so the next load redoes them.

	For when the synthesizer is not running and there is nothing to check against."""
	global _data
	with _lock:
		data = copy.deepcopy(_load())
		data.pop("voiceChecks", None)
		data.pop("modelCheck", None)
		if data == _data:
			return True
		if not _save(data):
			return False
		_data = data
		return True


# --- API key ---


class _DataBlob(ctypes.Structure):
	_fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char)))


# Own handles rather than ctypes.windll's shared ones, so the argtypes set here
# can't clash with anyone else's.
_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_blobArgs = (
	ctypes.POINTER(_DataBlob),
	wintypes.LPCWSTR,
	ctypes.POINTER(_DataBlob),
	ctypes.c_void_p,
	ctypes.c_void_p,
	wintypes.DWORD,
	ctypes.POINTER(_DataBlob),
)
_crypt32.CryptProtectData.argtypes = _blobArgs
_crypt32.CryptProtectData.restype = wintypes.BOOL
_crypt32.CryptUnprotectData.argtypes = (
	ctypes.POINTER(_DataBlob),
	ctypes.POINTER(wintypes.LPWSTR),
) + _blobArgs[2:]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
_kernel32.LocalFree.restype = ctypes.c_void_p

# Never show Windows' own prompts; NVDA may be running where nobody can answer them.
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
# Ties the encrypted key to this add-on, so other DPAPI data can't be mistaken for it.
_ENTROPY = b"ElevenLabsTTS NVDA add-on API key"


def _blob(data):
	buffer = ctypes.create_string_buffer(data, len(data))
	return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _crypt(function, data, *extra):
	"""Runs CryptProtectData or CryptUnprotectData on data and returns the result."""
	dataIn, _keepIn = _blob(data)
	entropy, _keepEntropy = _blob(_ENTROPY)
	dataOut = _DataBlob()
	if not function(
		ctypes.byref(dataIn), *extra, ctypes.byref(entropy), None, None,
		_CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(dataOut),
	):
		raise ctypes.WinError(ctypes.get_last_error())
	try:
		return ctypes.string_at(dataOut.pbData, dataOut.cbData)
	finally:
		_kernel32.LocalFree(ctypes.cast(dataOut.pbData, ctypes.c_void_p))


def getApiKey():
	"""The saved API key, or None if there is none or it can't be decrypted."""
	global _apiKey
	with _lock:
		if _apiKey is not _UNREAD:
			return _apiKey
		_apiKey = None
		saved = _load().get("apiKey")
		if saved:
			try:
				_apiKey = _crypt(
					_crypt32.CryptUnprotectData, base64.b64decode(saved), None
				).decode("utf-8")
			except (OSError, ValueError):
				# E.g. a configuration copied from another computer or user.
				log.warning(
					"ElevenLabs: the saved API key can't be decrypted by this Windows user "
					"on this computer. Set it again in NVDA's settings.",
					exc_info=True,
				)
		return _apiKey


def setApiKey(key):
	"""Encrypts and saves key; False if it couldn't be saved."""
	global _apiKey
	try:
		encrypted = _crypt(_crypt32.CryptProtectData, key.encode("utf-8"), None)
	except OSError:
		log.error("ElevenLabs: could not encrypt the API key", exc_info=True)
		return False
	with _lock:
		with editing() as data:
			data["apiKey"] = base64.b64encode(encrypted).decode("ascii")
		saved = _data.get("apiKey") == data["apiKey"]
		if saved:
			_apiKey = key
		return saved


def clearApiKey():
	"""Removes the saved key; False if the file couldn't be saved."""
	global _apiKey
	with _lock:
		with editing() as data:
			data.pop("apiKey", None)
		cleared = "apiKey" not in _data
		if cleared:
			_apiKey = None
		return cleared

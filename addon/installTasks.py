# -*- coding: utf-8 -*-
# Runs when the add-on is installed, so the file to paste the API key into is
# there straight away rather than after the next restart.
#
# This deliberately duplicates the template from synthDrivers/elevenlabs.py
# instead of importing it: while the add-on is being installed its own
# synthDrivers folder is not on NVDA's import path yet, so importing the driver
# here would fail.

import os

import globalVars
from logHandler import log

SETTINGS_FILENAME = "elevenlabs_settings.txt"

# Kept in step with SETTINGS_HEADER in synthDrivers/elevenlabs.py.
TEMPLATE = """\
# Settings for the ElevenLabs NVDA add-on, one name = value per line.
# api_key: your ElevenLabs API key. Paste it after the equals sign below.
# output_format: audio format the driver negotiated with your plan.
# unusable_voices, voice_check: voices this account cannot generate with,
# and a fingerprint of the account state they were worked out for.
# check_voices = no turns the voice check off.
# Remove a line to work that setting out again.
api_key = 
"""


def onInstall():
	path = os.path.join(globalVars.appArgs.configPath, SETTINGS_FILENAME)
	if os.path.isfile(path):
		# Left alone: it holds the user's key, and uninstalling the add-on does
		# not remove it, so an existing file is normal when reinstalling.
		log.info(f"ElevenLabs: {path} already exists and was left as it is.")
		return
	try:
		with open(path, "w", encoding="utf-8") as f:
			f.write(TEMPLATE)
	except OSError:
		log.error(f"ElevenLabs: could not create {path}", exc_info=True)
		return
	log.info(f"ElevenLabs: created {path}; add your API key to the api_key line.")

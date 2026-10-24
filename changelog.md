V1.4 (10/24/2026):
The API key is now set in a new ElevenLabs TTS category in NVDA's settings. The key is checked with ElevenLabs before it is saved, and is stored encrypted for your Windows user. ElevenLabs only appears in the synthesizer list once a key is set.
If you are upgrading: elevenlabs_settings.txt is no longer read, so set your key again in the new category. You can then delete the old file, which holds your key in plain text.
New option in the ElevenLabs TTS settings: a synthesizer for navigation. It speaks everything except say all, so ElevenLabs credits are only spent on what you choose to read. A command to switch it on and off can be assigned in Input Gestures.
While a synthesizer for navigation is loaded, the settings ring holds both synthesizers' settings, named after their synthesizer.
Say all no longer pauses between sentences while the next one is fetched from ElevenLabs.
During say all, the caret now follows the speech more closely.
Say all now tells ElevenLabs which text comes before and after each request, so intonation flows from one sentence to the next.
Speech may start sooner, as the connection to ElevenLabs is kept open between utterances.
Voice settings now only show what the chosen model supports. For Eleven v3, stability is a choice of Creative, Natural or Robust.

V1.3 (10/04/2026):
All settings and messages within the add-on are now localized.
If the ElevenLabs API suddenly becomes unavailable during a session, NVDA now reverts to using eSpeak instead of becoming silent.

V1.2 (10/02/2026):
New setting: Text normalization.
The list of available models is now checked against the active account.
If a model doesn't support a particular voice for some reason, the voice is now disabled for this model.
If a model doesn't support the speed parameter, it is now handled entirely by Sonic.
A few other small improvements.

V1.1.5 (09/30/2026):
Fixed a bug where an audio tag would only apply to the first sentence after it.

V1.1.4 (09/30/2026):
Audio tags are now supported, and can be enabled in voice settings.

V1.1.3 (09/30/2026):
Speech no longer pauses suddenly in the middle of a sentence.
Splitting of long text can now be turned off in voice settings.
Long text now only splits between whole sentences, unless the sentence is longer than alowed.

V1.1.2 (09/30/2026):
Longer text strings are now split up into shorter chunks.
Maximum waiting time for faster models is now much shorter, so NVDA doesn't hang for too long.

V1.1.1 (09/29/2026):
Selecting a new voice with one of the V4 models should now work more smoothly.

V1.1 (09/29/2026):
Added option to select any ElevenLabs model.
The language box is now read in NVDA's own language.
Any parameter unavailable in the model currently being used is now hidden.

V1.0 (08/31/2026):
Initial release

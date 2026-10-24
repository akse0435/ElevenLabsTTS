# ElevenLabsTTS

NVDA driver for the ElevenLabs Text-to-Speech API

## What is this

This add-on adds an ElevenLabs speech synthesizer to NVDA. It uses the ElevenLabs Text-to-Speech API to generate speech, meaning you must create an API key for your ElevenLabs account in order to use it. The add-on will automatically detect relevant information from your ElevenLabs account, such as highest available audio output format and which voices you can use.

## How to install

1) Create an API key for your ElevenLabs account on the website. If you restrict the key's permissions, it needs at least text to speech and reading voices; reading models and your subscription is used too.
2) Install the add-on and restart NVDA.
3) Open NVDA's settings (NVDA+N, Preferences, Settings) and go to the ElevenLabs TTS category. Press "Set API key...", paste your key and press Enter. The key is checked with ElevenLabs before it is saved.
4) Select ElevenLabs as your synthesizer. It only appears in the synthesizer list once a key is set. Settings such as the audio output format are worked out from your account automatically.

The key is stored encrypted for your Windows user in elevenlabs_settings.json in your NVDA configuration folder, so it only works for the same Windows user on the same computer. To remove it, use "Clear API key" in the same category.

If you used version 1.3 or earlier, the add-on no longer reads elevenlabs_settings.txt, so set your key again as above. You can delete the old file, which still holds your key in plain text.

## Keyboard commands

You can use the following keyboard commands in NVDA when the add-on is installed, regardless of whether or not you're using the synth:
- NVDA+shift+u: Check how many credits you've used on your ElevenLabs account, as well as your plan.
- NVDA+shift+V: Reload list of usable voices and models from your ElevenLabs account, good if you've upgraded your subscription while using the add-on.

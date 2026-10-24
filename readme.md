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

## Saving credits: a separate synthesizer for navigation

Every word ElevenLabs speaks costs credits, including menus, key echo and moving around a web page. To spend them only on what you actually want to read, choose a "Synthesizer for navigation" in the ElevenLabs TTS category of NVDA's settings. That synthesizer then speaks everything, and ElevenLabs only reads say all (NVDA+down arrow on the desktop layout, NVDA+a on the laptop layout, and the other say all commands). Find the article you want to read, start say all, and it is read with your ElevenLabs voice; stop it and you are back to the other synthesizer at once. Both stay loaded, so switching costs nothing.

The option is off by default ("None").

- The synthesizer for navigation speaks with the voice, rate, pitch and other settings it already has in NVDA. To change them, select that synthesizer in NVDA, set it up, then select ElevenLabs again. NVDA's voice settings and the settings ring keep showing ElevenLabs' settings, even while the other synthesizer is speaking. Settings from configuration profiles apply to both.
- Only say all you start yourself uses ElevenLabs. "Automatic say all on page load" in browse mode, for example, is read by the synthesizer for navigation. Anything NVDA says while say all reads, such as a notification, is spoken by ElevenLabs.
- If the synthesizer for navigation can't be loaded, eSpeak is used instead, and NVDA tells you so.
- If ElevenLabs stops working, for example when you run out of credits or the key no longer works, say all is read by the synthesizer for navigation for the rest of the session, and ElevenLabs is not asked again. If ElevenLabs can't be reached or its servers fail, a sentence it couldn't read is read by the other synthesizer, and ElevenLabs is given up after three such sentences in a row. To try ElevenLabs again, set a new API key, press NVDA+shift+v, or select ElevenLabs again.
- A configuration profile that selects another synthesizer, such as one for say all, unloads ElevenLabs altogether, along with the synthesizer for navigation.

## Keyboard commands

You can use the following keyboard commands in NVDA when the add-on is installed, regardless of whether or not you're using the synth:
- NVDA+shift+u: Check how many credits you've used on your ElevenLabs account, as well as your plan.
- NVDA+shift+V: Reload list of usable voices and models from your ElevenLabs account, good if you've upgraded your subscription while using the add-on.
- Switch between the synthesizer for navigation and ElevenLabs for all speech: no key is assigned. Assign one in NVDA's Input Gestures, under "ElevenLabs speech synthesizer". It switches to the synthesizer you chose last in the settings.

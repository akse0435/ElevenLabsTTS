# ElevenLabsTTS

NVDA driver for the ElevenLabs Text-to-Speech API

## What is this?

This add-on gives NVDA an ElevenLabs speech synthesizer. It uses the ElevenLabs Text-to-Speech API to generate speech, so you'll need to create an API key for your ElevenLabs account to use it. The add-on works out the details of your account by itself, such as the best audio output format you have and which voices you can use.

## How to install

1) Create an API key for your ElevenLabs account on the website. If you restrict the key's permissions, it needs at least text to speech and reading voices; reading models and your subscription are used as well.
2) Install the add-on and restart NVDA.
3) Open NVDA's settings (NVDA+N, Preferences, Settings) and go to the ElevenLabs TTS category. Press "Set API key...", paste your key and press Enter. The key is checked with ElevenLabs before it's saved.
4) Select ElevenLabs as your synthesizer. It only shows up in the synthesizer list once a key is set. Settings such as the audio output format are worked out from your account automatically.

Your key is stored encrypted for your Windows user in elevenlabs_settings.json in your NVDA configuration folder, so it only works for the same Windows user on the same computer. To remove it, use "Clear API key" in the same category.

If you used version 1.3 or earlier, the add-on no longer reads elevenlabs_settings.txt, so you'll need to set your key again as above. After that, you can delete the old file, which still holds your key in plain text.

## Saving credits: a separate synthesizer for navigation

Every word ElevenLabs speaks costs credits, and that includes menus, key echo and moving around a web page. To spend them only on what you actually want to read, choose a "Synthesizer for navigation" in the ElevenLabs TTS category of NVDA's settings. That synthesizer then speaks everything else, and ElevenLabs only reads say all (NVDA+down arrow on the desktop layout, NVDA+a on the laptop layout, and the other say all commands). Find the article you want to read and start say all, and you'll hear it in your ElevenLabs voice. Stop it and you're straight back to the other synthesizer. Both stay loaded, so switching between them doesn't slow anything down.

This option is off by default ("None").

The synthesizer for navigation speaks with the voice, rate, pitch and other settings it already has in NVDA. The synth settings ring holds both synthesizers' settings, with the synthesizer's name in front of each setting's name.

Only a say all you start yourself uses ElevenLabs. For example, "Automatic say all on page load" in browse mode is read by the synthesizer for navigation. On the other hand, anything NVDA says while say all is reading, such as a notification, is spoken by ElevenLabs.

If the synthesizer for navigation can't be loaded, eSpeak takes its place, and NVDA tells you so.

If ElevenLabs stops working, for example because you've run out of credits or your key no longer works, the synthesizer for navigation reads say all for the rest of the session, and the add-on stops asking ElevenLabs. If ElevenLabs can't be reached or its servers fail, the other synthesizer reads any sentence ElevenLabs couldn't, and the add-on gives up on ElevenLabs after three such sentences in a row. To try ElevenLabs again, set a new API key, press NVDA+shift+v, or select ElevenLabs again.

One thing to be aware of: a configuration profile that selects another synthesizer, such as a profile for say all, unloads ElevenLabs completely, and the synthesizer for navigation goes with it.

## Keyboard commands

These commands work whenever the add-on is installed, whether or not you're using the synth.

Press NVDA+shift+u to hear how many credits you've used on your ElevenLabs account, along with your plan.

Press NVDA+shift+v to reload the list of voices and models your ElevenLabs account can use. This is handy if you've upgraded your subscription while using the add-on.

Press NVDA+shift+t to switch between the synthesizer for navigation and ElevenLabs for all speech. Switching back on uses the synthesizer you last chose in the settings.

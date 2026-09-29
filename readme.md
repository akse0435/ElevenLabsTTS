# ElevenLabsTTS

NVDA driver for the ElevenLabs Text-to-Speech API

## What is this

This add-on adds an ElevenLabs speech synthesizer to NVDA. It uses the ElevenLabs Text-to-Speech API to generate speech, meaning you must create an API key for your ElevenLabs account in order to use it. The add-on will automatically detect relevant information from your ElevenLabs account, such as highest available audio output format and which voices you can use.

## How to install

1) Create an API key for your ElevenLabs account on the website.
2) Install the add-on. It will create a file in your NVDA configuration folder called elevenlabs_settings.txt. Open this file and find the line:

```
api_key = 
```

Paste your API key at the end of that line, then save the file and restart NVDA.
3) Done. There're a few more settings to be set in the file, such as audio output format, but these will automatically be detected from your account when you start using the synthesizer.

## Keyboard commands

You can use the following keyboard commands in NVDA when the add-on is installed, regardless of whether or not you're using the synth:
- NVDA+shift+u: Check how many credits you've used on your ElevenLabs account, as well as your plan.
- NVDA+shift+V: Reload list of usable voices from your ElevenLabs account, good if you've upgraded your subscription while using the add-on.

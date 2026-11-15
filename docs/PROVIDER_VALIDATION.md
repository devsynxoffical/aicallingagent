# Provider validation

What has been run against a real provider, with what result, and what has not.
Everything else in this repository was tested with stand-ins.

| Provider | State |
| --- | --- |
| Deepgram (speech to text) | **Partly validated** on 2026-11-15: models, languages, options and accuracy on clean audio, over HTTPS. Live streaming not tested |
| ElevenLabs (voice) | **Partly validated** on 2026-11-15: the key, the model, the engine's voice settings and telephone-format speech in English, French and Arabic, over HTTPS. The live connection a call uses not tested |
| Twilio (phone) | Not validated: no credentials |
| Anthropic (the agent's model) | Not validated: no credentials |

No phone call has been placed.

## Deepgram, 2026-11-15

Run from the build sandbox with a real Deepgram account, using
`tools/check_stt.py --prerecorded`, which takes the model, language and options from
the same code a call uses (`callagent/providers/stt.py`).

### What the audio was

Sentences a prospect would say on a sales call, **spoken by Deepgram's own synthetic
voices** and passed through 8 kHz mu-law, the format a phone line delivers. This is
the easy case: clear speech, no line noise, no accent, nobody talking over anyone,
and a recogniser hearing voices from the same company. Real calls will do worse.
For Arabic, Deepgram has no voice; a basic local synthesizer was used (see below).

### Results

| Mode the engine uses | Language | Sentences | Words that differ | Real mistakes |
| --- | --- | --- | --- | --- |
| Single language (`nova-3-general`) | English | 6 | 8 of 71 | 0: all eight are formatting (`12`, `03:30`, an email address written out) |
| Single language (`nova-3-general`) | French | 5 | 4 of 58 | 1: "me rappeler" heard as "mettre rappeler"; the rest is digits |
| Multi-language (`nova-3`, `language=multi`) | English | 6 | 8 of 71 | 0, the same output |
| Multi-language (`nova-3`, `language=multi`) | French | 5 | 4 of 58 | 1, the same output |
| Single language (`nova-3-general`) | Arabic | 5 | 42 of 42 | **Not a result**: see below |

Names, with and without the campaign's key terms (English, 3 sentences):

| | Heard |
| --- | --- |
| Without | "Bright Books", "Ledger Pro" (split in one of three sentences) |
| With `BrightBooks`, `LedgerPro`, `Lopez Dental`, `Okafor` | every name exactly as written, 0 of 29 words differ |

### What this establishes

1. The key, the model names (`nova-3-general`, `nova-3`) and the options the engine
   sends are accepted. Deepgram's model list shows both models available for
   streaming in English, French and Arabic.
2. Deepgram's multi-language mode covers English and French and **not Arabic**, as
   the engine already assumes: a campaign that follows the caller between Arabic and
   another language will not start on Deepgram and needs Soniox.
3. Passing the playbook's key terms makes company and product names come out as
   written. The engine does this for English single-language calls.
4. Spoken numbers arrive as digits: "three thirty in the afternoon" becomes "03:30
   in the afternoon". Whether the agent's model always reads that as 15:30 when it
   books a callback has not been tested and should be watched on the first calls.

### What it does not establish

- **Live streaming.** A call sends audio over a WebSocket and gets words back while
  the person is still talking. The build sandbox's proxy does not carry WebSockets,
  so none of this was exercised: how long after the person stops the final transcript
  arrives (the engine asks for 300 ms of silence), interim results, reconnecting
  after a dropped connection, and whether the provider's and the engine's ideas of
  "the person has finished" agree. `tools/check_stt.py` without `--prerecorded`
  measures the first of these; it has been run only as far as the connection attempt.
- **Arabic.** The local synthesizer's Arabic was not recognised at all, and
  Deepgram's language detection took it for another language, while the same
  synthesizer's English was transcribed correctly. The test audio is at fault, so
  nothing is known about Arabic accuracy. It needs recordings of real Arabic speech,
  or a voice provider's key.
- **Real telephone audio**: noise, accents, mobile compression, two people talking.
- **Cost.** These tests used well under a dollar of Deepgram's credit; the per-minute
  price was not checked against an invoice.

### Running it again

```bash
# on a machine with a direct connection to the provider, with DEEPGRAM_API_KEY set
python tools/check_stt.py --audio-dir recordings --language en --language fr            # streaming, with delays
python tools/check_stt.py --audio-dir recordings --language en --prerecorded            # accuracy only
python tools/check_stt.py --audio-dir recordings --language en --language fr --follow   # multi-language mode
```

`recordings/` holds `<language>-<n>.wav` (8 kHz, mono, 16-bit) and a `phrases.json`
with what is said in each. Recordings of real people on a real phone are what
matters; put those there.

## ElevenLabs, 2026-11-15

Run from the build sandbox with a real ElevenLabs account on the free plan.

- The key is accepted, the account has 21 ready-made voices, and the engine's model
  (`eleven_flash_v2_5`) lists English, French and Arabic.
- Speech was requested with the settings the engine sends (stability 0.45,
  similarity 0.8, style 0.25, speaker boost on, speed 1.0) in the telephone format
  (`ulaw_8000`), 16 sentences in three languages, plus five lines of an agent's part.
  Every request succeeded on the free plan.
- **First audio arrived 190 ms after the request (median; worst 457 ms, the first
  one), over 16 requests** on the HTTPS streaming endpoint, through the sandbox's
  proxy. A call uses a WebSocket connection that stays open, which was not tested,
  so this is an indication and not the figure a caller would experience.

What it does not establish: the live connection, interruption (stopping speech when
the caller talks over it), how the voice sounds to a person on a real phone, and the
free plan's licence, which does not cover commercial use.

## Deepgram hearing the ElevenLabs voice, 2026-11-15

The same sentences as above, spoken by ElevenLabs instead of Deepgram's own voices,
so the recogniser is no longer hearing its own company's speech. Still clean,
synthetic, single-speaker audio.

| Language | Sentences | Words that differ | Real mistakes |
| --- | --- | --- | --- |
| English | 6 | 8 of 71 | 0: digits, `03:30`, an email address written out |
| French | 5 | 3 of 58 | 0: digits and a hyphen |
| Arabic | 5 | 14 of 42 | 4, one of which changes the meaning |

**Arabic is now a result, and it is weaker than the other two.** Of the 14
differences, 9 are spelling conventions (a plain alef for one with hamza, a final
ha for ta marbuta, `12` for the words) that a reader would not call mistakes. The
real ones: two short words dropped ("نحن", "من"), one word heard in a dialect form
("واحنا" for "ونحن"), and **"الساعة الثالثة" (three o'clock) heard as "السعر
الثالث" (the third price)**. That last one is in a sentence asking for a callback
at a time, which is exactly where a mistake costs a meeting. Arabic calls need a
check on real recordings before anything is promised to a customer, and Soniox
should be compared on the same recordings.

This was the multi-language question too: English and French gave the same output
in multi-language mode as in single-language mode.

## Found while re-testing everything on a Sunday

Five tests failed on 2026-11-15, a Sunday, that had passed every other day: a
campaign does not dial on Sunday by default, which is right, and those tests were
about which lead is next and had not said "on any day". The tests were corrected;
the engine was not changed. Before the fix, the build would have failed on every
Sunday.

# Provider validation

What has been run against a real provider, with what result, and what has not.
Everything else in this repository was tested with stand-ins.

| Provider | State |
| --- | --- |
| Deepgram (speech to text) | **Partly validated** on 2026-11-15: models, languages, options and accuracy on clean audio, over HTTPS. Live streaming not tested |
| Twilio (phone) | Not validated: no credentials |
| ElevenLabs (voice) | Not validated: no credentials |
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

## Found while re-testing everything on a Sunday

Five tests failed on 2026-11-15, a Sunday, that had passed every other day: a
campaign does not dial on Sunday by default, which is right, and those tests were
about which lead is next and had not said "on any day". The tests were corrected;
the engine was not changed. Before the fix, the build would have failed on every
Sunday.

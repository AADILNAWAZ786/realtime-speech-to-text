# Speech recognition: manual evaluation guide

How to test the faster-whisper engine on **real English, Hindi and Hinglish recordings**,
and how to record the results. Automated tests use a fake model and prove the plumbing;
only real recordings tell us how well recognition works.

> **No accuracy claims yet.** Whisper decodes each window in one language, so Hindi-English
> code-switching is handled imperfectly. The defaults for Hinglish (`mixed` mode,
> `hi_prompted` strategy) are hypotheses. This guide is how we test them.

---

## 1. One-time setup

```bash
cd backend
source .venv/bin/activate
pip install -r requirements-dev.txt
```

The first run of a model downloads its weights from Hugging Face (needs internet):

| Model | Download | RAM in use (int8, approx.) | Use |
|---|---|---|---|
| `tiny` | ~75 MB | ~0.4 GB | plumbing checks only |
| `base` | ~145 MB | ~0.5 GB | fast CPU baseline |
| `small` | ~485 MB | ~1 GB | **default CPU candidate** |
| `medium` | ~1.5 GB | ~2.5 GB | better Hindi; slow on CPU |
| `large-v3-turbo` | ~1.6 GB | ~3 GB | GPU candidate |

Sizes are rough: measure on your machine. Weights are cached in `~/.cache/huggingface`
(or `WHISPER_DOWNLOAD_ROOT`). **Never put them in the repository.** `.gitignore` covers
`/model_cache/` and `/backend/model_cache/` if you keep a cache inside the project.

Download a model once:

```bash
python scripts/transcribe_file.py any.wav --model small
```

After that, add `--local-files-only` to guarantee no network access.

English-only models (`*.en`, `distil-*`) are rejected because they cannot transcribe Hindi.

---

## 2. Recording audio

- Any format PyAV can read works (WAV, MP3, M4A from a phone, OGG, …). The tool resamples
  to 16 kHz mono. 16 kHz mono 16-bit WAV is read directly with no resampling, which
  matches what the browser will send later.
- To convert a file yourself:
  `ffmpeg -i input.m4a -ar 16000 -ac 1 -c:a pcm_s16le output.wav`
- Record in a normal room with a laptop or phone microphone, the way real users will.
  Don't use a studio setup.
- Use 3–5 different speakers if you can: different voices, accents and genders.
- Keep the files out of git. `*.wav` is ignored; store recordings in a folder outside the
  repo, e.g. `~/stt-eval/`.

### File naming

`<language>_<length>_<condition>_<nn>.<ext>`, for example:

```
en_short_clean_01.wav      hi_long_noise10db_02.m4a      mixed_short_fast_03.wav
```

- language: `en`, `hi`, `mixed`
- length: `short` (< 5 s) or `long` (20–60 s)
- condition: `clean`, `slow`, `fast`, `noise20db`, `noise10db`, `noise5db`, `pauses`

The optional pytest run (section 6) relies on the `en_` / `hi_` / `mixed_` prefixes.

### Reference transcripts

For each recording, save what was actually said in `<same name>.txt`, using this convention:

- English: normal English spelling.
- Hindi: Devanagari.
- Hinglish: **Hindi words in Devanagari, English words in Latin script**, e.g.
  `आज की meeting cancel हो गई`. This is a scoring convention, not a statement about what
  the model should output.

---

## 3. Test matrix

Record at least one clip for each row. Two or three per row is better.

| # | Language | Length | Condition | What it checks |
|---|---|---|---|---|
| 1 | English | short | clean | baseline accuracy and latency |
| 2 | English | long | clean | long-audio segmentation, timestamps |
| 3 | Hindi | short | clean | Devanagari output, Hindi accuracy |
| 4 | Hindi | long | clean | segmentation, drift |
| 5 | Hinglish | short | clean | code-switching, output script |
| 6 | Hinglish | long | clean | consistency across sentences |
| 7 | all three | short | slow speech | elongated words |
| 8 | all three | short | fast speech | dropped or merged words |
| 9 | all three | short | background noise (fan, traffic, TV) | robustness, hallucinations |
| 10 | all three | long | 1–2 s pauses between sentences | no text invented in pauses; timestamps across gaps |
| 11 | none | 5 s | silence / noise only | **must produce no text** |

Suggested Hinglish sentences: "Kal ki meeting cancel ho gayi, Monday ko reschedule karte
hain." "Mera laptop slow chal raha hai, IT team ko ticket raise karna padega." "Aaj office
mein bahut kaam tha but finally project submit ho gaya."

---

## 4. Running the tool

```bash
# One file, each speech mode
python scripts/transcribe_file.py ~/stt-eval/en_short_clean_01.wav --mode en
python scripts/transcribe_file.py ~/stt-eval/hi_short_clean_01.wav --mode hi
python scripts/transcribe_file.py ~/stt-eval/mixed_short_clean_01.wav --mode mixed
python scripts/transcribe_file.py ~/stt-eval/mixed_short_clean_01.wav --mode auto

# Show a romanised version as well (deterministic transliteration of the output)
python scripts/transcribe_file.py ~/stt-eval/mixed_short_clean_01.wav --mode mixed --script roman

# Machine-readable output for a results spreadsheet
python scripts/transcribe_file.py clip.wav --mode mixed --format json > clip.small.mixed.json

# Choose model / hardware
python scripts/transcribe_file.py clip.wav --model base --threads 2
python scripts/transcribe_file.py clip.wav --model large-v3-turbo --device cuda   # GPU, float16

# The fast settings used for the live preview
python scripts/transcribe_file.py clip.wav --preset partial
```

| Option | Values | Default |
|---|---|---|
| `--model` | `tiny` `base` `small` `medium` `large-v3` `large-v3-turbo`, a repo id, or a path | `small` |
| `--mode` | `auto` `en` `hi` `mixed` | `auto` |
| `--mixed-strategy` | `hi_prompted` `hi` `auto` `multilingual` | `hi_prompted` |
| `--device` | `cpu` `cuda` `auto` | `cpu` |
| `--compute-type` | e.g. `int8`, `float16` | int8 on CPU, float16 on CUDA |
| `--threads` | CPU threads, 0 = default | 0 |
| `--script` | `as_recognised` `roman` `devanagari` | `as_recognised` |
| `--format` | `text` `json` | `text` |
| `--preset` | `final` (beam 5, VAD, word timings), `partial` (greedy, fast) | `final` |
| `--no-vad` | turn off faster-whisper's Silero VAD filter | VAD on for `final` |
| `--local-files-only` | never download | off |

Exit codes: `0` ok, `2` bad arguments, `3` audio error, `4` model load error, `5` inference error.

Defaults can also be set with `WHISPER_*` variables in `backend/.env` (see `.env.example`).

The text report shows the model, device, compute type and threads, model load time, mode
and strategy, detected or forced language, processing time and real-time factor, the
Devanagari/Latin character mix, the recognised text, and each segment with timestamps and
average log probability.

---

## 5. Comparing Hinglish strategies

Run every `mixed_*` recording through all four strategies:

```bash
for f in ~/stt-eval/mixed_*; do
  for s in hi_prompted hi auto multilingual; do
    python scripts/transcribe_file.py "$f" --mode mixed --mixed-strategy "$s" \
      --format json > "${f%.*}.small.$s.json"
  done
done
```

For each output, check:

1. **Accuracy:** word error rate (WER) against the reference. Before comparing, convert
   both texts to the same script (`--script roman` applies the same transliteration to the
   output; apply it to the reference too) so a script difference is not counted as an error.
   (`pip install jiwer` gives a one-line WER function; it is not a project dependency.)
2. **Script consistency:** the `Script mix` line. Are English words in Latin script, or
   transliterated into Devanagari (`मीटिंग`)?
3. **Translation errors:** did Hindi get *translated* into English, or the other way
   round? This changes meaning and is worse than a misspelling. Watch `--mode en` and the
   `auto` strategy closely.
4. **Wrong language or script:** Hindi detected as Urdu (`ur`, Perso-Arabic script) is a
   known Whisper failure. Note how often it happens.

The strategy with the best accuracy, and no translation or Urdu problems, becomes the
default (`WHISPER_MIXED_STRATEGY`).

---

## 6. Optional automated real-model run

Once a model is cached, the opt-in pytest suite runs it end to end. It never downloads:

```bash
STT_REAL_MODEL_TESTS=1 STT_REAL_MODEL=small STT_REAL_AUDIO_DIR=~/stt-eval \
  pytest tests/real_model -v
```

It checks that the model loads on CPU int8, that silence produces no text, that every mode
runs, and that each `en_` / `hi_` / `mixed_` recording produces text with valid timestamps.
It does not measure quality; sections 5 and 7 do that.

---

## 7. Results template

Record the hardware first: CPU model, vCPUs, RAM, GPU, OS, plus the `faster-whisper` and
`ctranslate2` versions.

| File | Mode | Strategy | Model | Lang (p) | WER | Script mix | Translation? | Processing s | RTF | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| mixed_short_clean_01 | mixed | hi_prompted | small | hi (forced) | | | | | | |

Also record:
- the model load time, first run and cached;
- the real-time factor (RTF) for short and long clips; RTF < 1 means faster than real time;
- whether silence or noise clips produced any text (hallucination);
- how long pauses affected segments and timestamps.

These numbers decide the production model, the default Hinglish strategy, and whether
CPU hosting is good enough (see the architecture plan, section 7).

---

## 8. Interpreting output scripts

- **Recognition** (what the model heard) and **script rendering** (how it is displayed) are
  separate. The recognised text is stored unchanged.
- `--script roman` is **deterministic transliteration** of the model's Devanagari output.
  It does not improve recognition. An English word the model wrote as `मीटिंग` becomes
  `miting`, not `meeting`. Medial schwa deletion isn't implemented, so `करते` becomes
  `karate`, while people usually write `karte`.
- `--script devanagari` keeps the recognised text. Latin words are not converted, because
  a rule cannot tell romanised Hindi (`kal`) from English (`call`).

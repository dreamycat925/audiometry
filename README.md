# Simple Audiometry Screening Streamlit App

This app is a **brief auditory input screening tool** intended for use before neuropsychological testing, especially when formal audiometry is not immediately available.

It presents pure tones through a PC and headphones and estimates air-conduction thresholds for each ear at `500 / 1000 / 2000 / 4000 Hz`. It then reports a conversational-frequency average and a 4-frequency average.

It is designed for **examiner-operated use**: the examiner controls tone presentation and records the patient's verbal or gesture response on screen.

> Important: Without calibration, the displayed values are **app-dB, not dB HL**. This tool must not be used for diagnosis, disability certification, hearing-aid fitting, or formal audiological assessment.

## What It Does

- Tests the right and left ears separately at `500 / 1000 / 2000 / 4000 Hz`
- Re-tests `1000 Hz` for a simple reliability check
- Calculates the traditional 4-frequency conversational average: `(500 + 2*1000 + 2000) / 4`
- Calculates the 4-frequency pure-tone average: `(500 + 1000 + 2000 + 4000) / 4`
- Shows a latest-run summary table, raw result table, and an audiogram-style chart
- Exports a session log CSV and a latest-run summary TXT file
- Supports undo of the most recent response
- Keeps an in-session run history
- Optionally shows estimated `dB HL` using a local calibration JSON profile

## Setup

### 1. Prepare Python

Python `3.10+` is recommended.

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Run the App

```bash
streamlit run audiometry_app.py
```

Then open the browser page and follow the on-screen instructions.

### 4. Run with Docker

For local-only Docker execution:

```bash
docker compose up --build -d
```

Then open:

```text
http://localhost:60000
```

For details, see `README_DOCKER.md`.

### Hosted App

Public URL:

`https://audiometry-2cduqgwpruh6vu8uxuibia.streamlit.app/`

## Recommended Use Conditions

- Use as quiet a room as possible
- Prefer wired headphones
- Keep the PC, browser, OS volume, browser volume, and headphones fixed
- Do not change OS or browser volume during testing
- Confirm left-right headphone orientation before starting
- Instruct the patient clearly, for example:

```text
Press "Heard" as soon as you hear the tone.
If you do not hear it, or if you are unsure, press "Not heard".
```

## Measurement Flow

By default, each ear is tested in this order:

```text
1000 Hz -> 2000 Hz -> 4000 Hz -> 500 Hz -> 1000 Hz retest
```

The threshold search is a fast ascending screening procedure:

1. Start at `40 app-dB`
2. If the tone is heard, decrease by `10 dB` steps
3. Once it is no longer heard, increase by `5 dB` steps
4. The first heard level on the ascending run is recorded as the threshold
5. If the start level is not heard, increase by `10 dB` steps until first response, then confirm in `5 dB` steps

This is a pragmatic screening method designed to finish in a few minutes. It is not a strict Hughson-Westlake implementation.

The default presentation range is `0` to `80 app-dB`. In the current internal output scale, the app limits the configurable maximum to `88 app-dB` to avoid digital clipping.

## Interpreting Results

### app-dB

Without calibration, `app-dB` is only an internal app scale.

```text
30 app-dB
40 app-dB
50 app-dB
```

do **not** mean `30 / 40 / 50 dB HL`.

The app now keeps the `app-dB` sound-output scale fixed internally, so changing the screening upper limit does not redefine the meaning of a given `app-dB` value.

In the current implementation:

```text
80 app-dB = -8 dBFS
88 app-dB = 0 dBFS
```

For that reason, values above `88 app-dB` are not allowed.

### Traditional 4-Frequency Conversational Average

```text
(500 + 2 x 1000 + 2000) / 4
```

This is useful as a rough speech-frequency summary.

### 4-Frequency Average

```text
(500 + 1000 + 2000 + 4000) / 4
```

This includes `4000 Hz`, so it reflects high-frequency loss somewhat more clearly.

### Censored Results at the Maximum Presentation Level

If the patient still does not respond at the maximum presentation level, the result is treated as **censored** rather than as a true threshold.

The summary therefore displays it as:

```text
>=(maximum presentation level + 5) app-dB
```

or, if calibration is applied:

```text
>=(maximum presentation level + 5 + offset) dB HL
```

These censored values are not used in the average calculations.

With the default maximum of `80 app-dB`, this appears as:

```text
>=85 app-dB
```

## Local Calibration Profile

Without calibration, the app should not display `dB HL`. If you want an approximate `dB HL` estimate for a fixed local setup, create an in-house calibration profile using the same hardware and software conditions.

Examples of conditions that should remain fixed:

```text
PC: same model
Browser: same browser
OS volume: 100%
Browser volume: 100%
Headphones: same model, ideally the same unit
Connection: wired
Room: as similar as possible
```

See `calibration_profile.example.json` for the expected structure.

```json
{
  "profile_name": "ward_profile_2026_01",
  "offsets_db": {
    "右": {"500": 5, "1000": 0, "2000": 3, "4000": 8},
    "左": {"500": 6, "1000": 1, "2000": 4, "4000": 10}
  }
}
```

The app interprets this as:

```text
app threshold + offsets_db = estimated dB HL
```

Example:

```text
Right 1000 Hz app threshold = 35 app-dB
Right 1000 Hz offset = 0
-> Estimated threshold = 35 dB HL
```

Calibration values should ideally be derived by comparing same-day app results with standard audiometry results using the same listening setup.

## Report Language

The app generates a neuropsychology-oriented note and a latest-run summary TXT. They include wording along these lines:

```text
This test is a brief auditory screening performed with a non-calibrated or locally calibrated PC/browser sound source and headphones.
It is not a replacement for standard pure-tone audiometry and must not be used for diagnosis, disability certification, or hearing-aid fitting.
It should be treated as a reference value for checking auditory input conditions before neuropsychological testing.
```

## Limitations

- Results are affected by room noise outside a sound booth
- Results depend strongly on headphone frequency response
- Output depends on OS and browser audio handling
- Bone conduction, masking, and air-bone gap assessment are not available
- Conductive vs sensorineural loss cannot be differentiated
- Auditory agnosia or pure word deafness cannot be diagnosed by this app
- Attention problems, aphasia, impaired comprehension, and delayed responses can affect results

If you see abnormal values, large asymmetry, large retest discrepancy, auditory complaints, or suspected auditory agnosia / pure word deafness, consider formal pure-tone audiometry, speech audiometry, and ENT evaluation.

## Files

```text
audiometry_app.py
requirements.txt
README.md
calibration_profile.example.json
```

## Development Notes

- Tones are generated as WAV data in Python and played with `st.audio`
- `st.audio(..., autoplay=True)` may still require user interaction depending on browser autoplay restrictions
- Test state is stored in `st.session_state`
- The app includes left/right headphone check tones before the run starts
- The latest run view includes summary cards, raw tables, an audiogram-style plot, and per-run logs

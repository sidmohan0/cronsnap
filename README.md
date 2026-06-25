# CronSnap

CronSnap is a local-first macOS activity sampler with a minimalist Tauri menu bar app. It watches the active window, labels activity with fast rules or a local llama.cpp vision model, writes JSONL logs under `~/Library/Application Support/CronSnap`, and renders archive summaries from those logs. Screenshots are temporary by default; full-screen capture requires explicit opt-in; OCR uses Apple Vision locally and is not saved unless you explicitly export command output.

## Usage

Install local OCR bridges and frontend dependencies:

```bash
python3 -m pip install -r requirements.txt
npm install
```

Run the menu bar app:

```bash
./script/build_and_run.sh
```

Useful engine commands:

```bash
python3 llama-screen.py status
python3 llama-screen.py archive --days 21
python3 llama-screen.py report today
python3 llama-screen.py report yesterday --format json --output -
python3 llama-screen.py ocr --format json
```

The app stores logs and exported reports in `~/Library/Application Support/CronSnap`. Existing repo-local `logs/` and `reports/` are imported once if app data is empty. Markdown reports are exports; JSONL logs remain the source of truth.

## Validation

```bash
python3 -m py_compile llama-screen.py
npm run build:ui
cargo check --manifest-path src-tauri/Cargo.toml
npm run build
```

# cronsnap

cronsnap is a local macOS activity sampler that watches the active window, labels what you are doing with lightweight rules or a local llama.cpp vision-language model, and writes daily JSONL logs. It can also turn those logs into Markdown reports summarizing time by app, activity, window, source, and session. Active-window capture fails closed by default if the window ID cannot be identified, so full-screen screenshots require an explicit opt-in flag.

Local OCR is available with `python3 llama-screen.py ocr`, which captures the active window and extracts text with Apple's on-device Vision framework for fast downstream parsing. Install the OCR bridge with `python3 -m pip install pyobjc-framework-Vision pyobjc-framework-Quartz`; use `--format json` for app/title/text/line metadata.

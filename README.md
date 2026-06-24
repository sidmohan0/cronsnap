# cronsnap

cronsnap is a local macOS activity sampler that watches the active window, labels what you are doing with lightweight rules or a local llama.cpp vision-language model, and writes daily JSONL logs. It can also turn those logs into Markdown reports summarizing time by app, activity, window, source, and session. Active-window capture fails closed by default if the window ID cannot be identified, so full-screen screenshots require an explicit opt-in flag.

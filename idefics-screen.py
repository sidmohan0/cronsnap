#!/usr/bin/env python3
"""
Watch the active macOS window and infer what you are doing with IDEFICS.

Install the likely dependencies:
  python3 -m pip install torch transformers accelerate pillow pyobjc-framework-Quartz sentencepiece

For 4-bit loading on a CUDA machine, also install bitsandbytes and run:
  python3 idefics-screen.py --load-in-4bit

macOS will usually ask for Screen Recording permission the first time this runs.
System Events may also require Accessibility permission to read the active app/window title.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image


MODEL_ID = "HuggingFaceM4/idefics-9b-instruct"


@dataclass
class ActiveWindow:
    app: str = "unknown app"
    title: str = ""
    window_id: Optional[int] = None


def run_osascript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def get_active_window_metadata() -> ActiveWindow:
    if platform.system() != "Darwin":
        return ActiveWindow()

    app = run_osascript(
        'tell application "System Events" to get name of first application process whose frontmost is true'
    )
    title = run_osascript(
        """
        tell application "System Events"
          set frontApp to first application process whose frontmost is true
          try
            get name of first window of frontApp
          on error
            return ""
          end try
        end tell
        """
    )

    window_id = find_frontmost_window_id(app, title)
    return ActiveWindow(app=app or "unknown app", title=title or "", window_id=window_id)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def find_frontmost_window_id(app: str, title: str) -> Optional[int]:
    try:
        import Quartz  # type: ignore
    except Exception:
        return None

    wanted_app = normalize(app)
    wanted_title = normalize(title)

    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []

    best_id = None
    for window in windows:
        owner = normalize(window.get("kCGWindowOwnerName", ""))
        name = normalize(window.get("kCGWindowName", ""))
        layer = window.get("kCGWindowLayer", 1)
        bounds = window.get("kCGWindowBounds", {}) or {}
        width = bounds.get("Width", 0)
        height = bounds.get("Height", 0)

        if layer != 0 or width < 50 or height < 50:
            continue
        if wanted_app and owner != wanted_app:
            continue

        window_id = window.get("kCGWindowNumber")
        if wanted_title and name == wanted_title:
            return int(window_id)
        if best_id is None:
            best_id = int(window_id)

    return best_id


def capture_screenshot(window: ActiveWindow, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "idefics-active-window.png"

    command = ["screencapture", "-x"]
    if platform.system() == "Darwin" and window.window_id is not None:
        command.extend(["-l", str(window.window_id)])
    command.append(str(path))

    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "screencapture failed. On macOS, grant Screen Recording permission to Terminal, "
            f"iTerm, or the app running Python. stderr={result.stderr.strip()!r}"
        )
    return path


def load_model(model_id: str, load_in_4bit: bool, dtype: str):
    import torch
    from transformers import AutoProcessor

    processor = AutoProcessor.from_pretrained(model_id)

    kwargs = {"device_map": "auto"}
    if load_in_4bit:
        kwargs["load_in_4bit"] = True
    elif dtype == "auto":
        if torch.cuda.is_available():
            kwargs["torch_dtype"] = torch.bfloat16
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            kwargs["torch_dtype"] = torch.float16
    elif dtype != "none":
        kwargs["torch_dtype"] = getattr(torch, dtype)

    try:
        from transformers import AutoModelForImageTextToText

        model = AutoModelForImageTextToText.from_pretrained(model_id, **kwargs)
    except Exception:
        from transformers import IdeficsForVisionText2Text

        model = IdeficsForVisionText2Text.from_pretrained(model_id, **kwargs)
    return processor, model


def tensor_device(model):
    try:
        return next(model.parameters()).device
    except Exception:
        return "cpu"


def infer_activity(processor, model, image_path: Path, window: ActiveWindow, max_new_tokens: int) -> str:
    import torch

    image = Image.open(image_path).convert("RGB")
    prompt = [
        [
            image,
            (
                "User: You are looking at a screenshot of my active computer window. "
                f"The active app is {window.app!r} and the active window title is {window.title!r}. "
                "Infer what I am doing in one short phrase. "
                "Use formats like 'writing a paper about {topic}', 'drafting email to {person}', "
                "'editing code for {project}', or 'reading {document/site}'. "
                "Do not mention that this is a screenshot unless that is the activity.\n"
                "Assistant:"
            ),
        ]
    ]

    inputs = processor(prompt, return_tensors="pt")
    device = tensor_device(model)
    inputs = {key: value.to(device) for key, value in inputs.items()}

    with torch.inference_mode():
        generated_ids = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            bad_words_ids=processor.tokenizer(["<image>", "<fake_token_around_image>"], add_special_tokens=False).input_ids,
        )

    generated_text = processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
    answer = generated_text.split("Assistant:")[-1].strip()
    return " ".join(answer.split())


def main() -> int:
    parser = argparse.ArgumentParser(description="Infer your current activity from the active window every N seconds.")
    parser.add_argument("--model-id", default=MODEL_ID)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--max-new-tokens", type=int, default=32)
    parser.add_argument("--load-in-4bit", action="store_true")
    parser.add_argument(
        "--dtype",
        default="auto",
        choices=["auto", "none", "float16", "bfloat16", "float32"],
        help="'auto' chooses a reasonable dtype; 'none' lets Transformers decide.",
    )
    parser.add_argument(
        "--keep-screenshots",
        action="store_true",
        help="Keep each screenshot in a temporary directory instead of overwriting one file.",
    )
    args = parser.parse_args()

    print(f"Loading {args.model_id}. This can take a while for the first run.", flush=True)
    processor, model = load_model(args.model_id, args.load_in_4bit, args.dtype)
    print("Loaded. Press Ctrl-C to stop.", flush=True)

    with tempfile.TemporaryDirectory(prefix="idefics-screen-") as tmp:
        out_dir = Path(tmp)
        try:
            while True:
                started = time.time()
                window = get_active_window_metadata()
                image_path = capture_screenshot(window, out_dir)

                if args.keep_screenshots:
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    kept_path = out_dir / f"active-window-{stamp}.png"
                    os.replace(image_path, kept_path)
                    image_path = kept_path

                activity = infer_activity(processor, model, image_path, window, args.max_new_tokens)
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                title = f" - {window.title}" if window.title else ""
                print(f"[{now}] {window.app}{title}: {activity}", flush=True)

                elapsed = time.time() - started
                time.sleep(max(0.0, args.interval - elapsed))
        except KeyboardInterrupt:
            print("\nStopped.", flush=True)
            return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Watch the active macOS window and infer your activity with llama.cpp + a lightweight VLM.

Prerequisite:
  curl -LsSf https://llama.app/install.sh | sh

Run:
  python3 llama-screen.py

The first run downloads the GGUF model and multimodal projector into llama.cpp's
cache. macOS may ask for Screen Recording permission. System Events may also
need Accessibility permission to read active app/window metadata.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Tuple


MODEL_REPO = "ggml-org/SmolVLM2-2.2B-Instruct-GGUF"
APP_NAME = "CronSnap"
APP_DATA_ENV = "CRONSNAP_DATA_DIR"


def app_data_dir() -> Path:
    configured = os.environ.get(APP_DATA_ENV)
    if configured:
        return Path(configured).expanduser()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / ".cronsnap"


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def reports_dir() -> Path:
    return app_data_dir() / "reports"


def ensure_app_data() -> None:
    logs_dir().mkdir(parents=True, exist_ok=True)
    reports_dir().mkdir(parents=True, exist_ok=True)

    legacy_logs = script_dir() / "logs"
    if legacy_logs.exists() and not any(logs_dir().glob("activity-*.jsonl")):
        for source in legacy_logs.glob("activity-*.jsonl"):
            target = logs_dir() / source.name
            if not target.exists():
                shutil.copyfile(source, target)

    legacy_reports = script_dir() / "reports"
    if legacy_reports.exists() and not any(reports_dir().glob("activity-*.md")):
        for source in legacy_reports.glob("activity-*.md"):
            target = reports_dir() / source.name
            if not target.exists():
                shutil.copyfile(source, target)


@dataclass
class ActiveWindow:
    app: str = "unknown app"
    title: str = ""
    window_id: Optional[int] = None


@dataclass
class OCRResult:
    text: str
    lines: list[dict]
    app: str
    title: str
    screenshot: str


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


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def title_topic(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        return ""

    separators = [" — ", " – ", " - "]
    parts = [title]
    for separator in separators:
        if separator in title:
            parts = [part.strip() for part in title.split(separator) if part.strip()]
            break

    ignored = {
        "google chrome",
        "chrome",
        "mail",
        "inbox",
        "ghostty",
        "codex",
        "new tab",
    }
    for part in parts:
        if normalize(part) not in ignored and not re.search(r"\b\d+\s+(messages?|unread)\b", part, re.I):
            return part
    return parts[0] if parts else title


def rule_activity(window: ActiveWindow) -> Optional[str]:
    app = normalize(window.app)
    title = normalize(window.title)
    raw_title = window.title.strip()

    if app in {"mail", "apple mail"}:
        if any(word in title for word in ["compose", "new message", "reply", "re:", "fwd:", "draft"]):
            return "drafting email"
        if "sent" in title:
            return "reviewing sent mail"
        if "inbox" in title or "messages" in title or "unread" in title:
            return "checking mail"
        return "using mail"

    if app in {"google chrome", "chrome", "safari", "arc", "firefox"}:
        topic = title_topic(raw_title)
        if "hugging face" in title or "huggingface" in title:
            return "researching models"
        if "github" in title:
            return "reviewing code"
        if "google search" in title or title.startswith("search"):
            return "searching web"
        if topic and normalize(topic) not in {"new tab", "google chrome"}:
            return clean_activity(f"reading {topic}")
        return "browsing web"

    if app == "ghostty":
        if "tmux" in title or "llama-screen" in title:
            return "monitoring activity script"
        return "using terminal"

    if app == "codex":
        if any(word in title for word in ["diff", "review"]):
            return "reviewing code"
        return "working with codex"

    return None


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
    return ActiveWindow(app=app or "unknown app", title=title or "", window_id=find_frontmost_window_id(app, title))


def capture_screenshot(window: ActiveWindow, out_dir: Path, allow_full_screen: bool = False) -> Path:
    if platform.system() != "Darwin":
        raise RuntimeError("llama-screen currently requires macOS because it uses screencapture.")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "active-window.png"

    command = ["screencapture", "-x"]

    if window.window_id is not None:
        command.extend(["-l", str(window.window_id)])
    elif not allow_full_screen:
        raise RuntimeError(
            "Could not identify the active window ID; refusing to capture the full screen. "
            "Install/enable Quartz support or pass --allow-full-screen-capture."
        )

    command.append(str(path))

    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "screencapture failed. Grant Screen Recording permission to the app running Python. "
            f"stderr={result.stderr.strip()!r}"
        )
    return path


def load_vision_frameworks():
    try:
        import Foundation  # type: ignore
        import Vision  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "Local OCR requires Apple's Vision framework through PyObjC. "
            "Install it with: python3 -m pip install pyobjc-framework-Vision pyobjc-framework-Quartz"
        ) from exc
    return Foundation, Vision


def vision_ocr_image(
    image_path: Path,
    recognition_level: str = "fast",
    languages: Optional[list[str]] = None,
    language_correction: bool = True,
) -> tuple[str, list[dict]]:
    Foundation, Vision = load_vision_frameworks()

    image_url = Foundation.NSURL.fileURLWithPath_(str(image_path))
    request = Vision.VNRecognizeTextRequest.alloc().init()

    level_name = "VNRequestTextRecognitionLevelAccurate" if recognition_level == "accurate" else "VNRequestTextRecognitionLevelFast"
    if hasattr(Vision, level_name):
        request.setRecognitionLevel_(getattr(Vision, level_name))
    if languages:
        request.setRecognitionLanguages_(languages)
    if hasattr(request, "setUsesLanguageCorrection_"):
        request.setUsesLanguageCorrection_(language_correction)

    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(image_url, {})
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision OCR failed: {error}")

    lines = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        text = str(candidate.string()).strip()
        if not text:
            continue
        lines.append(
            {
                "text": text,
                "confidence": float(candidate.confidence()),
            }
        )

    text = "\n".join(line["text"] for line in lines)
    return text, lines


def read_active_window_text(args: argparse.Namespace) -> OCRResult:
    window = get_active_window_metadata()
    with tempfile.TemporaryDirectory(prefix="cronsnap-ocr-") as tmp:
        image_path = capture_screenshot(
            window,
            Path(tmp),
            allow_full_screen=args.allow_full_screen_capture,
        )
        text, lines = vision_ocr_image(
            image_path,
            recognition_level=args.recognition_level,
            languages=args.language,
            language_correction=not args.no_language_correction,
        )

        screenshot = ""
        if args.keep_screenshot:
            args.keep_screenshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(image_path, args.keep_screenshot)
            screenshot = str(args.keep_screenshot)

    return OCRResult(text=text, lines=lines, app=window.app, title=window.title, screenshot=screenshot)


def post_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


def wait_for_server(base_url: str, process: subprocess.Popen, timeout: float) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"llama serve exited early with code {process.returncode}")
        try:
            get_json(f"{base_url}/health", timeout=2)
            return
        except Exception as exc:
            last_error = str(exc)
            time.sleep(1)
    raise TimeoutError(f"Timed out waiting for llama serve at {base_url}. Last error: {last_error}")


def start_llama_server(args: argparse.Namespace) -> subprocess.Popen:
    llama = shutil.which("llama") or os.path.expanduser("~/.local/bin/llama")
    if not Path(llama).exists():
        raise FileNotFoundError("Could not find llama. Install it with: curl -LsSf https://llama.app/install.sh | sh")

    command = [
        llama,
        "serve",
        "-hf",
        args.model,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--ctx-size",
        str(args.ctx_size),
        "--gpu-layers",
        str(args.gpu_layers),
        "--temp",
        str(args.temperature),
        "--image-min-tokens",
        str(args.image_min_tokens),
        "--image-max-tokens",
        str(args.image_max_tokens),
    ]
    if args.verbose_llama:
        stdout = None
        stderr = None
    else:
        stdout = subprocess.DEVNULL
        stderr = subprocess.STDOUT
    return subprocess.Popen(command, stdout=stdout, stderr=stderr)


def image_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def clean_activity(text: str) -> str:
    text = text.strip().replace("\n", " ")
    text = re.sub(r"(?i)^the user is currently\s+", "", text)
    text = re.sub(r"(?i)^the user is\s+", "", text)
    text = re.sub(r"(?i)^user is\s+", "", text)
    text = re.sub(r"(?i)^observer\s+", "", text)
    text = re.sub(r"(?i)\bthe user\b.*$", "", text)
    text = re.sub(r"^\s*[-*\"']+", "", text)
    text = re.sub(r"[\"']+\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip(".")
    text = re.split(r"[,.;:!?]", text, maxsplit=1)[0].strip()
    if re.search(r"(?i)\b(observer|activity script|script running)\b", text):
        return "monitoring activity script"
    if normalize(text) in {"open", "analyze", "ghostly", "inbox", "unknown"}:
        return "unknown activity"
    words = text.split()
    if len(words) > 5:
        text = " ".join(words[:5])
    return text or "unknown activity"


def infer_activity(base_url: str, image_path: Path, window: ActiveWindow, args: argparse.Namespace) -> str:
    prompt = (
        "You are looking at a screenshot of my active computer window plus metadata.\n"
        f"Active app: {window.app}\n"
        f"Active window title: {window.title or '(none)'}\n\n"
        "Infer the user's current high-level activity from visible evidence. "
        "The app name and window title are clues, but do not simply restate them. "
        "If the window shows a terminal running an observer script, infer the broader task "
        "only if the visible conversation or text supports it. "
        "Return only a short activity label, maximum 5 words. "
        "Do not write a sentence. Do not include quotes, punctuation, metadata, or explanation."
    )
    payload = {
        "model": args.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url(image_path)}},
                ],
            }
        ],
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
    }
    response = post_json(f"{base_url}/v1/chat/completions", payload, timeout=args.request_timeout)
    return clean_activity(response["choices"][0]["message"]["content"])


def activity_for_window(
    base_url: str,
    image_path: Path,
    window: ActiveWindow,
    args: argparse.Namespace,
) -> Tuple[str, str]:
    rule_label = rule_activity(window)
    if rule_label:
        return rule_label, "rule"
    return infer_activity(base_url, image_path, window, args), "model"


def log_path(log_dir: Path, when: datetime) -> Path:
    return log_dir / f"activity-{when.strftime('%Y-%m-%d')}.jsonl"


def write_log(log_dir: Path, when: datetime, window: ActiveWindow, activity: str, source: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": when.isoformat(timespec="seconds"),
        "app": window.app,
        "title": window.title,
        "activity": activity,
        "source": source,
    }
    with log_path(log_dir, when).open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def load_records(paths: Iterable[Path]) -> list[dict]:
    records = []
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    record["_ts"] = parse_ts(record["ts"])
                    records.append(record)
                except Exception as exc:
                    print(f"Skipping {path}:{line_number}: {exc}", file=sys.stderr)
    return sorted(records, key=lambda item: item["_ts"])


def seconds_to_hm(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def display_title(title: str, max_length: int = 80) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    if not title:
        return "(untitled)"
    if len(title) <= max_length:
        return title
    return title[: max_length - 1].rstrip() + "..."


def is_low_signal(record: dict) -> bool:
    activity = normalize(record.get("activity", ""))
    if activity in {"unknown activity", "open", "analyze", "ghostly", "inbox"}:
        return True
    if "the user" in activity:
        return True
    return False


def row_duration(records: list[dict], index: int, default_interval: float, max_gap: float) -> float:
    if index + 1 >= len(records):
        return default_interval
    delta = (records[index + 1]["_ts"] - records[index]["_ts"]).total_seconds()
    if delta <= 0:
        return 0
    return min(delta, max_gap)


def aggregate_records(
    records: list[dict],
    default_interval: float,
    max_gap: float,
    exclude_low_signal: bool,
    exclude_monitoring: bool,
) -> tuple[dict, list[dict]]:
    totals = {
        "app": defaultdict(float),
        "activity": defaultdict(float),
        "window": defaultdict(float),
        "source": defaultdict(float),
    }
    samples = []
    for index, record in enumerate(records):
        if exclude_low_signal and is_low_signal(record):
            continue
        if exclude_monitoring and normalize(record.get("activity", "")) == "monitoring activity script":
            continue

        duration = row_duration(records, index, default_interval, max_gap)
        if duration <= 0:
            continue

        app = record.get("app") or "unknown app"
        title = record.get("title") or ""
        activity = record.get("activity") or "unknown activity"
        source = record.get("source") or "unknown"

        totals["app"][app] += duration
        totals["activity"][activity] += duration
        totals["window"][(app, title)] += duration
        totals["source"][source] += duration
        samples.append({**record, "_duration": duration})

    return totals, samples


def build_sessions(samples: list[dict], min_session_seconds: float, max_gap: float) -> list[dict]:
    sessions = []
    current = None
    previous = None

    for sample in samples:
        key = (sample.get("app"), sample.get("activity"))
        if current and previous:
            gap = (sample["_ts"] - previous["_ts"]).total_seconds()
            same_session = key == current["key"] and gap <= max_gap
        else:
            same_session = False

        if same_session:
            current["end"] = sample["_ts"] + timedelta_seconds(sample["_duration"])
            current["seconds"] += sample["_duration"]
            current["count"] += 1
        else:
            if current and current["seconds"] >= min_session_seconds:
                sessions.append(current)
            current = {
                "key": key,
                "app": sample.get("app") or "unknown app",
                "activity": sample.get("activity") or "unknown activity",
                "title": sample.get("title") or "",
                "start": sample["_ts"],
                "end": sample["_ts"] + timedelta_seconds(sample["_duration"]),
                "seconds": sample["_duration"],
                "count": 1,
            }
        previous = sample

    if current and current["seconds"] >= min_session_seconds:
        sessions.append(current)
    return sessions


def timedelta_seconds(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=seconds)


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def top_rows(counter: dict, total_seconds: float, limit: int) -> list[list[str]]:
    rows = []
    for label, seconds in sorted(counter.items(), key=lambda item: item[1], reverse=True)[:limit]:
        share = (seconds / total_seconds * 100) if total_seconds else 0
        rows.append([str(label), seconds_to_hm(seconds), f"{share:.1f}%"])
    return rows


def window_rows(counter: dict, limit: int) -> list[list[str]]:
    rows = []
    for (app, title), seconds in sorted(counter.items(), key=lambda item: item[1], reverse=True)[:limit]:
        rows.append([app, display_title(title), seconds_to_hm(seconds)])
    return rows


def session_rows(sessions: list[dict], limit: int) -> list[list[str]]:
    rows = []
    for session in sessions[:limit]:
        rows.append(
            [
                f"{session['start'].strftime('%H:%M:%S')} - {session['end'].strftime('%H:%M:%S')}",
                session["app"],
                session["activity"],
                seconds_to_hm(session["seconds"]),
            ]
        )
    return rows


def date_label_from_paths(paths: Iterable[Path]) -> str:
    labels = []
    for path in paths:
        match = re.search(r"activity-(\d{4}-\d{2}-\d{2})\.jsonl$", path.name)
        if match:
            labels.append(match.group(1))
    labels = sorted(set(labels))
    if len(labels) == 1:
        return labels[0]
    if labels:
        return f"{labels[0]} to {labels[-1]}"
    return "multiple days"


def summarize_records(args: argparse.Namespace) -> dict:
    records = load_records(args.files)
    totals, samples = aggregate_records(
        records,
        default_interval=args.interval,
        max_gap=args.max_gap_seconds,
        exclude_low_signal=not args.include_low_signal,
        exclude_monitoring=args.exclude_monitoring,
    )
    sessions = build_sessions(samples, args.min_session_seconds, args.max_gap_seconds)
    total_seconds = sum(totals["app"].values())
    date_label = date_label_from_paths(args.files)
    if records:
        first = records[0]["_ts"].date()
        last = records[-1]["_ts"].date()
        date_label = str(first) if first == last else f"{first} to {last}"

    app_rows = top_rows(totals["app"], total_seconds, args.limit)
    activity_rows = top_rows(totals["activity"], total_seconds, args.limit)
    source_rows = top_rows(totals["source"], total_seconds, args.limit)
    windows = [
        {
            "app": app,
            "title": display_title(title),
            "seconds": seconds,
            "duration": seconds_to_hm(seconds),
        }
        for (app, title), seconds in sorted(totals["window"].items(), key=lambda item: item[1], reverse=True)[
            : args.limit
        ]
    ]
    session_items = [
        {
            "start": session["start"].isoformat(timespec="seconds"),
            "end": session["end"].isoformat(timespec="seconds"),
            "start_time": session["start"].strftime("%H:%M:%S"),
            "end_time": session["end"].strftime("%H:%M:%S"),
            "app": session["app"],
            "activity": session["activity"],
            "title": session["title"],
            "seconds": session["seconds"],
            "duration": seconds_to_hm(session["seconds"]),
        }
        for session in sessions[: args.session_limit]
    ]
    return {
        "date_label": date_label,
        "files": [str(path) for path in args.files],
        "samples_read": len(records),
        "samples_included": len(samples),
        "accounted_seconds": total_seconds,
        "accounted": seconds_to_hm(total_seconds),
        "max_gap_seconds": args.max_gap_seconds,
        "apps": [
            {"name": row[0], "duration": row[1], "share": row[2], "seconds": totals["app"][row[0]]}
            for row in app_rows
        ],
        "activities": [
            {"name": row[0], "duration": row[1], "share": row[2], "seconds": totals["activity"][row[0]]}
            for row in activity_rows
        ],
        "windows": windows,
        "sources": [
            {"name": row[0], "duration": row[1], "share": row[2], "seconds": totals["source"][row[0]]}
            for row in source_rows
        ],
        "sessions": session_items,
    }


def generate_report(args: argparse.Namespace) -> str:
    summary = summarize_records(args)

    lines = [
        f"# Activity Report: {summary['date_label']}",
        "",
        f"- Samples read: {summary['samples_read']}",
        f"- Samples included: {summary['samples_included']}",
        f"- Accounted time: {summary['accounted']}",
        f"- Gap cap: {seconds_to_hm(args.max_gap_seconds)}",
        "",
        "## Time By App",
        markdown_table(["App", "Time", "Share"], [[row["name"], row["duration"], row["share"]] for row in summary["apps"]]),
        "",
        "## Time By Activity",
        markdown_table(
            ["Activity", "Time", "Share"],
            [[row["name"], row["duration"], row["share"]] for row in summary["activities"]],
        ),
        "",
        "## Top Windows",
        markdown_table(
            ["App", "Window", "Time"],
            [[row["app"], row["title"], row["duration"]] for row in summary["windows"]],
        ),
        "",
        "## Source Mix",
        markdown_table(
            ["Source", "Time", "Share"],
            [[row["name"], row["duration"], row["share"]] for row in summary["sources"]],
        ),
        "",
        "## Timeline Sessions",
        markdown_table(
            ["Time", "App", "Activity", "Duration"],
            [
                [f"{row['start_time']} - {row['end_time']}", row["app"], row["activity"], row["duration"]]
                for row in summary["sessions"]
            ],
        ),
        "",
    ]
    return "\n".join(lines)


def add_watch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=MODEL_REPO)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--ctx-size", type=int, default=4096)
    parser.add_argument("--gpu-layers", type=int, default=99)
    parser.add_argument("--image-min-tokens", type=int, default=1024)
    parser.add_argument("--image-max-tokens", type=int, default=1024)
    parser.add_argument("--max-tokens", type=int, default=12)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--startup-timeout", type=float, default=600.0)
    parser.add_argument("--request-timeout", type=float, default=180.0)
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=logs_dir(),
        help="Directory for daily JSONL activity logs. Use --no-log to disable.",
    )
    parser.add_argument("--no-log", action="store_true", help="Print activity but do not write JSONL logs.")
    parser.add_argument(
        "--allow-full-screen-capture",
        action="store_true",
        help="Allow fallback to full-screen screenshots if active-window capture is unavailable.",
    )
    parser.add_argument("--verbose-llama", action="store_true")


def activity_log_path(day: datetime) -> Path:
    return logs_dir() / f"activity-{day.strftime('%Y-%m-%d')}.jsonl"


def activity_report_path(day: datetime) -> Path:
    return reports_dir() / f"activity-{day.strftime('%Y-%m-%d')}.md"


def resolve_report_args(args: argparse.Namespace) -> None:
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    shortcuts = {
        "today": today,
        "yesterday": yesterday,
    }

    if not args.inputs:
        args.files = [activity_log_path(today)]
        if args.output is None:
            args.output = activity_report_path(today)
        return

    files = []
    report_day = None
    for item in args.inputs:
        day = shortcuts.get(item.lower())
        if day is None:
            files.append(Path(item))
            continue
        files.append(activity_log_path(day))
        report_day = day

    args.files = files
    if args.output is None:
        args.output = activity_report_path(report_day) if report_day and len(files) == 1 else Path("-")


def add_report_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "inputs",
        nargs="*",
        help="JSONL log file(s) to summarize, or date shortcuts: today, yesterday.",
    )
    parser.add_argument("--interval", type=float, default=10.0, help="Default seconds for the final sample.")
    parser.add_argument("--max-gap-seconds", type=float, default=30.0, help="Cap time credited across logging gaps.")
    parser.add_argument("--min-session-seconds", type=float, default=60.0, help="Minimum session length shown in timeline.")
    parser.add_argument("--limit", type=int, default=10, help="Rows to show in top tables.")
    parser.add_argument("--session-limit", type=int, default=20, help="Timeline session rows to show.")
    parser.add_argument("--include-low-signal", action="store_true", help="Include low-signal labels in calculations.")
    parser.add_argument("--exclude-monitoring", action="store_true", help="Exclude watcher/monitoring time.")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--output", type=Path, default=None, help="Markdown report path. Use '-' for stdout.")


def add_ocr_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--recognition-level",
        choices=["fast", "accurate"],
        default="fast",
        help="Use fast OCR for low latency or accurate OCR for harder text.",
    )
    parser.add_argument(
        "--language",
        action="append",
        help="Recognition language tag, such as en-US. Can be passed multiple times.",
    )
    parser.add_argument(
        "--no-language-correction",
        action="store_true",
        help="Disable Vision language correction.",
    )
    parser.add_argument(
        "--allow-full-screen-capture",
        action="store_true",
        help="Allow fallback to full-screen screenshots if active-window capture is unavailable.",
    )
    parser.add_argument(
        "--keep-screenshot",
        type=Path,
        help="Copy the captured active-window screenshot to this path for debugging.",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--output", type=Path, default=Path("-"), help="OCR output path. Use '-' for stdout.")


def add_archive_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--days", type=int, default=21, help="Number of recent days to include.")
    parser.add_argument("--limit", type=int, default=10, help="Rows to show in top tables.")
    parser.add_argument("--session-limit", type=int, default=40, help="Timeline session rows per day.")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--max-gap-seconds", type=float, default=30.0)
    parser.add_argument("--min-session-seconds", type=float, default=60.0)
    parser.add_argument("--include-low-signal", action="store_true")
    parser.add_argument("--exclude-monitoring", action="store_true")


def status_payload() -> dict:
    ensure_app_data()
    today = datetime.now()
    log = activity_log_path(today)
    sample_count = 0
    last_sample = None
    if log.exists():
        records = load_records([log])
        sample_count = len(records)
        if records:
            last = records[-1]
            last_sample = {
                "ts": last.get("ts"),
                "app": last.get("app"),
                "title": last.get("title"),
                "activity": last.get("activity"),
                "source": last.get("source"),
            }
    return {
        "app": APP_NAME,
        "data_dir": str(app_data_dir()),
        "logs_dir": str(logs_dir()),
        "reports_dir": str(reports_dir()),
        "today": today.date().isoformat(),
        "today_log": str(log),
        "today_samples": sample_count,
        "last_sample": last_sample,
    }


def day_summary(day: date, args: argparse.Namespace) -> Optional[dict]:
    ensure_app_data()
    log = logs_dir() / f"activity-{day.isoformat()}.jsonl"
    if not log.exists():
        return None
    ns = argparse.Namespace(
        files=[log],
        interval=args.interval,
        max_gap_seconds=args.max_gap_seconds,
        min_session_seconds=args.min_session_seconds,
        limit=args.limit,
        session_limit=args.session_limit,
        include_low_signal=args.include_low_signal,
        exclude_monitoring=args.exclude_monitoring,
    )
    summary = summarize_records(ns)
    summary["date"] = day.isoformat()
    return summary


def archive_payload(args: argparse.Namespace) -> dict:
    today = datetime.now().date()
    days = []
    for offset in range(args.days):
        summary = day_summary(today - timedelta(days=offset), args)
        if summary:
            days.append(summary)
    return {
        "data_dir": str(app_data_dir()),
        "days": days,
    }


def run_watch(args: argparse.Namespace) -> int:
    ensure_app_data()
    base_url = f"http://{args.host}:{args.port}"
    print(f"Starting llama.cpp server for {args.model}", flush=True)
    server = start_llama_server(args)

    try:
        wait_for_server(base_url, server, args.startup_timeout)
        print(f"llama.cpp is ready at {base_url}. Press Ctrl-C to stop.", flush=True)

        with tempfile.TemporaryDirectory(prefix="llama-screen-") as tmp:
            out_dir = Path(tmp)
            while True:
                started = time.time()
                try:
                    window = get_active_window_metadata()
                    rule_label = rule_activity(window)
                    if rule_label:
                        activity, source = rule_label, "rule"
                    else:
                        image_path = capture_screenshot(
                            window,
                            out_dir,
                            allow_full_screen=args.allow_full_screen_capture,
                        )
                        activity, source = infer_activity(base_url, image_path, window, args), "model"
                    now_dt = datetime.now()
                    if not args.no_log:
                        write_log(args.log_dir, now_dt, window, activity, source)
                    now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                    title = f" - {window.title}" if window.title else ""
                    print(f"[{now}] {window.app}{title}: {activity}", flush=True)
                except Exception as exc:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(f"[{now}] error: {exc}", flush=True)
                time.sleep(max(0.0, args.interval - (time.time() - started)))
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def run_report(args: argparse.Namespace) -> int:
    ensure_app_data()
    resolve_report_args(args)
    report = json.dumps(summarize_records(args), ensure_ascii=False, indent=2) if args.format == "json" else generate_report(args)
    if str(args.output) == "-":
        print(report)
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


def run_status(args: argparse.Namespace) -> int:
    print(json.dumps(status_payload(), ensure_ascii=False, indent=2))
    return 0


def run_archive(args: argparse.Namespace) -> int:
    print(json.dumps(archive_payload(args), ensure_ascii=False, indent=2))
    return 0


def run_ocr(args: argparse.Namespace) -> int:
    try:
        result = read_active_window_text(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        output = json.dumps(
            {
                "app": result.app,
                "title": result.title,
                "text": result.text,
                "lines": result.lines,
                "screenshot": result.screenshot,
            },
            ensure_ascii=False,
            indent=2,
        )
    else:
        output = result.text

    if str(args.output) == "-":
        print(output)
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


def main() -> int:
    if len(sys.argv) == 1:
        parser = argparse.ArgumentParser(description="Watch your active window and log local activity labels.")
        add_watch_args(parser)
        args = parser.parse_args()
        return run_watch(args)

    subcommand = sys.argv[1]
    if subcommand == "report":
        parser = argparse.ArgumentParser(description="Generate deterministic Markdown reports from activity JSONL logs.")
        add_report_args(parser)
        args = parser.parse_args(sys.argv[2:])
        return run_report(args)
    if subcommand == "ocr":
        parser = argparse.ArgumentParser(description="Extract text from the active macOS window with local Vision OCR.")
        add_ocr_args(parser)
        args = parser.parse_args(sys.argv[2:])
        return run_ocr(args)
    if subcommand == "status":
        parser = argparse.ArgumentParser(description="Print local CronSnap status as JSON.")
        args = parser.parse_args(sys.argv[2:])
        return run_status(args)
    if subcommand == "archive":
        parser = argparse.ArgumentParser(description="Print recent CronSnap archive summaries as JSON.")
        add_archive_args(parser)
        args = parser.parse_args(sys.argv[2:])
        return run_archive(args)

    parser = argparse.ArgumentParser(description="Watch your active window and log local activity labels.")
    add_watch_args(parser)
    args = parser.parse_args()
    return run_watch(args)


if __name__ == "__main__":
    sys.exit(main())

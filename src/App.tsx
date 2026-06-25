import {
  Archive,
  CalendarDays,
  Eye,
  FileText,
  FolderOpen,
  Pause,
  Play,
  RefreshCw,
  Settings,
  Shield,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import "./styles.css";

type Status = {
  app: string;
  data_dir: string;
  logs_dir: string;
  reports_dir: string;
  today: string;
  today_samples: number;
  last_sample?: {
    ts?: string;
    app?: string;
    title?: string;
    activity?: string;
    source?: string;
  } | null;
};

type Row = {
  name: string;
  duration: string;
  share: string;
  seconds: number;
};

type Session = {
  start_time: string;
  end_time: string;
  app: string;
  activity: string;
  duration: string;
};

type DaySummary = {
  date: string;
  date_label: string;
  samples_read: number;
  samples_included: number;
  accounted: string;
  accounted_seconds: number;
  apps: Row[];
  activities: Row[];
  sessions: Session[];
};

type ArchivePayload = {
  data_dir: string;
  days: DaySummary[];
};

type OcrPayload = {
  app: string;
  title: string;
  text: string;
  lines: Array<{ text: string; confidence: number }>;
  screenshot: string;
};

const emptyArchive: ArchivePayload = { data_dir: "", days: [] };

async function invokeTauri<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(command, args);
}

async function listenTauri<T>(event: string, handler: (event: { payload: T }) => void): Promise<() => void> {
  const { listen } = await import("@tauri-apps/api/event");
  return listen<T>(event, handler);
}

function formatError(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [archive, setArchive] = useState<ArchivePayload>(emptyArchive);
  const [selectedDate, setSelectedDate] = useState<string>("");
  const [ocr, setOcr] = useState<OcrPayload | null>(null);
  const [watching, setWatching] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("Ready");
  const [showSettings, setShowSettings] = useState(false);
  const [allowFullScreenCapture, setAllowFullScreenCapture] = useState(
    window.localStorage.getItem("cronsnap.allowFullScreenCapture") === "true",
  );

  const selectedDay = useMemo(
    () => archive.days.find((day) => day.date === selectedDate) ?? archive.days[0],
    [archive.days, selectedDate],
  );

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, nextArchive] = await Promise.all([
        invokeTauri<Status>("engine_status"),
        invokeTauri<ArchivePayload>("engine_archive", { days: 21 }),
      ]);
      setStatus(nextStatus);
      setArchive(nextArchive);
      setSelectedDate((current) => current || nextArchive.days[0]?.date || "");
      setMessage("Refreshed");
    } catch (error) {
      setMessage(formatError(error));
    }
  }, []);

  useEffect(() => {
    refresh();
    const interval = window.setInterval(refresh, 30000);
    const unlisteners = [
      listenTauri("cronsnap://refresh", refresh),
      listenTauri("cronsnap://ocr", () => runOcr()),
      listenTauri("cronsnap://settings", () => setShowSettings(true)),
      listenTauri<boolean>("cronsnap://watching", (event) => setWatching(event.payload)),
    ];
    return () => {
      window.clearInterval(interval);
      unlisteners.forEach((promise) => promise.then((unlisten) => unlisten()));
    };
  }, [refresh]);

  async function runOcr() {
    setBusy(true);
    setMessage("Reading active window...");
    try {
      const result = await invokeTauri<OcrPayload>("engine_ocr", {
        allowFullScreenCapture,
      });
      setOcr(result);
      setMessage(result.text ? "OCR complete. Text was not saved." : "OCR complete. No text detected.");
    } catch (error) {
      setMessage(formatError(error));
    } finally {
      setBusy(false);
    }
  }

  async function generateReport(day: "today" | "yesterday") {
    setBusy(true);
    setMessage(`Generating ${day} report...`);
    try {
      await invokeTauri("engine_report", { day });
      await refresh();
      setMessage(`${day[0].toUpperCase()}${day.slice(1)} report exported`);
    } catch (error) {
      setMessage(formatError(error));
    } finally {
      setBusy(false);
    }
  }

  async function openDataDir() {
    try {
      await invokeTauri("open_data_dir");
    } catch (error) {
      setMessage(formatError(error));
    }
  }

  async function toggleWatch() {
    setBusy(true);
    try {
      if (watching) {
        await invokeTauri("stop_watcher");
        setWatching(false);
        setMessage("Watcher paused");
      } else {
        await invokeTauri("start_watcher");
        setWatching(true);
        setMessage("Watcher started");
      }
    } catch (error) {
      setMessage(formatError(error));
    } finally {
      setBusy(false);
    }
  }

  function updateFullScreenCapture(enabled: boolean) {
    setAllowFullScreenCapture(enabled);
    window.localStorage.setItem("cronsnap.allowFullScreenCapture", String(enabled));
    setMessage(enabled ? "Full-screen fallback enabled for OCR only" : "Full-screen fallback disabled");
  }

  return (
    <main className="app-shell">
      <section className="topbar">
        <div>
          <div className="eyebrow">CronSnap</div>
          <h1>Local activity archive</h1>
        </div>
        <div className={`status ${watching ? "on" : ""}`}>
          {watching ? "Watching" : "Paused"}
        </div>
      </section>

      <section className="toolbar">
        <button onClick={toggleWatch} disabled={busy}>
          {watching ? <Pause size={16} /> : <Play size={16} />} {watching ? "Pause" : "Start"}
        </button>
        <button onClick={runOcr} disabled={busy}>
          <Eye size={16} /> OCR Now
        </button>
        <button onClick={() => generateReport("today")} disabled={busy}>
          <FileText size={16} /> Today
        </button>
        <button onClick={() => generateReport("yesterday")} disabled={busy}>
          <CalendarDays size={16} /> Yesterday
        </button>
        <button onClick={refresh} disabled={busy}>
          <RefreshCw size={16} /> Refresh
        </button>
        <button onClick={openDataDir}>
          <FolderOpen size={16} /> Data
        </button>
        <button onClick={() => setShowSettings((open) => !open)}>
          <Settings size={16} /> Settings
        </button>
      </section>

      {showSettings && (
        <section className="settings-panel">
          <div>
            <div className="eyebrow">Privacy Settings</div>
            <h2>Capture policy</h2>
          </div>
          <label className="toggle-row">
            <input
              type="checkbox"
              checked={allowFullScreenCapture}
              onChange={(event) => updateFullScreenCapture(event.currentTarget.checked)}
            />
            <span>
              <strong>Allow full-screen fallback for OCR</strong>
              <small>Off by default. Active-window capture still fails closed unless this is enabled.</small>
            </span>
          </label>
          <div className="readonly-settings">
            <span>Screenshots are temporary</span>
            <span>OCR text is not saved</span>
            <span>Markdown reports are exports</span>
          </div>
        </section>
      )}

      <section className="notice">
        <Shield size={16} />
        <span>{message}</span>
      </section>

      <section className="grid">
        <aside className="sidebar">
          <div className="panel-title">
            <Archive size={16} /> Archive
          </div>
          <div className="day-list">
            {archive.days.map((day) => (
              <button
                className={day.date === selectedDay?.date ? "day active" : "day"}
                key={day.date}
                onClick={() => setSelectedDate(day.date)}
              >
                <span>{day.date}</span>
                <strong>{day.accounted}</strong>
              </button>
            ))}
            {!archive.days.length && <div className="empty">No logs in app data yet.</div>}
          </div>
        </aside>

        <section className="detail">
          {selectedDay ? <DayDetail day={selectedDay} /> : <EmptyState status={status} />}
          <OcrPanel ocr={ocr} />
        </section>
      </section>
    </main>
  );
}

function DayDetail({ day }: { day: DaySummary }) {
  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <div className="eyebrow">Day</div>
          <h2>{day.date_label}</h2>
        </div>
        <div className="metric">
          <span>{day.samples_included}/{day.samples_read}</span>
          <small>samples</small>
        </div>
      </div>
      <div className="metrics">
        <Metric label="Accounted" value={day.accounted} />
        <Metric label="Top app" value={day.apps[0]?.name ?? "None"} />
        <Metric label="Top activity" value={day.activities[0]?.name ?? "None"} />
      </div>
      <Bars title="Applications" rows={day.apps} />
      <Bars title="Activities" rows={day.activities} />
      <div className="section-title">Sessions</div>
      <div className="sessions">
        {day.sessions.slice(0, 12).map((session, index) => (
          <div className="session" key={`${session.start_time}-${index}`}>
            <span>{session.start_time}-{session.end_time}</span>
            <strong>{session.app}</strong>
            <em>{session.activity}</em>
            <small>{session.duration}</small>
          </div>
        ))}
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric-box">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Bars({ title, rows }: { title: string; rows: Row[] }) {
  const max = Math.max(...rows.map((row) => row.seconds), 1);
  return (
    <section>
      <div className="section-title">{title}</div>
      <div className="bars">
        {rows.slice(0, 8).map((row) => (
          <div className="bar-row" key={row.name}>
            <span>{row.name}</span>
            <div className="bar-track">
              <div className="bar-fill" style={{ width: `${(row.seconds / max) * 100}%` }} />
            </div>
            <strong>{row.duration}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}

function OcrPanel({ ocr }: { ocr: OcrPayload | null }) {
  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <div className="eyebrow">OCR</div>
          <h2>Active-window text</h2>
        </div>
        <div className="privacy-pill">not saved</div>
      </div>
      {ocr ? (
        <>
          <div className="ocr-meta">
            {ocr.app} {ocr.title ? `· ${ocr.title}` : ""}
          </div>
          <pre className="ocr-text">{ocr.text || "No text detected."}</pre>
        </>
      ) : (
        <div className="empty">Run OCR Now to read visible text once. The result stays in this window only.</div>
      )}
    </div>
  );
}

function EmptyState({ status }: { status: Status | null }) {
  return (
    <div className="panel empty-panel">
      <h2>No archive rows yet</h2>
      <p>Start the watcher or generate a report after logs exist.</p>
      <code>{status?.data_dir ?? "Loading app data path..."}</code>
    </div>
  );
}

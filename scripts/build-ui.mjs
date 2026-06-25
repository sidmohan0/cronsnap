import * as esbuild from "esbuild";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";

await rm("dist", { recursive: true, force: true });
await mkdir("dist/assets", { recursive: true });

await esbuild.build({
  entryPoints: ["src/main.tsx"],
  bundle: true,
  format: "iife",
  globalName: "CronSnapApp",
  outfile: "dist/assets/app.js",
  jsx: "automatic",
  loader: {
    ".css": "css",
  },
  minify: true,
  sourcemap: false,
  target: ["safari15"],
  logLevel: "info",
});

const css = await readFile("dist/assets/app.css", "utf8");
const js = await readFile("dist/assets/app.js", "utf8");

await writeFile(
  "dist/index.html",
  `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>CronSnap</title>
    <style>${css}</style>
  </head>
  <body>
    <div id="root">
      <div style="min-height:100vh;padding:24px;background:#0b0d10;color:#e8edf4;font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
        <div style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:#8792a2">CronSnap</div>
        <h1 style="margin:8px 0 0;font-size:24px">Starting local archive...</h1>
        <p id="boot-status" style="color:#aab5c5">Loading the app shell.</p>
      </div>
    </div>
    <script>
      window.addEventListener("error", function (event) {
        var status = document.getElementById("boot-status");
        if (status) status.textContent = "Startup error: " + event.message;
      });
      window.addEventListener("unhandledrejection", function (event) {
        var status = document.getElementById("boot-status");
        if (status) status.textContent = "Startup error: " + String(event.reason);
      });
    </script>
    <script>${js}</script>
  </body>
</html>
`,
  "utf8",
);

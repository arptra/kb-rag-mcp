#!/usr/bin/env node

/**
 * Export an HTML slide deck to a 16:9 PDF using a locally installed Chromium browser.
 *
 * Usage:
 *   node scripts/export-presentation-pdf.mjs [input.html] [output.pdf]
 *
 * Optional:
 *   PRESENTATION_CHROME_PATH=/path/to/chrome node scripts/export-presentation-pdf.mjs
 */

import { access, mkdir, mkdtemp, rm, stat } from "node:fs/promises";
import { constants } from "node:fs";
import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { tmpdir } from "node:os";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(scriptDirectory, "..");
const [inputArgument, outputArgument, ...extraArguments] = process.argv.slice(2);

if (inputArgument === "--help" || inputArgument === "-h" || extraArguments.length > 0) {
  console.log("Usage: node scripts/export-presentation-pdf.mjs [input.html] [output.pdf]");
  console.log("Set PRESENTATION_CHROME_PATH when Chrome/Chromium is in a non-standard location.");
  process.exit(inputArgument ? 0 : 1);
}

const sourcePath = resolve(
  repositoryRoot,
  inputArgument ?? "docs/corporate-rag-presentation.html",
);
const outputPath = resolve(
  repositoryRoot,
  outputArgument ?? "output/pdf/corporate-rag-presentation.pdf",
);

const browserPath = await resolveBrowserPath();
await assertReadableFile(sourcePath, "Input HTML");
await mkdir(dirname(outputPath), { recursive: true });

const profileDirectory = await mkdtemp(`${tmpdir()}/presentation-pdf-`);
try {
  await exportPdf(browserPath, [
    "--headless=new",
    "--disable-gpu",
    "--disable-extensions",
    "--no-pdf-header-footer",
    "--run-all-compositor-stages-before-draw",
    "--virtual-time-budget=1000",
    `--user-data-dir=${profileDirectory}`,
    `--print-to-pdf=${outputPath}`,
    pathToFileURL(sourcePath).href,
  ], outputPath);
} finally {
  await rm(profileDirectory, { force: true, recursive: true });
}

const output = await stat(outputPath);
if (output.size === 0) {
  throw new Error(`Browser created an empty PDF: ${outputPath}`);
}

console.log(`PDF created: ${outputPath}`);
console.log(`Size: ${(output.size / 1024 / 1024).toFixed(2)} MiB`);

async function resolveBrowserPath() {
  const configured = process.env.PRESENTATION_CHROME_PATH;
  const candidates = [
    configured,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  ].filter((candidate) => candidate);

  for (const candidate of candidates) {
    try {
      await access(candidate, constants.X_OK);
      return candidate;
    } catch {
      // Try the next standard browser location.
    }
  }

  throw new Error(
    "Chrome or Chromium was not found. Install one or set PRESENTATION_CHROME_PATH to its executable.",
  );
}

async function assertReadableFile(path, label) {
  try {
    await access(path, constants.R_OK);
  } catch {
    throw new Error(`${label} does not exist or cannot be read: ${path}`);
  }
}

async function exportPdf(command, argumentsList, expectedOutputPath) {
  const child = spawn(command, argumentsList, {
    detached: process.platform !== "win32",
    stdio: "ignore",
  });
  const exited = new Promise((resolvePromise, reject) => {
    child.once("error", reject);
    child.once("exit", (code, signal) => resolvePromise({ code, signal }));
  });
  const deadline = Date.now() + 60_000;
  let previousSize = -1;
  let stableChecks = 0;

  while (Date.now() < deadline) {
    const exitState = await Promise.race([
      exited.then((state) => state),
      delay(250).then(() => null),
    ]);
    if (exitState && exitState.code !== 0) {
      throw new Error(
        `Browser export failed (exit=${exitState.code ?? "unknown"}, signal=${exitState.signal ?? "none"}).`,
      );
    }

    try {
      const output = await stat(expectedOutputPath);
      if (output.size > 0 && output.size === previousSize) {
        stableChecks += 1;
      } else {
        stableChecks = 0;
      }
      previousSize = output.size;
      if (stableChecks >= 2) {
        await stopBrowser(child, exited);
        return;
      }
    } catch {
      // Chrome has not created the output file yet.
    }
  }

  await stopBrowser(child, exited);
  throw new Error(`Timed out waiting for PDF output: ${expectedOutputPath}`);
}

async function stopBrowser(child, exited) {
  if (child.exitCode === null && child.pid) {
    try {
      if (process.platform === "win32") {
        child.kill("SIGTERM");
      } else {
        process.kill(-child.pid, "SIGTERM");
      }
    } catch {
      // The browser process already finished between the checks.
    }
  }
  await Promise.race([exited, delay(5_000)]);
}

function delay(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

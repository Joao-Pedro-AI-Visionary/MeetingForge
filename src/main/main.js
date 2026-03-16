const { app, BrowserWindow, ipcMain, dialog, shell } = require("electron");
const path = require("path");
const { spawn, execSync } = require("child_process");
const fs = require("fs");
const os = require("os");

let mainWindow;
let pythonProcess = null;

// ── Paths ──
const isDev = process.argv.includes("--dev");
const appPath = isDev ? __dirname.replace(/src[/\\]main$/, "") : path.join(process.resourcesPath, "..");
const pythonDir = isDev
  ? path.join(appPath, "python")
  : path.join(process.resourcesPath, "python");
const outputDir = path.join(os.homedir(), "MeetingForge");

// Ensure output directory exists
if (!fs.existsSync(outputDir)) {
  fs.mkdirSync(outputDir, { recursive: true });
}

// ── Find Python (prefer venv) ──
function findPython() {
  // Prefer the project's virtualenv Python
  const venvPython = path.join(appPath, "venv", "bin", "python3");
  if (fs.existsSync(venvPython)) {
    try {
      execSync(`"${venvPython}" --version`, { stdio: "pipe" });
      return venvPython;
    } catch {}
  }

  const candidates = ["python3", "python"];
  for (const cmd of candidates) {
    try {
      execSync(`${cmd} --version`, { stdio: "pipe" });
      return cmd;
    } catch {}
  }
  return null;
}

// ── Find FFmpeg ──
function findFFmpeg() {
  try {
    execSync("ffmpeg -version", { stdio: "pipe" });
    return true;
  } catch {
    return false;
  }
}

// ── Get file duration via ffprobe ──
function getFileDuration(filePath) {
  try {
    const result = execSync(
      `ffprobe -v quiet -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "${filePath}"`,
      { stdio: ["pipe", "pipe", "pipe"], encoding: "utf-8", timeout: 15000 }
    );
    return parseFloat(result.trim());
  } catch {
    return null;
  }
}

// ── Check dependencies ──
function checkDependencies() {
  const python = findPython();
  const ffmpeg = findFFmpeg();
  const issues = [];

  if (!python) issues.push("python");
  if (!ffmpeg) issues.push("ffmpeg");

  // Check Python packages
  if (python) {
    const packages = ["whisper", "yt_dlp"];
    for (const pkg of packages) {
      try {
        execSync(`${python} -c "import ${pkg}"`, { stdio: "pipe" });
      } catch {
        issues.push(pkg === "whisper" ? "openai-whisper" : "yt-dlp");
      }
    }
  }

  return { python, ffmpeg, issues };
}

// ── Create Window ──
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 900,
    minHeight: 650,
    title: "MeetingForge",
    backgroundColor: "#0d0d1a",
    titleBarStyle: "hiddenInset",
    frame: process.platform !== "darwin",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  mainWindow.loadFile(path.join(__dirname, "..", "renderer", "index.html"));

  if (isDev) {
    mainWindow.webContents.openDevTools();
  }

  mainWindow.on("closed", () => {
    mainWindow = null;
    if (pythonProcess) {
      pythonProcess.kill();
      pythonProcess = null;
    }
  });
}

app.whenReady().then(createWindow);

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

// ── IPC Handlers ──

// Check system dependencies
ipcMain.handle("check-deps", async () => {
  return checkDependencies();
});

// Install Python dependencies (creates venv if needed)
ipcMain.handle("install-deps", async (event) => {
  const systemPython = (() => {
    for (const cmd of ["python3", "python"]) {
      try { execSync(`${cmd} --version`, { stdio: "pipe" }); return cmd; } catch {}
    }
    return null;
  })();
  if (!systemPython) return { success: false, error: "Python não encontrado" };

  // Create venv if it doesn't exist
  const venvDir = path.join(appPath, "venv");
  if (!fs.existsSync(path.join(venvDir, "bin", "python3"))) {
    try {
      execSync(`${systemPython} -m venv "${venvDir}"`, { stdio: "pipe" });
    } catch (e) {
      return { success: false, error: "Falha ao criar virtualenv: " + e.message };
    }
  }

  const venvPip = path.join(venvDir, "bin", "pip");
  return new Promise((resolve) => {
    const proc = spawn(venvPip, ["install", "openai-whisper", "yt-dlp", "anthropic"], {
      stdio: ["pipe", "pipe", "pipe"],
    });

    let output = "";
    proc.stdout.on("data", (d) => {
      output += d.toString();
      mainWindow?.webContents.send("install-progress", d.toString());
    });
    proc.stderr.on("data", (d) => {
      output += d.toString();
      mainWindow?.webContents.send("install-progress", d.toString());
    });
    proc.on("close", (code) => {
      resolve({ success: code === 0, output });
    });
  });
});

// Select file dialog
ipcMain.handle("select-file", async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    title: "Selecionar gravação da reunião",
    filters: [
      { name: "Mídia", extensions: ["mp4", "mp3", "wav", "webm", "m4a", "ogg", "mkv", "avi", "mov"] },
      { name: "Todos os Arquivos", extensions: ["*"] },
    ],
    properties: ["openFile"],
  });

  if (result.canceled) return null;
  const filePath = result.filePaths[0];
  const stats = fs.statSync(filePath);
  const duration = getFileDuration(filePath);
  return {
    path: filePath,
    name: path.basename(filePath),
    size: stats.size,
    duration: duration,
  };
});

// Process meeting (main pipeline)
ipcMain.handle("process-meeting", async (event, { filePath, url, model, useAI, apiKey }) => {
  const python = findPython();
  if (!python) return { success: false, error: "Python não encontrado no sistema." };

  const scriptPath = path.join(pythonDir, "processor.py");

  // Build arguments
  const args = ["-u", scriptPath]; // -u para unbuffered output
  if (url) {
    args.push("--url", url);
  } else {
    args.push("--file", filePath);
  }
  args.push("--model", model || "base");
  args.push("--output", outputDir);
  args.push("--json-output");

  if (!useAI) {
    args.push("--no-ai");
  }

  // Set environment
  const env = { ...process.env };
  if (apiKey) {
    env.ANTHROPIC_API_KEY = apiKey;
  }

  // Prevent app from sleeping during long processing
  const { powerSaveBlocker } = require("electron");
  const blockerId = powerSaveBlocker.start("prevent-app-suspension");

  return new Promise((resolve) => {
    pythonProcess = spawn(python, args, {
      env,
      stdio: ["pipe", "pipe", "pipe"],
      // No timeout - long files can take hours
    });

    let stdout = "";
    let stderr = "";

    pythonProcess.stdout.on("data", (data) => {
      const text = data.toString();
      stdout += text;

      // Parse progress messages (lines starting with PROGRESS:)
      const lines = text.split("\n");
      for (const line of lines) {
        if (line.startsWith("PROGRESS:")) {
          try {
            const progress = JSON.parse(line.replace("PROGRESS:", ""));
            mainWindow?.webContents.send("process-progress", progress);
          } catch {}
        }
      }
    });

    pythonProcess.stderr.on("data", (data) => {
      stderr += data.toString();
      mainWindow?.webContents.send("process-log", data.toString());
    });

    pythonProcess.on("close", (code) => {
      pythonProcess = null;
      powerSaveBlocker.stop(blockerId);

      if (code !== 0) {
        resolve({ success: false, error: stderr || "Processo falhou com código " + code });
        return;
      }

      // Try to parse the JSON result from stdout
      try {
        const jsonMatch = stdout.match(/RESULT_JSON_START(.+?)RESULT_JSON_END/s);
        if (jsonMatch) {
          const result = JSON.parse(jsonMatch[1].trim());
          resolve({ success: true, ...result });
        } else {
          resolve({ success: false, error: "Não foi possível ler o resultado do processamento." });
        }
      } catch (e) {
        resolve({ success: false, error: `Erro ao parsear resultado: ${e.message}` });
      }
    });
  });
});

// Cancel processing
ipcMain.handle("cancel-process", async () => {
  if (pythonProcess) {
    pythonProcess.kill("SIGTERM");
    pythonProcess = null;
    return true;
  }
  return false;
});

// Open output folder
ipcMain.handle("open-output-folder", async () => {
  shell.openPath(outputDir);
});

// Save settings
ipcMain.handle("save-settings", async (event, settings) => {
  const settingsPath = path.join(outputDir, "settings.json");
  fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2));
  return true;
});

// Load settings
ipcMain.handle("load-settings", async () => {
  const settingsPath = path.join(outputDir, "settings.json");
  try {
    return JSON.parse(fs.readFileSync(settingsPath, "utf-8"));
  } catch {
    return { model: "base", useAI: true, apiKey: "" };
  }
});

// Get app version
ipcMain.handle("get-version", () => app.getVersion());

// Copy to clipboard
ipcMain.handle("copy-to-clipboard", (event, text) => {
  const { clipboard } = require("electron");
  clipboard.writeText(text);
  return true;
});

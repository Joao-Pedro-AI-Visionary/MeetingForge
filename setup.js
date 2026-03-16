#!/usr/bin/env node
/**
 * MeetingForge - Script de Setup
 * Verifica e instala dependências automaticamente.
 * Execute: node setup.js
 */

const { execSync, spawnSync } = require("child_process");
const os = require("os");

const RESET = "\x1b[0m";
const BOLD = "\x1b[1m";
const GREEN = "\x1b[32m";
const RED = "\x1b[31m";
const YELLOW = "\x1b[33m";
const CYAN = "\x1b[36m";
const DIM = "\x1b[2m";

function log(msg) { console.log(msg); }
function ok(msg) { log(`  ${GREEN}✓${RESET} ${msg}`); }
function fail(msg) { log(`  ${RED}✗${RESET} ${msg}`); }
function warn(msg) { log(`  ${YELLOW}!${RESET} ${msg}`); }
function info(msg) { log(`  ${CYAN}→${RESET} ${msg}`); }

function check(cmd) {
  try { execSync(cmd, { stdio: "pipe" }); return true; }
  catch { return false; }
}

function findPython() {
  if (check("python3 --version")) return "python3";
  if (check("python --version")) return "python";
  return null;
}

log("");
log(`${BOLD}╔══════════════════════════════════════╗${RESET}`);
log(`${BOLD}║      ⚡ MeetingForge Setup           ║${RESET}`);
log(`${BOLD}╚══════════════════════════════════════╝${RESET}`);
log("");

// 1. Check Node.js
log(`${BOLD}Verificando dependências do sistema...${RESET}`);
log("");

const nodeVer = process.version;
ok(`Node.js ${nodeVer}`);

// 2. Check Python
const python = findPython();
if (python) {
  const ver = execSync(`${python} --version`, { encoding: "utf-8" }).trim();
  ok(ver);
} else {
  fail("Python não encontrado!");
  log("");
  log(`  Instale Python 3.9+ de: ${CYAN}https://python.org${RESET}`);
  if (os.platform() === "darwin") info("Ou: brew install python3");
  if (os.platform() === "win32") info("Ou: winget install Python.Python.3");
  if (os.platform() === "linux") info("Ou: sudo apt install python3 python3-pip");
  process.exit(1);
}

// 3. Check FFmpeg
if (check("ffmpeg -version")) {
  ok("FFmpeg instalado");
} else {
  fail("FFmpeg não encontrado!");
  log("");
  if (os.platform() === "darwin") info("Instale: brew install ffmpeg");
  else if (os.platform() === "win32") info("Instale: choco install ffmpeg");
  else info("Instale: sudo apt install ffmpeg");
  log("");
  warn("FFmpeg é necessário para processar áudio/vídeo.");
}

// 4. Install Python packages
log("");
log(`${BOLD}Instalando pacotes Python...${RESET}`);
log("");

const packages = [
  ["openai-whisper", "whisper"],
  ["yt-dlp", "yt_dlp"],
  ["anthropic", "anthropic"],
];

for (const [pipName, importName] of packages) {
  if (check(`${python} -c "import ${importName}"`)) {
    ok(`${pipName} já instalado`);
  } else {
    info(`Instalando ${pipName}...`);
    const result = spawnSync(python, ["-m", "pip", "install", pipName, "--break-system-packages"], {
      stdio: "inherit",
    });
    if (result.status === 0) {
      ok(`${pipName} instalado com sucesso`);
    } else {
      fail(`Falha ao instalar ${pipName}`);
      warn(`Tente manualmente: ${python} -m pip install ${pipName}`);
    }
  }
}

// 5. Install Node packages
log("");
log(`${BOLD}Instalando pacotes Node.js...${RESET}`);
log("");
info("npm install...");
const npmResult = spawnSync("npm", ["install"], { stdio: "inherit", shell: true });
if (npmResult.status === 0) {
  ok("Pacotes Node.js instalados");
} else {
  fail("Falha no npm install");
}

// Done
log("");
log(`${BOLD}${GREEN}══════════════════════════════════════${RESET}`);
log(`${BOLD}${GREEN}  ✅ Setup concluído!${RESET}`);
log(`${BOLD}${GREEN}══════════════════════════════════════${RESET}`);
log("");
log(`  Para iniciar o app:`);
log(`  ${CYAN}npm start${RESET}`);
log("");
log(`  Para gerar o instalador:`);
log(`  ${CYAN}npm run build:win${RESET}   (Windows .exe)`);
log(`  ${CYAN}npm run build:mac${RESET}   (macOS .dmg)`);
log(`  ${CYAN}npm run build:linux${RESET} (Linux .AppImage)`);
log("");

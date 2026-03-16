const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("api", {
  // Dependencies
  checkDeps: () => ipcRenderer.invoke("check-deps"),
  installDeps: () => ipcRenderer.invoke("install-deps"),

  // File selection
  selectFile: () => ipcRenderer.invoke("select-file"),

  // Processing
  processMeeting: (opts) => ipcRenderer.invoke("process-meeting", opts),
  cancelProcess: () => ipcRenderer.invoke("cancel-process"),

  // Events from main process
  onProcessProgress: (cb) => {
    ipcRenderer.on("process-progress", (_, data) => cb(data));
  },
  onProcessLog: (cb) => {
    ipcRenderer.on("process-log", (_, data) => cb(data));
  },
  onInstallProgress: (cb) => {
    ipcRenderer.on("install-progress", (_, data) => cb(data));
  },

  // Utils
  openOutputFolder: () => ipcRenderer.invoke("open-output-folder"),
  saveSettings: (s) => ipcRenderer.invoke("save-settings", s),
  loadSettings: () => ipcRenderer.invoke("load-settings"),
  getVersion: () => ipcRenderer.invoke("get-version"),
  copyToClipboard: (text) => ipcRenderer.invoke("copy-to-clipboard", text),
});

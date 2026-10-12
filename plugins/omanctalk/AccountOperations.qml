import QtQuick
import Quickshell.Io

// Login Flow v2: credentials only travel over helper stdin/stdout, never argv.
Item {
  id: root
  required property string helper
  required property var accounts
  property string statusMessage: ""
  property string linkPhase: "idle"
  property string linkTarget: ""
  property string pollToken: ""
  property string pollEndpoint: ""
  property double deadline: 0
  property int generation: 0
  readonly property bool linkBusy: linkPhase !== "idle"
  readonly property bool busy: linkBusy
  signal refreshRequested()

  function parseJson(raw) {
    try { return JSON.parse(String(raw || "{}")) } catch (error) { return null }
  }
  function linkAccount(server) {
    var value = String(server || "").trim().replace(/\/+$/, "")
    if (busy || startProcess.running || finishProcess.running) return false
    if (!/^https:\/\/[^\s/?#]+(?:\/[^\s?#]*)?$/.test(value)) {
      statusMessage = "Enter an HTTPS Nextcloud server URL."
      return false
    }
    generation += 1
    linkTarget = value
    deadline = Date.now() + 20 * 60 * 1000
    linkPhase = "starting"
    statusMessage = "Opening browser login…"
    startProcess.generation = generation
    startProcess.payload = JSON.stringify({ server: value })
    startProcess.stdinEnabled = true
    startProcess.running = true
    watchdog.interval = 20 * 60 * 1000
    watchdog.restart()
    return true
  }
  function cancelLogin(message) {
    generation += 1
    pollTimer.stop()
    watchdog.stop()
    startProcess.running = false
    finishProcess.running = false
    linkPhase = "idle"
    pollToken = ""
    pollEndpoint = ""
    linkTarget = ""
    statusMessage = message || "Login cancelled."
  }
  function pollLogin() {
    if (!linkBusy || finishProcess.running) return
    if (Date.now() >= deadline) { cancelLogin("Login timed out. Please try again."); return }
    finishProcess.generation = generation
    finishProcess.payload = JSON.stringify({ server: linkTarget,
      poll_token: pollToken, poll_endpoint: pollEndpoint })
    finishProcess.stdinEnabled = true
    finishProcess.running = true
  }
  Timer { id: pollTimer; interval: 2000; onTriggered: root.pollLogin() }
  Timer {
    id: watchdog
    interval: 20 * 60 * 1000
    onTriggered: root.cancelLogin("Login timed out. Please try again.")
  }
  Process {
    id: browser
    command: []
    onExited: function(code) {
      if (code !== 0 && root.linkBusy) root.cancelLogin("The browser could not be opened.")
    }
  }
  Process {
    id: startProcess
    property int generation: 0
    property string payload: ""
    command: [root.helper, "login-start"]
    stdinEnabled: true
    stdout: StdioCollector { id: startOutput }
    onStarted: { write(payload + "\n"); payload = ""; stdinEnabled = false }
    onExited: function(code) {
      if (generation !== root.generation || !root.linkBusy) return
      var response = root.parseJson(startOutput.text)
      if (code !== 0 || !response || !response.ok
          || !/^https:\/\//.test(String(response.login_url || ""))
          || !response.poll_token || !response.poll_endpoint) {
        root.cancelLogin("Login could not be started. Check the server URL.")
        return
      }
      root.pollToken = String(response.poll_token)
      root.pollEndpoint = String(response.poll_endpoint)
      root.linkPhase = "waiting"
      root.statusMessage = "Complete the Nextcloud login in your browser."
      browser.command = ["/usr/bin/xdg-open", String(response.login_url)]
      browser.running = true
      watchdog.interval = Math.max(1, root.deadline - Date.now())
      watchdog.restart()
      pollTimer.restart()
    }
  }
  Process {
    id: finishProcess
    property int generation: 0
    property string payload: ""
    command: [root.helper, "login-finish"]
    stdinEnabled: true
    stdout: StdioCollector { id: finishOutput }
    onStarted: { write(payload + "\n"); payload = ""; stdinEnabled = false }
    onExited: function(code) {
      if (generation !== root.generation || !root.linkBusy) return
      var response = root.parseJson(finishOutput.text)
      if (response && response.pending === true) { pollTimer.restart(); return }
      if (code === 0 && response && response.ok === true) {
        root.cancelLogin("Account connected.")
        root.refreshRequested()
      } else root.cancelLogin("Login failed. Please start again.")
    }
  }
  Component.onDestruction: { pollTimer.stop(); watchdog.stop() }
}

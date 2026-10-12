import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui

// Directory search followed by an explicit first-message step.
Popup {
  id: root
  objectName: "newChatDialog"
  property var service: null
  property bool demoMode: false
  property var demoPeople: []
  property string accountLabel: ""
  property color foreground: Color.foreground
  property color surface: Color.background
  property color accent: Color.accent
  property color muted: foreground
  property color urgent: "#e06c75"
  property string fontFamily: Style.font.family
  property bool showAvatars: true
  property var chats: []
  property string step: "search"
  property string query: ""
  property var person: null
  property bool sending: false
  property string sendError: ""
  readonly property var people: demoMode ? demoPeople.filter(function(item) {
    return String(item.name || item.jid || "").toLowerCase().indexOf(query.toLowerCase()) >= 0
  }) : (service && Array.isArray(service.newChatPeople) ? service.newChatPeople : [])
  signal openChatRequested(string jid)
  signal groupRequested(string kind)
  signal chatStarted(string jid)
  parent: Overlay.overlay
  anchors.centerIn: parent
  width: Math.min(Style.space(430), (parent ? parent.width : Style.space(460)) - Style.space(28))
  height: Math.min(Style.space(520), (parent ? parent.height : Style.space(560)) - Style.space(36))
  padding: Style.space(14)
  modal: true
  focus: true
  closePolicy: sending ? Popup.NoAutoClose : Popup.CloseOnEscape | Popup.CloseOnPressOutside
  onOpened: {
    step = "search"; person = null; sending = false; sendError = ""
    searchField.text = ""; query = ""
    if (!demoMode && service) service.searchPeople("")
    Qt.callLater(function() { searchField.forceActiveFocus() })
  }
  onClosed: { person = null; sending = false; sendError = "" }
  onQueryChanged: searchDelay.restart()
  function typeQuery(text) { searchField.text = String(text || ""); searchField.forceActiveFocus() }
  function label(item) { return String(item && (item.name || item.jid) || "Talk user") }
  function choose(item) {
    if (!item || !/^users\/.+/.test(String(item.jid || ""))) return false
    person = item
    step = "compose"
    firstMessage.text = ""
    Qt.callLater(function() { firstMessage.forceActiveFocus() })
    return true
  }
  function sendFirst() {
    var text = firstMessage.text.trim()
    if (sending || !person || text === "") return false
    if (demoMode) { chatStarted(person.jid); close(); return true }
    if (!service || !service.startNewChat(person.jid, text, "app")) {
      sendError = service && service.errorText || "The request could not be started."
      return false
    }
    sending = true
    return true
  }
  Timer {
    id: searchDelay
    interval: 300
    onTriggered: if (root.opened && !root.demoMode && root.service) root.service.searchPeople(root.query)
  }
  Connections {
    target: root.service
    ignoreUnknownSignals: true
    function onWriteCompleted(kind, chatRef, request, owner) {
      if (kind !== "send-new" || !root.sending || !root.person
          || String(chatRef.jid || "") !== String(root.person.jid)) return
      root.sending = false
      root.chatStarted(String(root.service.lastStartedChatJid || ""))
      root.close()
    }
    function onWriteFailed(message, chatRef, details, owner) {
      if (!root.sending || !details || details.kind !== "send-new") return
      root.sending = false
      root.sendError = String(message || "The message could not be sent.")
    }
  }
  background: Rectangle {
    radius: Style.cornerRadius
    color: root.surface
    border.width: 1
    border.color: root.muted
  }
  contentItem: Column {
    spacing: Style.space(12)
    Text {
      textFormat: Text.PlainText
      text: root.step === "compose" ? "Message " + root.label(root.person) : "New Talk chat · " + root.accountLabel
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
    }
    Row {
      visible: root.step === "search"
      spacing: Style.space(8)
      Button { text: "Create room"; foreground: root.foreground; accent: root.accent; fontFamily: root.fontFamily
        onClicked: { root.close(); root.groupRequested("create") } }
      Button { text: "Join room"; foreground: root.foreground; accent: root.accent; fontFamily: root.fontFamily
        onClicked: { root.close(); root.groupRequested("join") } }
    }
    TextField {
      id: searchField
      visible: root.step === "search"
      width: parent.width
      placeholderText: "Search by display name or user ID"
      foreground: root.foreground
      accent: root.accent
      font.family: root.fontFamily
      onTextChanged: root.query = text.trim()
      Keys.onReturnPressed: if (root.people.length) root.choose(root.people[0])
    }
    ListView {
      visible: root.step === "search"
      width: parent.width
      height: Style.space(300)
      clip: true
      model: root.people
      delegate: Item {
        required property var modelData
        width: ListView.view.width
        height: Style.space(48)
        ChatAvatar {
          id: avatar
          width: Style.space(34); height: width
          anchors.verticalCenter: parent.verticalCenter
          chat: ({ name: root.label(modelData), kind: "dm", avatar_path: "" })
          showPhoto: root.showAvatars
          foreground: root.foreground; background: root.surface; accent: root.accent; fontFamily: root.fontFamily
        }
        Text {
          anchors.left: avatar.right; anchors.leftMargin: Style.space(10)
          anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
          textFormat: Text.PlainText; text: root.label(modelData); elide: Text.ElideRight
          color: root.foreground; font.family: root.fontFamily; font.pixelSize: Style.font.bodySmall
        }
        TapHandler { onTapped: root.choose(modelData) }
      }
      Text {
        anchors.centerIn: parent
        visible: root.people.length === 0
        textFormat: Text.PlainText
        text: root.service && root.service.newChatPeopleLoading ? "Searching…" : "No matching users"
        color: root.muted; font.family: root.fontFamily; font.pixelSize: Style.font.caption
      }
    }
    TextArea {
      id: firstMessage
      visible: root.step === "compose"
      width: parent.width; height: Style.space(200)
      enabled: !root.sending
      placeholderText: "First message"
      textFormat: TextEdit.PlainText
      wrapMode: TextEdit.Wrap
      color: root.foreground
      font.family: root.fontFamily
    }
    Text {
      width: parent.width
      visible: root.sendError !== ""
      textFormat: Text.PlainText; text: root.sendError; wrapMode: Text.Wrap
      color: root.urgent; font.family: root.fontFamily; font.pixelSize: Style.font.caption
    }
    Row {
      spacing: Style.space(8)
      Button { text: "Cancel"; enabled: !root.sending; foreground: root.foreground; accent: root.accent; fontFamily: root.fontFamily
        onClicked: root.close() }
      Button {
        visible: root.step === "compose"
        text: root.sending ? "Sending…" : "Send"
        enabled: !root.sending && firstMessage.text.trim() !== ""
        foreground: root.foreground; accent: root.accent; fontFamily: root.fontFamily
        onClicked: root.sendFirst()
      }
    }
  }
}

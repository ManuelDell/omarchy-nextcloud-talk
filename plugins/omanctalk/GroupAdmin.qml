import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui

// Talk room moderation; participant roles are managed in ChatDetailsPanel.
Column {
  id: root
  objectName: "groupAdmin"
  property var service: null
  property string localRole: ""
  property color foreground: Color.foreground
  property color surface: Color.background
  property color accent: Color.accent
  property color muted: foreground
  property color urgent: "#e06c75"
  property string fontFamily: Style.font.family
  readonly property var settings: service && service.groupSettings || ({})
  readonly property bool loaded: settings.ok === true
  readonly property string role: String(settings.my_role || localRole)
  readonly property bool admin: role === "owner" || role === "moderator"
  property string armed: ""
  signal copyRequested(string text)
  spacing: Style.space(10)
  width: parent ? parent.width : Style.space(300)
  function act(action, value) { return !!service && service.groupAction(action, value, "app") }
  function leave() {
    if (armed !== "leave") { armed = "leave"; disarm.restart(); return false }
    armed = ""
    return act("leave")
  }
  Timer { id: disarm; interval: 4000; onTriggered: root.armed = "" }
  Button {
    visible: !root.loaded
    text: root.service && root.service.groupSettingsLoading ? "Loading…" : "Load room settings"
    enabled: !!root.service && !root.service.groupSettingsLoading
    foreground: root.foreground; accent: root.accent; fontFamily: root.fontFamily; bordered: true
    onClicked: root.service.loadGroupSettings()
  }
  Text {
    width: parent.width
    textFormat: Text.PlainText
    wrapMode: Text.Wrap
    text: root.service && root.service.groupSettingsError || ""
    visible: text !== ""
    color: root.urgent; font.family: root.fontFamily; font.pixelSize: Style.font.caption
  }
  Column {
    visible: root.loaded && root.admin
    width: parent.width
    spacing: Style.space(8)
    TextField {
      id: nameField
      width: parent.width
      text: String(root.settings.name || "")
      placeholderText: "Room name"
      foreground: root.foreground; accent: root.accent; font.family: root.fontFamily
    }
    Button {
      text: "Save name"
      enabled: nameField.text.trim() !== "" && nameField.text.trim() !== String(root.settings.name || "")
      foreground: root.foreground; accent: root.accent; fontFamily: root.fontFamily; bordered: true
      onClicked: root.act("rename", nameField.text.trim())
    }
    TextArea {
      id: descriptionField
      width: parent.width; height: Style.space(90)
      text: String(root.settings.description || "")
      textFormat: TextEdit.PlainText
      placeholderText: "Description"
      wrapMode: TextEdit.Wrap
      color: root.foreground; font.family: root.fontFamily
    }
    Button {
      text: "Save description"
      enabled: descriptionField.text !== String(root.settings.description || "")
      foreground: root.foreground; accent: root.accent; fontFamily: root.fontFamily; bordered: true
      onClicked: root.act("description", descriptionField.text)
    }
    TextField {
      id: addField
      width: parent.width
      placeholderText: "Nextcloud user ID"
      foreground: root.foreground; accent: root.accent; font.family: root.fontFamily
    }
    Button {
      text: "Add participant"
      enabled: addField.text.trim() !== ""
      foreground: root.foreground; accent: root.accent; fontFamily: root.fontFamily; bordered: true
      onClicked: if (root.act("add", ["users/" + addField.text.trim().replace(/^users\//, "")])) addField.text = ""
    }
  }
  Button {
    text: root.armed === "leave" ? "Confirm: leave room" : "Leave room"
    foreground: root.urgent; accent: root.urgent; fontFamily: root.fontFamily; bordered: true
    onClicked: root.leave()
  }
}

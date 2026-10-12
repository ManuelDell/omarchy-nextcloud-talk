import QtQuick
import qs.Commons
import qs.Ui

Rectangle {
  id: root
  property var service: null
  property bool demoMode: false
  property color foreground: Color.foreground
  property color background: Color.background
  property color accent: Color.accent
  property color dim: foreground
  property string fontFamily: Style.font.family
  readonly property var operations: service ? service.accountOperations : null
  readonly property bool linking: !!operations && operations.linkBusy
  color: background
  MouseArea { anchors.fill: parent }
  Column {
    anchors.centerIn: parent
    width: Math.min(Style.space(520), parent.width - Style.space(48))
    spacing: Style.space(18)
    Text {
      textFormat: Text.PlainText
      text: "Welcome to OmaNCTalk"
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.heading
    }
    Text {
      width: parent.width
      textFormat: Text.PlainText
      wrapMode: Text.Wrap
      text: "Nextcloud Talk on your desktop. Chats open instantly from a private local mirror and remain searchable offline."
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
    }
    TextField {
      id: serverField
      width: parent.width
      placeholderText: "https://cloud.example.org"
      enabled: !root.linking
      foreground: root.foreground
      accent: root.accent
      font.family: root.fontFamily
      Keys.onReturnPressed: if (root.operations && !root.linking) root.operations.linkAccount(text)
    }
    Row {
      spacing: Style.space(10)
      Button {
        text: root.linking ? "Waiting for browser…" : "Im Browser anmelden"
        enabled: !root.demoMode && !!root.operations && !root.operations.busy
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        bordered: true
        onClicked: root.operations.linkAccount(serverField.text)
      }
      Button {
        visible: root.linking
        text: "Cancel"
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        bordered: true
        onClicked: root.operations.cancelLogin()
      }
    }
    Text {
      width: parent.width
      textFormat: Text.PlainText
      wrapMode: Text.Wrap
      text: root.operations ? root.operations.statusMessage : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
    Text {
      width: parent.width
      textFormat: Text.PlainText
      wrapMode: Text.Wrap
      text: "Sign in on your Nextcloud server in the browser and grant access. Login expires after 20 minutes. Messages and media are stored on this computer; sending uses your own Nextcloud server."
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }
}

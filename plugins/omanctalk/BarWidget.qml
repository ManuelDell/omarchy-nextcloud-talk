import QtQuick
import QtQuick.Shapes
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// The OmaNCTalk bar icon.
//
// NOTE (replacement-bar bridge): a cloned/replacement bar is deliberately not
// handed third-party service objects (`bar.shell.serviceFor` stays null), so
// this widget cannot receive the live service instance there. It therefore
// talks to its own service over IPC: it polls a compact `overview` for the
// badge and tooltip, and opens the full app on click. The compact bar
// drop-down mini client requires the stock bar and is skipped here.
BarWidget {
  id: root

  readonly property string pluginId: "io.github.manueldell.omanctalk"

  // ----------------------------------------------------- IPC-polled state
  property int notificationUnreadCount: 0
  property bool railReady: false
  property bool notificationsMuted: false
  property bool closed: false
  property bool showUnreadCount: true
  property string barTooltip: "OmaNCTalk · reconnecting"

  readonly property bool available: railReady
  readonly property int unreadCount: notificationUnreadCount

  function refresh() {
    if (overviewProcess.running) return
    overviewProcess.running = true
  }

  Process {
    id: overviewProcess
    command: ["omarchy-shell", root.pluginId, "overview"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var d = JSON.parse(text)
          if (!d || d.ok !== true) return
          root.notificationUnreadCount = Number(d.unreadCount) || 0
          root.railReady = d.railReady === true
          root.notificationsMuted = d.muted === true
          root.closed = d.closed === true
          root.showUnreadCount = d.showUnreadCount !== false
          root.barTooltip = String(d.tooltip || "OmaNCTalk · reconnecting")
        } catch (e) {}
      }
    }
  }

  Timer {
    interval: 4000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // ------------------------------------------------------------- actions
  function launchApp() {
    Quickshell.execDetached(["omarchy-shell", root.pluginId, "toggleApp", "{}"])
  }

  function dismissNotifications() {
    Quickshell.execDetached(["omarchy-shell", root.pluginId, "dismiss", "{}"])
  }

  function toggleMute() {
    Quickshell.execDetached(["omarchy-shell", root.pluginId, "mute", "{}"])
  }

  // -------------------------------------------------------------- visuals
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // The Nextcloud Talk logo has no glyph in the bar font, so it is drawn as an outline:
  // transparent inside, thin lines in the bar's foreground colour like the other icons.
  // Icon and count sit in their own row, so the gap between them is fixed and small and
  // the button is exactly as wide as its content.
  readonly property real iconSize: Math.round(button.fontSize * 1.35)
  readonly property string countText: root.available && root.showUnreadCount && root.unreadCount > 0
    ? String(root.unreadCount > 99 ? "99+" : root.unreadCount) : ""
  // Muted shows a crossed bell next to the count; a vertical bar has room for one
  // glyph, so the bell replaces the logo there.
  readonly property bool bellOnly: root.vertical && root.notificationsMuted
  readonly property string labelText: root.vertical
    ? (root.notificationsMuted ? "󰂛" : "")
    : countText + (root.notificationsMuted ? (countText !== "" ? " " : "") + "󰂛" : "")

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // The row below draws the content; a non-empty text keeps the button visible.
    text: " "
    labelVisible: false
    fixedWidth: root.vertical ? -1 : Math.ceil(contentRow.implicitWidth + scaledHorizontalMargin * 2)
    active: root.available && root.unreadCount > 0
    // Closed: grey until it opens again.
    dimmed: root.notificationsMuted || root.closed
    horizontalMargin: 8
    tooltipText: root.barTooltip

    onPressed: function(code) {
      if (code === Qt.MiddleButton) root.dismissNotifications()
      else if (code === Qt.RightButton) root.toggleMute()
      else root.launchApp()
    }

    readonly property color contentColor: button.active && button.useActiveColor
      ? button.activeColor : button.foreground

    Row {
      id: contentRow
      anchors.centerIn: parent
      spacing: Math.round(button.fontSize * 0.4)

      Shape {
        id: talkIcon
        objectName: "talkOutlineIcon"
        visible: !root.bellOnly
        width: visible ? root.iconSize : 0
        height: root.iconSize
        anchors.verticalCenter: parent.verticalCenter
        preferredRendererType: Shape.CurveRenderer
        // 32-unit logo space scaled to the icon; the stroke is about 1 px at bar size.
        transform: Scale { xScale: root.iconSize / 32; yScale: root.iconSize / 32 }

        // Speech bubble with the tail at the bottom right.
        ShapePath {
          strokeColor: button.contentColor
          strokeWidth: 1.9
          fillColor: "transparent"
          capStyle: ShapePath.RoundCap
          joinStyle: ShapePath.RoundJoin
          PathSvg {
            path: "M16 4.6 A11.4 11.4 0 0 0 4.6 16 A11.4 11.4 0 0 0 16 27.4 A11.4 11.4 0 0 0 21.9 25.7 C23.3 26.3 26.5 27.9 27.3 27.2 C28.1 26.5 26.3 22.9 26 21.6 A11.4 11.4 0 0 0 27.4 16 A11.4 11.4 0 0 0 16 4.6 Z"
          }
        }
        // The ring inside the bubble.
        ShapePath {
          strokeColor: button.contentColor
          strokeWidth: 1.9
          fillColor: "transparent"
          PathSvg {
            path: "M16 10.4 A5.6 5.6 0 0 1 21.6 16 A5.6 5.6 0 0 1 16 21.6 A5.6 5.6 0 0 1 10.4 16 A5.6 5.6 0 0 1 16 10.4 Z"
          }
        }
      }

      Text {
        objectName: "barCountText"
        visible: root.labelText !== ""
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: root.labelText
        color: button.contentColor
        font.family: button.fontFamily
        font.pixelSize: button.fontSize
        renderType: Text.NativeRendering
      }
    }
  }
}

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Explicit updates from GitHub Releases. Checking contacts GitHub and downloads
// nothing; the AppImage is replaced only after Download and install, once the
// download has been verified. The replaced version is kept for switching back.
Sheet {
    id: dialog
    required property QtObject controller
    required property real windowWidth
    required property real windowHeight
    readonly property var u: controller.viewState.update
    readonly property bool working: u.status === "working"
    readonly property bool canCheck: !working && u.status !== "checking"
    readonly property bool canChange: canCheck && !u.problem
    property bool confirmRestore: false
    signal restartRequested()
    objectName: "updateDialog"
    width: Math.min(windowWidth - 40, 620)
    height: Math.min(windowHeight - 40, implicitHeight)
    closePolicy: working ? Popup.NoAutoClose : Popup.CloseOnEscape
    title: "Updates"
    subtitle: "Linux Theming " + u.current + (u.path ? " · " + homePath(u.path) : "")
    onOpened: { confirmRestore = false; controller.openUpdates() }

    function homePath(path) {
        const home = controller.homePath
        return home && path && path.startsWith(home + "/") ? "~" + path.slice(home.length) : path
    }
    function amount(value) { return value >= 10000000 ? Math.round(value / 1000000) + " MB" : (value / 1000000).toFixed(1) + " MB" }
    function phaseText() {
        const restoring = u.operation === "restore"
        switch (u.phase) {
        case "download": return restoring ? "Copying the kept version " + (u.previous.version || "") + "…"
                                          : "Downloading Linux Theming " + u.version + "…"
        case "verify": return "Checking that the new program starts…"
        case "keep": return "Keeping a copy of the version being replaced…"
        case "install": return "Replacing the AppImage…"
        default: return "Preparing…"
        }
    }
    function checkedText() {
        if (!u.lastCheck) return ""
        const when = new Date(u.lastCheck)
        return isNaN(when.getTime()) ? "" : "Last checked " + when.toLocaleString(Qt.locale(), Locale.ShortFormat) + "."
    }

    component Detail: RowLayout {
        property string label
        property string value
        property bool mono: false
        Layout.fillWidth: true
        spacing: 12
        Text { Layout.preferredWidth: 96; Layout.alignment: Qt.AlignTop; text: parent.label; color: Theme.textFaint; font.pixelSize: Theme.caption; textFormat: Text.PlainText }
        TextEdit {
            Layout.fillWidth: true
            text: parent.value
            readOnly: true
            selectByMouse: true
            wrapMode: TextEdit.WrapAnywhere
            color: Theme.text
            selectionColor: Theme.accentBorder
            selectedTextColor: Theme.text
            font.pixelSize: parent.mono ? 12 : Theme.body
            font.family: parent.mono ? Theme.mono : "sans-serif"
            textFormat: TextEdit.PlainText
            Accessible.name: parent.label
        }
    }

    contentItem: ColumnLayout {
        spacing: 14

        Banner {
            objectName: "updateMirror"
            Layout.fillWidth: true
            visible: !!u.mirror
            tone: "warning"
            title: "Updates come from " + (u.mirror || "") + ", not GitHub"
            body: "LINUX_THEMING_UPDATE_URL replaces this project's GitHub Releases. That source provides both the download and its checksums; install only if you trust it."
        }
        Banner {
            objectName: "updateUnsupported"
            Layout.fillWidth: true
            visible: !!u.problem
            tone: "warning"
            title: "This copy can't update itself"
            body: u.problem || ""
            Command { compact: true; text: "Open releases page"; onClicked: Qt.openUrlExternally(u.releases) }
        }
        Body {
            Layout.fillWidth: true
            visible: u.status === "idle" && !u.problem
            text: "Check GitHub for a newer release. Checking downloads nothing; you decide whether to install it."
        }
        Banner {
            Layout.fillWidth: true
            visible: u.status === "checking"
            tone: "info"
            title: "Checking GitHub for a newer release…"
        }
        Banner {
            objectName: "updateCurrent"
            Layout.fillWidth: true
            visible: u.status === "current"
            tone: "success"
            title: "Linux Theming " + u.current + " is the latest version"
            body: (u.version && u.version !== u.current ? "The latest release is " + u.version + ". " : "") + checkedText()
        }

        Rectangle {
            objectName: "updateAvailable"
            Layout.fillWidth: true
            visible: u.status === "available"
            implicitHeight: available.implicitHeight + 28
            radius: Theme.radius
            color: Theme.raised
            border.color: Theme.infoBorder
            ColumnLayout {
                id: available
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 14
                spacing: 10
                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        Layout.fillWidth: true
                        text: "Linux Theming " + u.version + " is available"
                        color: Theme.text
                        font.pixelSize: Theme.bodyLarge
                        font.weight: Font.DemiBold
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                    }
                    Command { objectName: "releaseNotes"; compact: true; quiet: true; text: "Release notes"; onClicked: Qt.openUrlExternally(u.page) }
                }
                Detail { label: "Replaces"; value: homePath(u.path) + " (" + u.current + ")" }
                Detail { label: "Download"; value: u.size + (u.published ? " · published " + u.published : "") }
                Detail { label: "SHA-256"; value: u.sha256; mono: true }
                Body {
                    Layout.fillWidth: true
                    font.pixelSize: Theme.caption
                    text: "Before anything is replaced, the download's size and checksum are checked against "
                          + (u.confirmed ? "the release's checksum file and GitHub's own digest" : "the release's checksum file")
                          + ", and the new program is started once in a temporary profile. Version " + u.current
                          + " is kept in " + homePath(u.keptIn) + " so you can switch back. This window keeps running "
                          + u.current + " until you restart. Your theme and history are not changed."
                }
            }
        }

        ColumnLayout {
            objectName: "updateProgress"
            Layout.fillWidth: true
            visible: working
            spacing: 8
            Text { Layout.fillWidth: true; text: phaseText(); color: Theme.text; font.pixelSize: Theme.body; wrapMode: Text.Wrap; textFormat: Text.PlainText }
            ProgressBar {
                id: bar
                Layout.fillWidth: true
                from: 0
                to: 1
                value: u.total > 0 ? u.done / u.total : 0
                indeterminate: u.total <= 0 || u.phase === "verify" || u.phase === "install"
                Accessible.name: phaseText()
                background: Rectangle { implicitHeight: 6; radius: 3; color: Theme.hover }
                contentItem: Item {
                    implicitHeight: 6
                    clip: true
                    Rectangle {
                        id: fill
                        height: parent.height
                        radius: 3
                        color: Theme.accent
                        width: bar.indeterminate ? parent.width * 0.3 : bar.visualPosition * parent.width
                        x: 0
                        XAnimator on x {
                            running: bar.indeterminate && dialog.working
                            loops: Animation.Infinite
                            from: -fill.width
                            to: bar.width
                            duration: 1200
                            easing.type: Easing.InOutQuad
                        }
                    }
                }
            }
            Text {
                visible: u.total > 0 && (u.phase === "download" || u.phase === "keep")
                text: amount(u.done) + " of " + amount(u.total)
                color: Theme.textFaint
                font.pixelSize: Theme.caption
                textFormat: Text.PlainText
            }
        }

        Banner {
            objectName: "updateInstalled"
            Layout.fillWidth: true
            visible: u.status === "installed"
            tone: "success"
            title: u.restored ? "Version " + u.installed + " is back in place" : "Linux Theming " + u.installed + " is installed"
            body: "Restart Linux Theming to use it. Until then this window keeps running " + u.current + "."
                  + (u.previous.version ? " Version " + u.previous.version + " is kept so you can switch back." : "")
        }
        Banner {
            objectName: "updateError"
            Layout.fillWidth: true
            visible: u.status === "error"
            tone: "danger"
            title: "The update did not complete"
            body: u.error || ""
            Command { objectName: "retryUpdate"; visible: u.retry !== "check"; compact: true; text: "Try again"; enabled: !working; onClicked: controller.retryUpdate() }
        }
        Banner {
            Layout.fillWidth: true
            visible: !!u.notice && !working
            tone: "info"
            title: u.notice || ""
        }

        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }

        Toggle {
            objectName: "automaticUpdates"
            Layout.fillWidth: true
            text: "Check for updates when Linux Theming starts"
            description: "At most once a day. It only checks; downloading and installing always wait for you."
            checked: !!u.automatic
            onToggled: controller.setAutomaticUpdates(checked)
        }

        RowLayout {
            objectName: "keptVersion"
            Layout.fillWidth: true
            visible: !!u.previous.version && !confirmRestore && u.status !== "installed"
            spacing: 8
            Text {
                Layout.fillWidth: true
                text: "Kept copy: version " + (u.previous.version || "") + " · " + (u.previous.sizeText || "")
                      + (u.previous.version === u.current ? " · same as this version" : "")
                color: Theme.textMuted
                font.pixelSize: Theme.body
                elide: Text.ElideRight
                textFormat: Text.PlainText
            }
            Command { objectName: "deleteKept"; compact: true; quiet: true; text: "Delete copy"; enabled: canChange; onClicked: controller.discardPrevious() }
            Command { objectName: "switchToKept"; visible: u.previous.version !== u.current; compact: true; text: "Switch to " + (u.previous.version || "") + "…"; enabled: canChange; onClicked: confirmRestore = true }
        }
        Banner {
            objectName: "confirmSwitch"
            Layout.fillWidth: true
            visible: confirmRestore && !!u.previous.version
            tone: "warning"
            title: "Replace this AppImage with the kept version " + (u.previous.version || "") + "?"
            body: homePath(u.path) + " is replaced after the kept copy's checksum is checked and it starts once in a temporary profile. "
                  + "The version it replaces is kept in turn, so you can switch again."
            Command { compact: true; quiet: true; text: "Cancel"; onClicked: confirmRestore = false }
            Command { objectName: "confirmSwitchButton"; compact: true; text: "Switch to " + (u.previous.version || ""); enabled: canChange; onClicked: { confirmRestore = false; controller.restorePrevious() } }
        }

        RowLayout {
            Layout.fillWidth: true
            Command { objectName: "closeUpdates"; text: u.status === "installed" ? "Later" : "Close"; enabled: !working; onClicked: dialog.close() }
            Item { Layout.fillWidth: true }
            Command { objectName: "cancelUpdate"; visible: working; text: "Cancel"; enabled: u.phase !== "install"; onClicked: controller.cancelUpdate() }
            Command {
                objectName: "checkUpdates"
                visible: ["idle", "current", "checking"].indexOf(u.status) >= 0 || u.status === "error" && u.retry === "check"
                primary: true
                text: u.status === "checking" ? "Checking…" : "Check now"
                enabled: canCheck
                onClicked: controller.checkForUpdates()
            }
            Command { objectName: "installUpdate"; visible: u.status === "available"; primary: true; text: "Download and install"; enabled: canChange; onClicked: controller.installUpdate() }
            Command { objectName: "restartUpdate"; visible: u.status === "installed"; primary: true; text: "Restart now"; onClicked: dialog.restartRequested() }
        }
    }
}

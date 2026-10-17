//
//  MenuContent.swift
//  Robin
//

import AppKit
import ServiceManagement
import SwiftUI

struct MenuLabel: View {
    let controller: ServiceController

    var body: some View {
        HStack(spacing: 4) {
            Image(nsImage: StatusDot.image(color: StatusDot.color(controller)))
            if controller.isRunning && controller.attentionCount > 0 {
                Text("\(controller.attentionCount)")
            }
        }
    }
}

enum StatusDot {
    static func color(_ controller: ServiceController) -> NSColor? {
        if controller.isFailing { return .systemRed }
        switch controller.state {
        case .running: return .systemGreen
        case .notInstalled: return nil
        default: return .systemYellow
        }
    }

    /// A filled dot in color, or a hollow one for "not installed". Not a template, so the menu bar keeps the color.
    static func image(color: NSColor?) -> NSImage {
        let image = NSImage(size: NSSize(width: 10, height: 10), flipped: false) { rect in
            let path = NSBezierPath(ovalIn: rect.insetBy(dx: 1, dy: 1))
            if let color {
                color.setFill()
                path.fill()
            } else {
                NSColor.secondaryLabelColor.setStroke()
                path.lineWidth = 1.5
                path.stroke()
            }
            return true
        }
        image.isTemplate = false
        return image
    }
}

struct MenuContent: View {
    let controller: ServiceController
    @Environment(\.openWindow) private var openWindow
    @State private var loginStatus = SMAppService.mainApp.status

    var body: some View {
        Text(statusLine)
        if let detail = statusDetail {
            Text(detail)
        }
        if controller.isRunning && controller.attentionCount > 0 {
            Button("\(controller.attentionCount) \(controller.attentionCount == 1 ? "needs" : "need") attention") {
                openDashboard()
            }
        }
        Divider()

        if case .notInstalled = controller.state {
            Button("Copy Install Command") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString("uv tool install .", forType: .string)
            }
        } else {
            Button("Open Dashboard") { openDashboard() }
                .disabled(!controller.isRunning)
            Button(restartTitle) { controller.restart() }
            Toggle(
                "Notifications",
                isOn: Binding(
                    get: { controller.notifications?.enabled ?? false },
                    set: { controller.setNotifications($0) }
                )
            )
            .disabled(controller.notifications == nil || !controller.isRunning)
        }

        Toggle("Launch at Login", isOn: Binding(get: { loginStatus == .enabled }, set: setLaunchAtLogin))
        if loginStatus == .requiresApproval {
            Button("Approve in System Settings…") { SMAppService.openSystemSettingsLoginItems() }
        }
        Divider()
        Button("Quit Robin") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    private var statusLine: String {
        switch controller.state {
        case .running(_, let owned): return owned ? "Running — started by Robin.app" : "Running — started in Terminal"
        case .starting, .discovering: return "Starting the service…"
        case .backoff(_, let delay): return "Service exited — retrying in \(Int(delay))s"
        case .stopping: return "Stopping the service…"
        case .error(let message): return message
        case .notInstalled: return "Robin isn't installed"
        }
    }

    private var statusDetail: String? {
        switch controller.state {
        case .running:
            return controller.health?.supervisorConnected == false ? "Supervisor disconnected" : nil
        case .backoff, .error:
            return controller.lastError
        case .notInstalled:
            return "Install from the Robin repo: uv tool install ."
        default:
            return nil
        }
    }

    private var restartTitle: String {
        if case .running(_, false) = controller.state { return "Restart Service (started in Terminal)" }
        return "Restart Service"
    }

    private func openDashboard() {
        openWindow(id: "dashboard")
        NSApp.activate()
    }

    private func setLaunchAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            NSLog("Launch at Login: \(error.localizedDescription)")
        }
        loginStatus = SMAppService.mainApp.status
    }
}

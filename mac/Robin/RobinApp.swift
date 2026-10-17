//
//  RobinApp.swift
//  Robin
//
//  A menu bar app that keeps the Robin service running and shows the dashboard in a window.
//

import AppKit
import SwiftUI

@main
struct RobinApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        MenuBarExtra {
            MenuContent(controller: appDelegate.controller)
        } label: {
            MenuLabel(controller: appDelegate.controller)
        }
        .menuBarExtraStyle(.menu)

        Window("Robin", id: "dashboard") {
            DashboardView(controller: appDelegate.controller)
        }
        .defaultSize(width: 1280, height: 820)
        .defaultLaunchBehavior(.suppressed)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    let controller = ServiceController()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }

    /// Covers Quit in the menu, ⌘Q with the window open, and logout: stop the service only if the app started it.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        var synchronous = true
        var done = false
        controller.quit {
            if synchronous { done = true } else { sender.reply(toApplicationShouldTerminate: true) }
        }
        synchronous = false
        return done ? .terminateNow : .terminateLater
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
}

//
//  DashboardView.swift
//  Robin
//
//  The existing web dashboard in a WKWebView, signed in with a fresh one-time link.
//

import AppKit
import SwiftUI
import WebKit

struct DashboardView: View {
    let controller: ServiceController
    @State private var page = DashboardPage()

    var body: some View {
        ZStack {
            WebView(page: page)
                .opacity(controller.isRunning ? 1 : 0)
            if !controller.isRunning {
                VStack(spacing: 6) {
                    Text(waitingTitle).font(.title3.weight(.semibold))
                    Text("The dashboard signs in on its own once the service is up.")
                        .foregroundStyle(.secondary)
                }
            }
        }
        .frame(minWidth: 720, minHeight: 480)
        .background(Self.backdrop)
        .onAppear {
            NSApp.setActivationPolicy(.regular)
            // An LSUIElement app turned regular can get the generic dock tile; hand it the bundle icon.
            NSApp.applicationIconImage = NSWorkspace.shared.icon(forFile: Bundle.main.bundlePath)
            NSApp.activate()
            page.onReauth = { [controller] in await controller.bootstrapURL() }
            Task { await page.signIn(force: true) }
        }
        .onDisappear {
            page.unload()  // drops the event stream so the service stops treating the dashboard as visible
            NSApp.setActivationPolicy(.accessory)
        }
        .onChange(of: controller.isRunning) { _, running in
            if running { Task { await page.signIn(force: true) } }
        }
        .onChange(of: controller.reauthGeneration) {
            Task { await page.signIn(force: true) }
        }
    }

    // Behind the page while the service starts: the dashboard's background in the system's appearance.
    private static let backdrop = Color(
        nsColor: NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
                ? NSColor(srgbRed: 0.035, green: 0.024, blue: 0.067, alpha: 1)
                : NSColor(srgbRed: 0.965, green: 0.953, blue: 0.988, alpha: 1)
        })

    private var waitingTitle: String {
        switch controller.state {
        case .starting, .discovering: return "Starting Robin…"
        default: return "Robin isn't running"
        }
    }
}

@MainActor
final class DashboardPage: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    let webView: WKWebView
    var onReauth: (() async -> URL?)?
    private var throttle = ReauthThrottle()
    private var base: URL?

    /// Tells the app when the page's own requests come back signed out, so it can mint a new link.
    private static let reauthScript = """
    (() => {
      const fetch0 = window.fetch;
      window.fetch = async (...args) => {
        const res = await fetch0(...args);
        if (res.status === 401) {
          res.clone().json().then((b) => {
            if (b && b.error && b.error.retry === "reauthenticate") window.webkit.messageHandlers.robin.postMessage("reauth");
          }).catch(() => {});
        }
        return res;
      };
    })();
    """

    override init() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: config)
        super.init()
        config.userContentController.addUserScript(
            WKUserScript(source: Self.reauthScript, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        )
        config.userContentController.add(self, name: "robin")
        webView.navigationDelegate = self
        webView.uiDelegate = self
        #if DEBUG
        webView.isInspectable = true
        #endif
    }

    func signIn(force: Bool) async {
        guard throttle.allow() || force, let url = await onReauth?() else { return }
        base = URL(string: "\(url.scheme ?? "http")://\(url.host ?? "127.0.0.1"):\(url.port ?? 80)")
        webView.load(URLRequest(url: url))
    }

    func unload() {
        webView.load(URLRequest(url: URL(string: "about:blank")!))
    }

    nonisolated func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        Task { @MainActor in await self.signIn(force: false) }
    }

    /// Stay on the Robin service; anything else opens in the default browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction) async -> WKNavigationActionPolicy {
        guard let url = action.request.url else { return .cancel }
        if url.scheme == "about" || (url.host == base?.host && url.port == base?.port) { return .allow }
        NSWorkspace.shared.open(url)
        return .cancel
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for action: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }
}

struct WebView: NSViewRepresentable {
    let page: DashboardPage

    func makeNSView(context: Context) -> WKWebView { page.webView }
    func updateNSView(_ nsView: WKWebView, context: Context) {}
}

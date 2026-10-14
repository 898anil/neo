import AppKit
import SwiftUI
import WebKit

/// neo's dashboard (the same page the phone uses) in a native window. Links that leave the
/// dashboard (Slack, GitHub, the Claude app) open in the default browser or app.
struct DashboardView: View {
    @Environment(Backend.self) private var backend
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Group {
            switch backend.state {
            case .running:
                WebView(url: backend.signedInURL)
            case .starting:
                ProgressView("Starting neo…").frame(maxWidth: .infinity, maxHeight: .infinity)
            case .stopped:
                placeholder("neo is stopped.", action: "Start") { Task { await backend.start() } }
            case let .failed(message):
                placeholder(message, action: "Try again") { Task { await backend.restart() } }
            }
        }
        .frame(minWidth: 420, minHeight: 560)
    }

    private func placeholder(_ text: String, action: String, perform: @escaping () -> Void) -> some View {
        VStack(spacing: 14) {
            Text(text).multilineTextAlignment(.center).foregroundStyle(.secondary)
            HStack {
                Button(action, action: perform).keyboardShortcut(.defaultAction)
                Button("Open log") { NSWorkspace.shared.open(Paths.log) }
                Button("Settings") { openWindow(id: "settings") }
            }
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct WebView: NSViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator { Coordinator(home: url) }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        let view = WKWebView(frame: .zero, configuration: config)
        view.navigationDelegate = context.coordinator
        view.uiDelegate = context.coordinator
        view.allowsBackForwardNavigationGestures = true
        view.setValue(false, forKey: "drawsBackground")  // no white flash before the dark page paints
        view.load(URLRequest(url: url))
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        // The port can change after a restart; reload when it does.
        if context.coordinator.home != url {
            context.coordinator.home = url
            view.load(URLRequest(url: url))
        }
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        var home: URL

        init(home: URL) { self.home = home }

        private func isDashboard(_ url: URL?) -> Bool {
            guard let url else { return false }
            return url.scheme == home.scheme && url.host == home.host && url.port == home.port
        }

        /// Schemes the system may open for a link the user clicked. Anything else (file:, smb:,
        /// custom app schemes) is refused: the page shows agent and Slack text we do not control.
        static let externalSchemes: Set<String> = ["http", "https", "mailto", "slack", "claude", "x-github-client"]

        static func shouldOpenExternally(_ url: URL, userClicked: Bool) -> Bool {
            userClicked && externalSchemes.contains(url.scheme?.lowercased() ?? "")
        }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url else { return decisionHandler(.cancel) }
            if isDashboard(url) || url.scheme == "about" || url.scheme == "blob" || url.scheme == "data" {
                return decisionHandler(.allow)
            }
            let mainFrame = action.targetFrame?.isMainFrame ?? true
            if mainFrame, Self.shouldOpenExternally(url, userClicked: action.navigationType == .linkActivated) {
                NSWorkspace.shared.open(url)
            }
            decisionHandler(.cancel)
        }

        // target=_blank / window.open: open outside, never in a second web view.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            guard let url = action.request.url else { return nil }
            if isDashboard(url) {
                webView.load(action.request)
            } else if Self.shouldOpenExternally(url, userClicked: action.navigationType == .linkActivated
                                                || action.navigationType == .other) {
                // window.open from a click handler arrives as .other; the scheme list still applies.
                NSWorkspace.shared.open(url)
            }
            return nil
        }

        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable (Bool) -> Void) {
            let alert = NSAlert()
            alert.messageText = message
            alert.addButton(withTitle: "OK")
            alert.addButton(withTitle: "Cancel")
            completionHandler(alert.runModal() == .alertFirstButtonReturn)
        }

        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable () -> Void) {
            let alert = NSAlert()
            alert.messageText = message
            alert.runModal()
            completionHandler()
        }

        func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable (String?) -> Void) {
            let alert = NSAlert()
            alert.messageText = prompt
            let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
            field.stringValue = defaultText ?? ""
            alert.accessoryView = field
            alert.addButton(withTitle: "OK")
            alert.addButton(withTitle: "Cancel")
            completionHandler(alert.runModal() == .alertFirstButtonReturn ? field.stringValue : nil)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            // The server can restart under us; try again shortly. A cancelled load is the user moving on.
            guard (error as NSError).code != NSURLErrorCancelled else { return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [home, weak webView] in
                webView?.load(URLRequest(url: home))
            }
        }
    }
}

import AppKit
import SwiftUI

@main
struct NeoApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        // First scene: SwiftUI opens it at launch. Setup shows here until neo is configured.
        Window("neo", id: "dashboard") {
            RootView()
                .environment(delegate.backend)
        }
        .defaultSize(width: 520, height: 820)

        Window("neo Settings", id: "settings") {
            SettingsView()
                .environment(delegate.backend)
        }
        .windowResizability(.contentSize)

        MenuBarExtra {
            MenuContent()
                .environment(delegate.backend)
        } label: {
            Image(systemName: menuIcon)
        }
    }

    private var menuIcon: String {
        switch delegate.backend.state {
        case .running: "circle.grid.2x1.fill"
        case .starting: "circle.grid.2x1"
        case .stopped, .failed: "circle.grid.2x1.left.filled"
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let backend = Backend()

    func applicationDidFinishLaunching(_ notification: Notification) {
        if backend.configured { Task { await backend.start() } }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { WindowOpener.shared.open("dashboard") }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        backend.shutdown()
    }
}

/// Lets AppKit callbacks (Dock icon click) open SwiftUI windows. SwiftUI hands over its
/// `openWindow` action when the main window first appears.
@MainActor
final class WindowOpener {
    static let shared = WindowOpener()
    var action: OpenWindowAction?

    func open(_ id: String) {
        action?(id: id)
        NSApp.activate(ignoringOtherApps: true)
    }
}

/// The main window: setup on the first run, the dashboard after that.
struct RootView: View {
    @Environment(Backend.self) private var backend
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Group {
            if backend.configured {
                DashboardView()
            } else {
                SettingsView(firstRun: true)
            }
        }
        .onAppear { WindowOpener.shared.action = openWindow }
    }
}

struct MenuContent: View {
    @Environment(Backend.self) private var backend
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Text(backend.statusLine)
        Divider()
        Button("Open neo") { show("dashboard") }
            .keyboardShortcut("o")
        Button("Open in browser") { NSWorkspace.shared.open(backend.signedInURL) }
            .disabled(backend.state != .running)
        Divider()
        if backend.state == .running || backend.state == .starting {
            Button("Restart") { Task { await backend.restart() } }
            Button("Stop") { backend.stop() }
        } else {
            Button("Start") { Task { await backend.start() } }
                .disabled(!backend.configured)
        }
        Divider()
        Button("Settings…") { show(backend.configured ? "settings" : "dashboard") }
            .keyboardShortcut(",")
        Button("Open log") { NSWorkspace.shared.open(Paths.log) }
        Divider()
        Button("Quit neo") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    private func show(_ id: String) {
        openWindow(id: id)
        NSApp.activate(ignoringOtherApps: true)
    }
}

import AppKit
import ServiceManagement
import SwiftUI

/// First-run setup and, later, Settings. Saving writes neo.env (+ the keychain) and restarts neo.
struct SettingsView: View {
    var firstRun = false
    @Environment(Backend.self) private var backend
    @Environment(\.dismissWindow) private var dismissWindow
    @State private var settings = NeoSettings.load()
    @State private var launchAtLogin = SMAppService.mainApp.status == .enabled
    @State private var error: String?
    @State private var checking = false

    var body: some View {
        Form {
            if firstRun {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Welcome to neo").font(.title2.bold())
                        Text("neo runs Claude Code and Codex on this Mac and shows every agent, run and question in one place. It uses the CLIs you are already signed in to, so there are no API keys to add.")
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 4)
                }
            }

            Section("Agents on this Mac") {
                ForEach(backend.tools) { tool in
                    ToolRow(tool: tool)
                }
                if backend.tools.isEmpty {
                    ProgressView().controlSize(.small)
                } else if !Tools.hasAgent(backend.tools) {
                    Label("Install Claude Code or Codex (above) to run tasks. The dashboard works without them.",
                          systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                }
                Button(checking ? "Checking…" : "Check again") {
                    checking = true
                    Task { await backend.refreshTools(); checking = false }
                }
                .disabled(checking)
            }

            Section("How neo works for you") {
                TextField("Your name", text: $settings.ownerName, prompt: Text("Used in the instructions neo gives agents"))
                Picker("Default agent", selection: $settings.engine) {
                    Text("Claude Code").tag("claude")
                    Text("Codex").tag("codex")
                }
                Picker("Background tasks may", selection: $settings.mode) {
                    Text("Only read").tag("readonly")
                    Text("Edit files in the project").tag("edit")
                    Text("Do anything (no approval prompts)").tag("full")
                }
                HStack {
                    TextField("Projects folder", text: $settings.workdir)
                    Button("Choose…", action: chooseFolder)
                }
            }

            Section {
                SecureField("Bot token (xoxb-…)", text: $settings.slackBotToken)
                SecureField("App token (xapp-…)", text: $settings.slackAppToken)
                TextField("Allowed Slack user ids", text: $settings.slackUserIds, prompt: Text("U0123ABC, comma separated"))
                if let problem = settings.problem {
                    Text(problem).foregroundStyle(.red)
                }
            } header: {
                Text("Slack (optional)")
            } footer: {
                Link("Mention @neo from Slack and get notified on your phone: how to create the Slack app",
                     destination: URL(string: "https://github.com/898anil/neo/blob/main/macos/README.md#slack")!)
                    .font(.footnote)
            }

            Section("General") {
                Toggle("Open neo when I log in", isOn: $launchAtLogin)
                    .onChange(of: launchAtLogin) { _, on in setLaunchAtLogin(on) }
                Stepper("Dashboard port: \(String(settings.port))", value: $settings.port, in: 1025...65000)
                HStack {
                    Button("Show neo's files") { NSWorkspace.shared.open(Paths.home) }
                    Button("Open log") { NSWorkspace.shared.open(Paths.log) }
                }
            }

            if let error {
                Text(error).foregroundStyle(.red)
            }
        }
        .formStyle(.grouped)
        .frame(width: 520)
        .frame(minHeight: 560)
        .safeAreaInset(edge: .bottom) {
            HStack {
                Spacer()
                if !firstRun { Button("Cancel") { dismissWindow() } }
                Button(firstRun ? "Start neo" : "Save") { save() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(settings.problem != nil)
            }
            .padding()
            .background(.bar)
        }
        .task { if backend.tools.isEmpty { await backend.refreshTools() } }
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.directoryURL = URL(fileURLWithPath: settings.workdir)
        if panel.runModal() == .OK, let url = panel.url { settings.workdir = url.path }
    }

    private func setLaunchAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            self.error = "Could not change login item: \(error.localizedDescription). Move Neo to Applications first."
            launchAtLogin = SMAppService.mainApp.status == .enabled
        }
    }

    private func save() {
        if let problem = settings.problem {
            error = problem
            return
        }
        do {
            try settings.save()
        } catch {
            self.error = "Could not save: \(error.localizedDescription)"
            return
        }
        Task { await backend.restart() }
        if !firstRun { dismissWindow() }  // on the first run this view becomes the dashboard
    }
}

private struct ToolRow: View {
    let tool: ToolCheck

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Image(systemName: tool.found ? "checkmark.circle.fill" : "circle.dashed")
                .foregroundStyle(tool.found ? .green : .secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(tool.name).font(.body.monospaced())
                Text(tool.found ? (tool.version ?? tool.path ?? "") : tool.purpose)
                    .font(.caption).foregroundStyle(.secondary)
                if !tool.found {
                    Text(tool.installHint).font(.caption.monospaced()).textSelection(.enabled)
                }
            }
        }
    }
}

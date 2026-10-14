import Foundation
import Observation
import Security

/// Runs neo on this Mac as a child process: the dashboard alone (`python -m cos serve`), or the
/// Slack bot, which serves the same dashboard, when Slack tokens are set. Restarts it if it dies.
@MainActor
@Observable
final class Backend {
    enum State: Equatable {
        case stopped
        case starting
        case running
        case failed(String)
    }

    private(set) var state: State = .stopped
    private(set) var port: Int = NeoSettings.defaultPort
    private(set) var tools: [ToolCheck] = []
    private(set) var slackMode = false
    /// Setup has been saved at least once (neo.env exists).
    private(set) var configured = NeoSettings.isConfigured

    private var process: Process?
    private var wantRunning = false
    private var crashes: [Date] = []
    private var loginPath: String?
    /// Only one start / restart at a time: each one awaits, and two interleaved would spawn two children.
    private var transition: Task<Void, Never>?
    /// The dashboard trusts plain localhost unless COS_TOKEN is set, and any local process (or other
    /// account) can reach localhost. So the app always sets a token: neo.env's COS_TOKEN, else one kept
    /// in Application Support (0600) so the browser's 30-day cookie and Slack links keep working.
    private(set) var token = ""

    var dashboardURL: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    /// The dashboard with the token; the server answers by setting a cookie, so later requests carry it.
    var signedInURL: URL {
        var parts = URLComponents(url: dashboardURL, resolvingAgainstBaseURL: false)!
        parts.queryItems = [URLQueryItem(name: "token", value: token)]
        return parts.url!
    }

    var statusLine: String {
        switch state {
        case .stopped: "Stopped"
        case .starting: "Starting…"
        case .running: slackMode ? "Running · Slack connected" : "Running"
        case let .failed(message): message
        }
    }

    /// Looks for the agent CLIs again (after the user installs one).
    func refreshTools() async {
        let path = await Task.detached { Tools.loginPath() }.value
        loginPath = path
        tools = await Task.detached { Tools.check(path: path) }.value
    }

    func start() async {
        wantRunning = true
        await serialized { await self.launch() }
    }

    func restart() async {
        wantRunning = true
        await serialized {
            await self.terminateChild()
            await self.launch()
        }
    }

    func stop() {
        wantRunning = false
        process?.terminate()
        process = nil
        state = .stopped
    }

    /// Stop the child synchronously when the app quits, so no orphan keeps the port. (If the app
    /// crashes instead, the child sees NEO_PARENT_PID disappear and exits on its own.)
    func shutdown() {
        wantRunning = false
        guard let p = process else { return }
        p.terminate()
        let deadline = Date().addingTimeInterval(3)
        while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        process = nil
    }

    /// Chains each start/restart after the previous one, so they never interleave.
    private func serialized(_ work: @escaping @MainActor () async -> Void) async {
        let previous = transition
        let task = Task { await previous?.value; await work() }
        transition = task
        await task.value
        if transition == task { transition = nil }
    }

    private func terminateChild() async {
        guard let p = process else { return }
        process = nil  // exited() ignores a child that is no longer ours, so this is not a crash
        p.terminate()
        await Task.detached { p.waitUntilExit() }.value
    }

    private func launch() async {
        configured = NeoSettings.isConfigured
        guard wantRunning, process == nil else { return }
        state = .starting
        if loginPath == nil { await refreshTools() }
        // stop() may have been pressed while we were looking for the tools.
        guard wantRunning, process == nil else { return }
        guard let python = Paths.bundledPython, let code = Paths.bundledCode,
              FileManager.default.isExecutableFile(atPath: python.path) else {
            state = .failed("This copy of Neo is incomplete (no bundled Python). Reinstall it.")
            return
        }
        let settings = NeoSettings.load()
        if let problem = settings.problem {  // neo.env can be edited by hand
            state = .failed("Settings need fixing: \(problem)")
            return
        }
        port = Self.freePort(preferred: settings.port)
        slackMode = settings.slackEnabled

        let p = Process()
        p.executableURL = python
        p.currentDirectoryURL = code
        p.arguments = slackMode ? ["app.py"] : ["-m", "cos", "serve", "--host", "127.0.0.1", "--port", String(port)]
        p.environment = environment(settings)
        do {
            try FileManager.default.createDirectory(at: Paths.logDir, withIntermediateDirectories: true)
            if !FileManager.default.fileExists(atPath: Paths.log.path) {
                FileManager.default.createFile(atPath: Paths.log.path, contents: nil)
            }
            let log = try FileHandle(forWritingTo: Paths.log)
            log.seekToEndOfFile()
            p.standardOutput = log
            p.standardError = log
            p.standardInput = FileHandle.nullDevice
            p.terminationHandler = { [weak self] proc in
                let status = proc.terminationStatus
                Task { @MainActor in self?.exited(proc, status: status) }
            }
            try p.run()
        } catch {
            state = .failed("Could not start neo: \(error.localizedDescription)")
            return
        }
        process = p
        await waitUntilServing(p)
    }

    private func environment(_ settings: NeoSettings) -> [String: String] {
        let inherited = ProcessInfo.processInfo.environment
        var env: [String: String] = [:]
        for key in ["HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "SSH_AUTH_SOCK"] {
            if let value = inherited[key] { env[key] = value }
        }
        env["LANG"] = env["LANG"] ?? "en_US.UTF-8"
        // Every neo setting from neo.env (the file the Settings window writes, plus anything added by hand).
        for (key, value) in EnvFile.load(Paths.envFile).values where !key.hasPrefix("SLACK_") {
            env[key] = value  // Slack tokens come only from the keychain, below
        }
        token = env["COS_TOKEN"].flatMap { $0.isEmpty ? nil : $0 } ?? Self.storedToken()
        env["COS_TOKEN"] = token
        env["PATH"] = loginPath ?? Tools.loginPath()
        env["COS_HOME"] = Paths.home.path
        env["NEO_MEMORY_DIR"] = env["NEO_MEMORY_DIR"] ?? Paths.memory.path
        env["NEO_DASHBOARD_HOST"] = "127.0.0.1"
        env["NEO_DASHBOARD_PORT"] = String(port)
        env["NEO_DASHBOARD_URL"] = env["NEO_DASHBOARD_URL"] ?? dashboardURL.absoluteString
        env["NEO_PARENT_PID"] = String(getpid())
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        for tool in tools where tool.found {
            env["\(tool.name.uppercased())_CLI_PATH"] = env["\(tool.name.uppercased())_CLI_PATH"] ?? tool.path
        }
        if settings.slackEnabled {
            env["SLACK_BOT_TOKEN"] = settings.slackBotToken
            env["SLACK_APP_TOKEN"] = settings.slackAppToken
        }
        try? FileManager.default.createDirectory(at: Paths.home, withIntermediateDirectories: true)
        return env
    }

    private func waitUntilServing(_ p: Process) async {
        // The Slack bot connects to Slack before it serves the dashboard, so allow it longer.
        let deadline = Date().addingTimeInterval(slackMode ? 90 : 30)
        var request = URLRequest(url: dashboardURL.appendingPathComponent("api/runs"))
        request.timeoutInterval = 2
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        while Date() < deadline, process === p, p.isRunning {
            if let (_, response) = try? await URLSession.shared.data(for: request),
               (response as? HTTPURLResponse)?.statusCode == 200 {
                state = .running
                return
            }
            try? await Task.sleep(for: .milliseconds(300))
        }
        if process === p, p.isRunning {
            state = .failed("neo started but its dashboard is not answering. See the log.")
        }
    }

    private func exited(_ p: Process, status: Int32) {
        guard process === p else { return }
        process = nil
        guard wantRunning else {
            state = .stopped
            return
        }
        crashes = crashes.filter { $0 > Date().addingTimeInterval(-300) } + [Date()]
        if crashes.count >= 4 {
            state = .failed("neo keeps stopping (exit \(status)). See the log.")
            wantRunning = false
            return
        }
        state = .starting
        Task {
            try? await Task.sleep(for: .seconds(Double(crashes.count) * 2))
            if wantRunning { await start() }
        }
    }

    /// The preferred port, or the next free one (another neo or app may hold it).
    nonisolated static func freePort(preferred: Int) -> Int {
        for candidate in preferred..<(preferred + 50) where isFree(candidate) { return candidate }
        return preferred
    }

    /// Can a server listen here? SO_REUSEADDR like Python's server, so our own previous run's
    /// TIME_WAIT connections do not push the dashboard to a new port on every restart.
    nonisolated static func isFree(_ port: Int) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var one: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(UInt16(port).bigEndian)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let result = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        return result == 0
    }

    /// The dashboard token kept across launches, created on first use.
    nonisolated static func storedToken() -> String {
        let url = Paths.home.appendingPathComponent("dashboard.token")
        if let saved = try? String(contentsOf: url, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
           saved.count >= 32 {
            return saved
        }
        let fresh = randomToken()
        try? FileManager.default.createDirectory(at: Paths.home, withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: url.path, contents: Data(fresh.utf8), attributes: [.posixPermissions: 0o600])
        return fresh
    }

    nonisolated static func randomToken() -> String {
        var bytes = [UInt8](repeating: 0, count: 24)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return bytes.map { String(format: "%02x", $0) }.joined()
    }
}

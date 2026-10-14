import Foundation

/// The command line tools neo drives, found the way the user's own terminal finds them.
/// Apps started from Finder get a bare PATH (/usr/bin:/bin:...), so we ask the login shell once.
struct ToolCheck: Identifiable, Equatable {
    let name: String
    let purpose: String
    let required: Bool
    let installHint: String
    var path: String?
    var version: String?

    var id: String { name }
    var found: Bool { path != nil }
}

enum Tools {
    static let extraDirs = ["~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.claude/local", "~/.bun/bin", "~/.volta/bin"]

    /// PATH from the user's login shell, plus the usual install folders, without duplicates.
    static func loginPath() -> String {
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
        // `env` prints PATH the same way in every shell (fish keeps $PATH as a list); the marker
        // skips anything a login script prints first.
        let marker = "__NEO_ENV__"
        let fromShell = run(shell, ["-l", "-c", "echo \(marker); /usr/bin/env"], timeout: 8)
            .flatMap { $0.components(separatedBy: marker).last }?
            .split(separator: "\n").first { $0.hasPrefix("PATH=") }
            .map { String($0.dropFirst(5)) } ?? ""
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let parts = fromShell.split(separator: ":").map(String.init)
            + extraDirs.map { $0.replacingOccurrences(of: "~", with: home) }
            + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        var seen = Set<String>()
        return parts.filter { !$0.isEmpty && seen.insert($0).inserted }.joined(separator: ":")
    }

    static func which(_ name: String, path: String) -> String? {
        for dir in path.split(separator: ":") {
            let candidate = "\(dir)/\(name)"
            if FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        return nil
    }

    static func check(path: String) -> [ToolCheck] {
        var tools = [
            ToolCheck(name: "claude", purpose: "Claude Code: runs tasks", required: false,
                      installHint: "curl -fsSL https://claude.ai/install.sh | bash, then run `claude` once to sign in"),
            ToolCheck(name: "codex", purpose: "Codex CLI: runs tasks", required: false,
                      installHint: "npm install -g @openai/codex, then run `codex` once to sign in"),
            ToolCheck(name: "herdr", purpose: "live agents you can take over (optional)", required: false,
                      installHint: "brew install herdr"),
            ToolCheck(name: "gh", purpose: "pull requests on the Work tab (optional)", required: false,
                      installHint: "brew install gh && gh auth login"),
        ]
        for i in tools.indices {
            tools[i].path = which(tools[i].name, path: path)
            if let p = tools[i].path {
                tools[i].version = run(p, ["--version"], timeout: 5)?
                    .split(separator: "\n").first.map { String($0).trimmingCharacters(in: .whitespaces) }
            }
        }
        return tools
    }

    /// At least one agent CLI is needed; everything else is optional.
    static func hasAgent(_ tools: [ToolCheck]) -> Bool {
        tools.contains { ($0.name == "claude" || $0.name == "codex") && $0.found }
    }

    /// Runs a short command and returns stdout, or nil on failure / timeout.
    static func run(_ launchPath: String, _ args: [String], timeout: TimeInterval) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: launchPath)
        process.arguments = args
        let out = Pipe()
        process.standardOutput = out
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
        // Collect output as it arrives, so a chatty command cannot fill the pipe and stall, and a
        // background child of a login script holding the pipe open cannot block us past the deadline.
        let buffer = OutputBuffer()
        out.fileHandleForReading.readabilityHandler = { buffer.append($0.availableData) }
        do { try process.run() } catch { return nil }
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        if process.isRunning {
            process.terminate()
            out.fileHandleForReading.readabilityHandler = nil
            return nil
        }
        Thread.sleep(forTimeInterval: 0.05)  // let the handler take the last chunk
        out.fileHandleForReading.readabilityHandler = nil
        let data = buffer.data
        guard process.terminationStatus == 0 else { return nil }
        return String(data: data, encoding: .utf8)
    }
}

private final class OutputBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var stored = Data()

    func append(_ chunk: Data) { lock.lock(); stored.append(chunk); lock.unlock() }
    var data: Data { lock.lock(); defer { lock.unlock() }; return stored }
}

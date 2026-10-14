import Foundation
import Security

/// Where the app keeps its state. neo's own state (runs, memory, watches) lives under `home`,
/// which the backend sees as COS_HOME, so the app bundle itself stays read-only.
enum Paths {
    static var home: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("Neo", isDirectory: true)
    }
    static var envFile: URL { home.appendingPathComponent("neo.env") }
    static var memory: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".neo/memory", isDirectory: true)
    }
    static var logDir: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Neo", isDirectory: true)
    }
    static var log: URL { logDir.appendingPathComponent("neo.log") }

    /// neo's Python code and the Python that runs it, both inside the app bundle.
    static var bundledCode: URL? { Bundle.main.resourceURL?.appendingPathComponent("neo", isDirectory: true) }
    static var bundledPython: URL? { Bundle.main.resourceURL?.appendingPathComponent("python/bin/python3") }
}

/// A KEY=VALUE file in the same format as neo's `.env`. Unknown keys and comments are kept,
/// so people can add any of neo's other settings (see .env.example) by hand.
struct EnvFile: Equatable {
    private(set) var lines: [String]

    init(text: String = "") {
        lines = text.isEmpty ? [] : text.components(separatedBy: "\n")
        while lines.last == "" { lines.removeLast() }
    }

    static func load(_ url: URL) -> EnvFile {
        EnvFile(text: (try? String(contentsOf: url, encoding: .utf8)) ?? "")
    }

    var values: [String: String] {
        var out: [String: String] = [:]
        for line in lines {
            if let (key, value) = Self.parse(line) { out[key] = value }
        }
        return out
    }

    subscript(key: String) -> String? {
        get { values[key] }
        set {
            let index = lines.lastIndex { Self.parse($0)?.0 == key }
            // One line per key: a pasted newline would otherwise start a second, unintended setting.
            let cleaned = (newValue ?? "").components(separatedBy: .newlines).joined()
                .trimmingCharacters(in: .whitespaces)
            switch (index, cleaned.isEmpty) {
            case let (i?, true): lines.remove(at: i)
            case let (i?, false): lines[i] = "\(key)=\(Self.quote(cleaned))"
            case (nil, false): lines.append("\(key)=\(Self.quote(cleaned))")
            case (nil, true): break
            }
        }
    }

    var text: String { lines.joined(separator: "\n") + (lines.isEmpty ? "" : "\n") }

    func save(_ url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try text.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    static func parse(_ line: String) -> (String, String)? {
        var trimmed = line.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty, !trimmed.hasPrefix("#") else { return nil }
        if trimmed.hasPrefix("export ") { trimmed = String(trimmed.dropFirst(7)) }
        guard let eq = trimmed.firstIndex(of: "=") else { return nil }
        let key = trimmed[..<eq].trimmingCharacters(in: .whitespaces)
        guard !key.isEmpty, key.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "_" }) else { return nil }
        var value = trimmed[trimmed.index(after: eq)...].trimmingCharacters(in: .whitespaces)
        if value.count >= 2, let first = value.first, first == value.last, first == "\"" || first == "'" {
            value = String(value.dropFirst().dropLast())
        }
        return (key, value)
    }

    /// Quoted when needed, the way python-dotenv reads it back: single quotes are literal, so a value
    /// with a double quote is single-quoted; one with both kinds loses its single quotes.
    private static func quote(_ value: String) -> String {
        guard value.contains(where: { " #\"'$".contains($0) }) else { return value }
        if !value.contains("'") { return "'\(value)'" }
        return "\"\(value.replacingOccurrences(of: "'", with: ""))\""
    }
}

/// Slack tokens are secrets, so they live in the login keychain, not in neo.env.
enum Keychain {
    private static let service = "dev.neo.mac"

    static func get(_ account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
            kSecAttrAccount as String: account, kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    @discardableResult
    static func set(_ account: String, _ value: String?) -> Bool {
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(base as CFDictionary)
        guard let value, !value.isEmpty else { return true }
        var add = base
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        return SecItemAdd(add as CFDictionary, nil) == errSecSuccess
    }
}

/// The choices made in Setup / Settings. Everything else neo reads stays in neo.env.
struct NeoSettings: Equatable {
    static let defaultPort = 8790
    static let slackSecretKeys = ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"]

    var ownerName = ""
    var workdir = FileManager.default.homeDirectoryForCurrentUser.path
    var engine = "claude"
    var mode = "edit"
    var port = defaultPort
    var slackBotToken = ""
    var slackAppToken = ""
    var slackUserIds = ""

    var slackEnabled: Bool { !slackBotToken.isEmpty && !slackAppToken.isEmpty }

    /// Slack member ids (U… or W…), comma separated. Empty means none.
    var slackUserIdList: [String] {
        slackUserIds.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }

    /// Why these settings cannot be saved, or nil. Slack without an allow-list would let anyone in
    /// the workspace (guests and Slack Connect users included) run agents on this Mac.
    var problem: String? {
        if slackBotToken.isEmpty != slackAppToken.isEmpty { return "Slack needs both tokens, or neither." }
        guard slackEnabled else { return nil }
        if !slackBotToken.hasPrefix("xoxb-") { return "The bot token starts with xoxb-." }
        if !slackAppToken.hasPrefix("xapp-") { return "The app token starts with xapp-." }
        let ids = slackUserIdList
        if ids.isEmpty { return "Add your Slack member id, so only you can start agents on this Mac." }
        if let bad = ids.first(where: { $0.range(of: "^[UW][A-Z0-9]{6,}$", options: .regularExpression) == nil }) {
            return "\(bad) is not a Slack member id (they look like U0123ABCD)."
        }
        return nil
    }

    static func load() -> NeoSettings {
        let env = EnvFile.load(Paths.envFile)
        var s = NeoSettings()
        s.ownerName = env["NEO_OWNER_NAME"] ?? ""
        s.workdir = env["NEO_WORKDIR"] ?? s.workdir
        s.engine = env["NEO_DEFAULT_ENGINE"] ?? s.engine
        s.mode = env["NEO_MODE"] ?? s.mode
        s.port = Int(env["NEO_DASHBOARD_PORT"] ?? "") ?? s.port
        s.slackUserIds = env["ALLOWED_USER_IDS"] ?? ""
        s.slackBotToken = Keychain.get("SLACK_BOT_TOKEN") ?? ""
        s.slackAppToken = Keychain.get("SLACK_APP_TOKEN") ?? ""
        return s
    }

    static var isConfigured: Bool { FileManager.default.fileExists(atPath: Paths.envFile.path) }

    func save() throws {
        var env = EnvFile.load(Paths.envFile)
        env["NEO_OWNER_NAME"] = ownerName
        env["NEO_WORKDIR"] = workdir
        env["NEO_DEFAULT_ENGINE"] = engine
        env["NEO_MODE"] = mode
        env["NEO_DASHBOARD_PORT"] = String(port)
        env["ALLOWED_USER_IDS"] = slackUserIdList.joined(separator: ",")
        for key in Self.slackSecretKeys { env[key] = nil }  // never on disk
        guard Keychain.set("SLACK_BOT_TOKEN", slackBotToken), Keychain.set("SLACK_APP_TOKEN", slackAppToken) else {
            throw NSError(domain: "Neo", code: 1, userInfo: [NSLocalizedDescriptionKey: "the keychain refused the Slack tokens"])
        }
        try env.save(Paths.envFile)
    }
}

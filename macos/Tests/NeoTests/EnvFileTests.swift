import XCTest
@testable import Neo

final class EnvFileTests: XCTestCase {
    func testParsesNeoEnvFormat() {
        let env = EnvFile(text: """
        # comment
        NEO_OWNER_NAME=Alex
        export NEO_WORKDIR="/Users/alex/My Projects"
        NEO_MODE='edit'
        not a line
        =novalue
        """)
        XCTAssertEqual(env["NEO_OWNER_NAME"], "Alex")
        XCTAssertEqual(env["NEO_WORKDIR"], "/Users/alex/My Projects")
        XCTAssertEqual(env["NEO_MODE"], "edit")
        XCTAssertEqual(env.values.count, 3)
    }

    func testSetKeepsCommentsAndUnknownKeys() {
        var env = EnvFile(text: "# mine\nNEO_GITHUB_ORG=acme\nNEO_OWNER_NAME=Old\n")
        env["NEO_OWNER_NAME"] = "New Name"
        env["NEO_MODE"] = "full"
        XCTAssertEqual(env.text, "# mine\nNEO_GITHUB_ORG=acme\nNEO_OWNER_NAME=\"New Name\"\nNEO_MODE=full\n")
    }

    func testEmptyValueRemovesKey() {
        var env = EnvFile(text: "SLACK_BOT_TOKEN=xoxb-1\nA=1\n")
        env["SLACK_BOT_TOKEN"] = ""
        env["MISSING"] = nil
        XCTAssertEqual(env.text, "A=1\n")
    }

    func testRoundTripThroughDisk() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString).appendingPathComponent("neo.env")
        var env = EnvFile()
        env["NEO_DEFAULT_ENGINE"] = "codex"
        try env.save(url)
        XCTAssertEqual(EnvFile.load(url)["NEO_DEFAULT_ENGINE"], "codex")
        let perms = try FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions] as? Int
        XCTAssertEqual(perms, 0o600)
    }

    @MainActor
    func testFreePortSkipsABusyPort() throws {
        let busy = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(busy) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(busy, $0, len) } }
        _ = withUnsafeMutablePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(busy, $0, &len) } }
        let port = Int(UInt16(bigEndian: addr.sin_port))
        XCTAssertFalse(Backend.isFree(port))
        XCTAssertNotEqual(Backend.freePort(preferred: port), port)
    }

    func testLoginPathIncludesUsualInstallFolders() {
        let path = Tools.loginPath()
        XCTAssertTrue(path.contains("/usr/bin"))
        XCTAssertTrue(path.contains("/opt/homebrew/bin"))
    }
}

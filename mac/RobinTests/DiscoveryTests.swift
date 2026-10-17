import XCTest

@MainActor
final class DiscoveryTests: XCTestCase {
    private var tmp: URL!

    override func setUp() async throws {
        tmp = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: tmp)
    }

    func testPrefersLocalBin() {
        let found = BinaryLocator.resolve(home: "/h") { _ in true }
        XCTAssertEqual(found?.path, "/h/.local/bin/robin")
    }

    func testFallsBackToHomebrew() {
        let found = BinaryLocator.resolve(home: "/h") { $0 == "/opt/homebrew/bin/robin" }
        XCTAssertEqual(found?.path, "/opt/homebrew/bin/robin")
    }

    func testNotInstalled() {
        XCTAssertNil(BinaryLocator.resolve(home: "/h") { _ in false })
    }

    func testDefaultDataDir() {
        XCTAssertEqual(ServiceFiles.dataDir(env: [:], home: "/h").path, "/h/Library/Application Support/Robin")
    }

    func testDataDirOverrideExpandsTilde() {
        let dir = ServiceFiles.dataDir(env: ["ROBIN_DATA_DIR": "~/robin-data"], home: "/h")
        XCTAssertEqual(dir.path, "/h/robin-data")
    }

    func testEndpoint() throws {
        try #"{"port": 7333, "url": "http://127.0.0.1:7333"}"#.write(
            to: tmp.appendingPathComponent("service.json"), atomically: true, encoding: .utf8)
        XCTAssertEqual(ServiceFiles.endpoint(dataDir: tmp)?.absoluteString, "http://127.0.0.1:7333")
    }

    func testEndpointMissingOrBroken() throws {
        XCTAssertNil(ServiceFiles.endpoint(dataDir: tmp))
        try "{not json".write(to: tmp.appendingPathComponent("service.json"), atomically: true, encoding: .utf8)
        XCTAssertNil(ServiceFiles.endpoint(dataDir: tmp))
        try #"{"port": 7333}"#.write(to: tmp.appendingPathComponent("service.json"), atomically: true, encoding: .utf8)
        XCTAssertNil(ServiceFiles.endpoint(dataDir: tmp))
    }

    func testEndpointRejectsNonLoopback() throws {
        try #"{"url": "http://example.com:7333"}"#.write(
            to: tmp.appendingPathComponent("service.json"), atomically: true, encoding: .utf8)
        XCTAssertNil(ServiceFiles.endpoint(dataDir: tmp))
    }

    func testTokenIsTrimmed() throws {
        XCTAssertNil(ServiceFiles.token(dataDir: tmp))
        try "secret\n".write(to: tmp.appendingPathComponent("cli.token"), atomically: true, encoding: .utf8)
        XCTAssertEqual(ServiceFiles.token(dataDir: tmp), "secret")
    }

    func testChildEnvironmentPutsCLIFoldersFirst() {
        let env = ChildEnvironment.make(base: ["PATH": "/usr/bin:/opt/homebrew/bin", "HOME": "/h"], home: "/h")
        XCTAssertEqual(
            env["PATH"], "/h/.local/bin:/opt/homebrew/bin:/usr/local/bin:/h/.npm-global/bin:/usr/bin")
        XCTAssertEqual(env["HOME"], "/h")
    }
}

import XCTest

@MainActor
final class RobinAPITests: XCTestCase {
    private let base = URL(string: "http://127.0.0.1:7333")!

    func testRequestCarriesBearerToken() {
        let r = RobinAPI.request(method: "GET", path: "/health", base: base, token: "t0k")
        XCTAssertEqual(r.url?.absoluteString, "http://127.0.0.1:7333/api/v1/health")
        XCTAssertEqual(r.httpMethod, "GET")
        XCTAssertEqual(r.value(forHTTPHeaderField: "Authorization"), "Bearer t0k")
        XCTAssertNil(r.httpBody)
    }

    func testNotificationsBodyKeepsKinds() throws {
        let body = try JSONEncoder().encode(NotificationSettings(enabled: false, kinds: ["input"]))
        let r = RobinAPI.request(method: "PUT", path: "/settings/notifications", base: base, token: "t", body: body)
        XCTAssertEqual(r.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let sent = try JSONSerialization.jsonObject(with: r.httpBody!) as? [String: Any]
        XCTAssertEqual(sent?["enabled"] as? Bool, false)
        XCTAssertEqual(sent?["kinds"] as? [String], ["input"])
    }

    func testDecodesHealth() throws {
        let json = #"""
        {"service":"ready","supervisor_connected":false,"pid":4242,"ready":true,"ok":true,
         "checks":{"service":{"ok":true}}}
        """#
        let h = try JSONDecoder().decode(Health.self, from: Data(json.utf8))
        XCTAssertEqual(h, Health(pid: 4242, supervisorConnected: false))
    }

    func testDecodesAttention() throws {
        let json = #"{"items":[{"attention_id":"att_1","kind":"approval","state":"open"},{"attention_id":"att_2"}]}"#
        XCTAssertEqual(try JSONDecoder().decode(AttentionList.self, from: Data(json.utf8)).items.count, 2)
    }

    func testDecodesNotificationsAndBootstrap() throws {
        let n = try JSONDecoder().decode(
            NotificationSettings.self, from: Data(#"{"enabled":true,"kinds":["input","failed","finished"]}"#.utf8))
        XCTAssertEqual(n, NotificationSettings(enabled: true, kinds: ["input", "failed", "finished"]))
        let b = try JSONDecoder().decode(
            Bootstrap.self, from: Data(#"{"url":"http://127.0.0.1:7333/#bootstrap=abc"}"#.utf8))
        XCTAssertEqual(b.url.fragment, "bootstrap=abc")
    }

    func testErrorMessage() {
        let body = #"{"error":{"code":"csrf","message":"missing or wrong CSRF token","retry":"reauthenticate"}}"#
        XCTAssertEqual(RobinAPI.errorMessage(Data(body.utf8)), "csrf: missing or wrong CSRF token")
        XCTAssertEqual(RobinAPI.errorMessage(Data("plain".utf8)), "plain")
    }
}

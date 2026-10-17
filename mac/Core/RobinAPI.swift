//
//  RobinAPI.swift
//  Robin
//
//  The few service calls the app makes, authenticated with the CLI token.
//

import Foundation

struct Health: Decodable, Equatable {
    let pid: Int32
    let supervisorConnected: Bool

    enum CodingKeys: String, CodingKey {
        case pid
        case supervisorConnected = "supervisor_connected"
    }
}

struct AttentionList: Decodable, Equatable {
    struct Item: Decodable, Equatable {
        let attentionId: String
        enum CodingKeys: String, CodingKey { case attentionId = "attention_id" }
    }
    let items: [Item]
}

struct NotificationSettings: Codable, Equatable {
    var enabled: Bool
    var kinds: [String]
}

struct Bootstrap: Decodable, Equatable {
    let url: URL
}

enum APIError: Error, Equatable {
    case unreachable
    case unauthorized
    case http(Int, String)
}

struct RobinAPI {
    let base: URL
    let token: String
    var session: URLSession = .shared

    static func request(method: String, path: String, base: URL, token: String, body: Data? = nil) -> URLRequest {
        var r = URLRequest(url: base.appendingPathComponent("api/v1" + path), timeoutInterval: 3)
        r.httpMethod = method
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            r.httpBody = body
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return r
    }

    /// The message from Robin's `{"error": {"code", "message", ...}}` body, or the raw text.
    static func errorMessage(_ data: Data) -> String {
        struct Body: Decodable { struct E: Decodable { let code: String; let message: String }; let error: E }
        if let b = try? JSONDecoder().decode(Body.self, from: data) { return "\(b.error.code): \(b.error.message)" }
        return String(data: data, encoding: .utf8) ?? ""
    }

    func health() async throws -> Health { try await call("GET", "/health") }
    func attention() async throws -> AttentionList { try await call("GET", "/attention") }
    func notifications() async throws -> NotificationSettings { try await call("GET", "/settings/notifications") }
    func bootstrap() async throws -> Bootstrap { try await call("POST", "/auth/bootstrap") }

    /// Always sends the current kinds: leaving them out resets the user's choice to all kinds.
    func setNotifications(_ settings: NotificationSettings) async throws -> NotificationSettings {
        try await call("PUT", "/settings/notifications", body: try JSONEncoder().encode(settings))
    }

    private func call<T: Decodable>(_ method: String, _ path: String, body: Data? = nil) async throws -> T {
        let req = Self.request(method: method, path: path, base: base, token: token, body: body)
        let data: Data, response: URLResponse
        do {
            (data, response) = try await session.data(for: req)
        } catch {
            throw APIError.unreachable
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 401 { throw APIError.unauthorized }
        guard (200..<300).contains(status) else { throw APIError.http(status, Self.errorMessage(data)) }
        return try JSONDecoder().decode(T.self, from: data)
    }
}

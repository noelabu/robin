//
//  Discovery.swift
//  Robin
//
//  Where the installed `robin` CLI and a running service's files are.
//

import Foundation
import Darwin

/// The user's home directory, even when the app's environment says otherwise.
var realHome: String {
    getpwuid(getuid()).flatMap { String(cString: $0.pointee.pw_dir, encoding: .utf8) }
        ?? NSHomeDirectory()
}

enum BinaryLocator {
    static func candidates(home: String) -> [String] {
        [
            "\(home)/.local/bin/robin",
            "/opt/homebrew/bin/robin",
            "/usr/local/bin/robin",
        ]
    }

    static func resolve(
        home: String = realHome,
        isExecutable: (String) -> Bool = { FileManager.default.isExecutableFile(atPath: $0) }
    ) -> URL? {
        candidates(home: home).first(where: isExecutable).map { URL(fileURLWithPath: $0) }
    }
}

enum ServiceFiles {
    /// `ROBIN_DATA_DIR` if set (a GUI app only sees it via `launchctl setenv`), else Robin's default.
    static func dataDir(env: [String: String] = ProcessInfo.processInfo.environment, home: String = realHome) -> URL {
        if let raw = env["ROBIN_DATA_DIR"], !raw.isEmpty {
            let expanded = raw.hasPrefix("~") ? home + raw.dropFirst() : raw
            return URL(fileURLWithPath: expanded).resolvingSymlinksInPath()
        }
        return URL(fileURLWithPath: "\(home)/Library/Application Support/Robin")
    }

    /// The service URL from `service.json`, only if it points at loopback (the token is never sent elsewhere).
    static func endpoint(dataDir: URL) -> URL? {
        guard let data = try? Data(contentsOf: dataDir.appendingPathComponent("service.json")),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let raw = json["url"] as? String,
              let url = URL(string: raw),
              url.scheme == "http",
              url.host == "127.0.0.1" || url.host == "localhost"
        else { return nil }
        return url
    }

    static func token(dataDir: URL) -> String? {
        guard let raw = try? String(contentsOf: dataDir.appendingPathComponent("cli.token"), encoding: .utf8) else {
            return nil
        }
        let token = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return token.isEmpty ? nil : token
    }
}

enum ChildEnvironment {
    /// The GUI environment with the usual CLI folders on PATH, so the service and its supervisor find claude/codex/pi.
    static func make(base: [String: String] = ProcessInfo.processInfo.environment, home: String = realHome) -> [String: String] {
        var env = base
        let front = ["\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "\(home)/.npm-global/bin"]
        let rest = (base["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin").split(separator: ":").map(String.init)
        var seen = Set<String>()
        env["PATH"] = (front + rest).filter { seen.insert($0).inserted }.joined(separator: ":")
        return env
    }
}

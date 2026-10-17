//
//  ServiceController.swift
//  Robin
//
//  Runs the state machine's effects: polls the service, spawns `robin serve`, stops it, retries.
//

import Foundation
import Observation
import Darwin

@MainActor
@Observable
final class ServiceController {
    private static let ownedKey = "robin.ownedServicePID"
    private static let pollInterval: Duration = .seconds(3)

    private(set) var state: ServiceState = .discovering
    private(set) var isFailing = false
    private(set) var health: Health?
    private(set) var attentionCount = 0
    private(set) var notifications: NotificationSettings?
    private(set) var stderrTail = ""
    /// Bumped when the dashboard must sign in again (the service restarted with a new pid).
    private(set) var reauthGeneration = 0

    private var machine: ServiceStateMachine
    private var child: Process?
    private var pollTask: Task<Void, Never>?
    private var retryTask: Task<Void, Never>?
    private var quitReply: (() -> Void)?
    let dataDir = ServiceFiles.dataDir()

    init() {
        let saved = UserDefaults.standard.integer(forKey: Self.ownedKey)
        machine = ServiceStateMachine(ownedPID: saved > 0 ? Int32(saved) : nil)
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.tick()
                try? await Task.sleep(for: Self.pollInterval)
            }
        }
    }

    var isRunning: Bool {
        if case .running = state { return true }
        return false
    }

    var api: RobinAPI? {
        guard let base = ServiceFiles.endpoint(dataDir: dataDir), let token = ServiceFiles.token(dataDir: dataDir) else {
            return nil
        }
        return RobinAPI(base: base, token: token)
    }

    // MARK: actions

    func restart() { send(.userRestart) }

    /// Calls `reply` once it's safe to terminate: at once, or after stopping a service the app started.
    func quit(reply: @escaping () -> Void) {
        quitReply = reply
        send(.quit)
    }

    func setNotifications(_ enabled: Bool) {
        guard let api, var settings = notifications else { return }
        settings.enabled = enabled
        notifications = settings
        Task {
            if let saved = try? await api.setNotifications(settings) { notifications = saved }
        }
    }

    func bootstrapURL() async -> URL? {
        guard isRunning, let api else { return nil }
        return try? await api.bootstrap().url
    }

    // MARK: the loop

    private func tick() async {
        send(.tick(installed: BinaryLocator.resolve() != nil))
    }

    private func send(_ event: ServiceEvent) {
        let effects = machine.handle(event)
        publish()
        for effect in effects { run(effect) }
    }

    private func publish() {
        state = machine.state
        isFailing = machine.isFailing
        if let pid = machine.ownedPID {
            UserDefaults.standard.set(Int(pid), forKey: Self.ownedKey)
        } else {
            UserDefaults.standard.removeObject(forKey: Self.ownedKey)
        }
    }

    private func run(_ effect: Effect) {
        switch effect {
        case .probe:
            Task { await probe() }
        case .spawn:
            spawn()
        case .stop(let pid):
            Task {
                await stop(pid)
                send(.stopFinished)
            }
        case .scheduleRetry(let delay):
            retryTask?.cancel()
            retryTask = Task { [weak self] in
                try? await Task.sleep(for: .seconds(delay))
                guard !Task.isCancelled else { return }
                self?.send(.retryDue)
            }
        case .reauthDashboard:
            reauthGeneration += 1
        case .terminate:
            quitReply?()
            quitReply = nil
        }
    }

    private func probe() async {
        guard let api else {
            send(.probe(.unreachable))
            return
        }
        do {
            let h = try await api.health()
            health = h
            send(.probe(.healthy(pid: h.pid)))
            attentionCount = (try? await api.attention().items.count) ?? attentionCount
            notifications = (try? await api.notifications()) ?? notifications
        } catch APIError.unauthorized {
            health = nil
            send(.probe(.unauthorized))
        } catch {
            health = nil
            send(.probe(.unreachable))
        }
    }

    private func spawn() {
        guard let binary = BinaryLocator.resolve() else { return }
        let proc = Process()
        let stderrPipe = Pipe()
        proc.executableURL = binary
        proc.arguments = ["serve", "--no-browser"]
        proc.currentDirectoryURL = URL(fileURLWithPath: realHome)
        proc.environment = ChildEnvironment.make()
        proc.standardInput = FileHandle.nullDevice
        proc.standardOutput = FileHandle.nullDevice  // only the one-time sign-in link, which shouldn't be kept
        proc.standardError = stderrPipe

        stderrPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let str = String(data: data, encoding: .utf8) else { return }
            let owner = self
            Task { @MainActor in owner?.appendStderr(str) }
        }
        proc.terminationHandler = { [weak self] p in
            let pid = p.processIdentifier
            let owner = self
            Task { @MainActor in
                guard let owner else { return }
                if owner.child?.processIdentifier == pid { owner.child = nil }
                owner.send(.childExited(pid: pid))
            }
        }

        do {
            try proc.run()
            child = proc
            stderrTail = ""
            send(.spawned(pid: proc.processIdentifier))
        } catch {
            send(.spawnFailed("couldn't start robin: \(error.localizedDescription)"))
        }
    }

    private func appendStderr(_ str: String) {
        stderrTail = String((stderrTail + str).suffix(8192))
    }

    /// The last non-empty line the service wrote to stderr, for the menu.
    var lastError: String? {
        stderrTail.split(separator: "\n").last.map(String.init)
    }

    /// SIGINT (a clean shutdown that removes service.json), again to cut a slow shutdown short, then SIGKILL.
    /// Only the service's pid: the supervisor runs in its own session and keeps agent sessions alive.
    private func stop(_ pid: Int32) async {
        for signal in [SIGINT, SIGINT, SIGKILL] {
            guard alive(pid) else { return }
            kill(pid, signal)
            for _ in 0..<30 {
                try? await Task.sleep(for: .milliseconds(100))
                if !alive(pid) { return }
            }
        }
    }

    private func alive(_ pid: Int32) -> Bool {
        if let child, child.processIdentifier == pid { return child.isRunning }
        return kill(pid, 0) == 0
    }
}

//
//  ServiceStateMachine.swift
//  Robin
//
//  The service's lifecycle as a pure value: events in, effects out. ServiceController runs the effects.
//

import Foundation

enum StopReason: Equatable {
    case restart
    case quit
}

enum ServiceState: Equatable {
    case notInstalled
    case discovering
    case starting(pid: Int32, since: Date)
    case running(pid: Int32, owned: Bool)
    case backoff(attempt: Int, delay: TimeInterval)
    case stopping(pid: Int32, then: StopReason)
    case error(String)
}

enum Probe: Equatable {
    case healthy(pid: Int32)
    case unreachable
    case unauthorized
}

enum ServiceEvent: Equatable {
    case tick(installed: Bool)
    case probe(Probe)
    case spawned(pid: Int32)
    case spawnFailed(String)
    case childExited(pid: Int32)
    case retryDue
    case userRestart
    case quit
    case stopFinished
}

enum Effect: Equatable {
    case probe
    case spawn
    case stop(pid: Int32)
    case scheduleRetry(TimeInterval)
    case reauthDashboard
    case terminate
}

enum Backoff {
    /// 1, 2, 4, 8, 16, 30, 30… seconds.
    static func delay(attempt: Int) -> TimeInterval {
        min(30, pow(2, Double(max(attempt, 1) - 1)))
    }
}

struct ServiceStateMachine {
    static let startTimeout: TimeInterval = 30
    static let healthyReset: TimeInterval = 60

    private(set) var state: ServiceState = .discovering
    /// The service the app started (persisted, so a relaunched app still recognises its own).
    var ownedPID: Int32?
    private(set) var attempt = 0
    private var unreachableStreak = 0
    private var healthySince: Date?

    init(ownedPID: Int32? = nil) {
        self.ownedPID = ownedPID
    }

    /// Red in the menu bar: an error, or the third restart in a row.
    var isFailing: Bool {
        switch state {
        case .error: return true
        case .backoff(let attempt, _): return attempt >= 3
        default: return false
        }
    }

    mutating func handle(_ event: ServiceEvent, now: Date = Date()) -> [Effect] {
        switch (event, state) {
        case (.tick(installed: false), .stopping):
            return []
        case (.tick(installed: false), _):
            state = .notInstalled
            return []
        case (.tick(installed: true), .notInstalled):
            state = .discovering
            return [.probe]
        case (.tick(installed: true), .discovering), (.tick(installed: true), .starting),
             (.tick(installed: true), .running), (.tick(installed: true), .error):
            return [.probe]
        case (.tick, _):
            return []

        case (.probe(let p), .discovering):
            switch p {
            case .healthy(let pid): return adopt(pid, now: now)
            case .unreachable: return [.spawn]
            case .unauthorized: return fail()
            }
        case (.probe(let p), .starting(let child, let since)):
            switch p {
            case .healthy(let pid): return adopt(pid, now: now)
            case .unauthorized: return fail()
            case .unreachable:
                guard now.timeIntervalSince(since) > Self.startTimeout else { return [] }
                return [.stop(pid: child)] + retry()
            }
        case (.probe(let p), .running(let current, _)):
            switch p {
            case .healthy(let pid):
                unreachableStreak = 0
                if let since = healthySince, now.timeIntervalSince(since) >= Self.healthyReset { attempt = 0 }
                if pid == current { return [] }
                return adopt(pid, now: now) + [.reauthDashboard]
            case .unauthorized:
                return fail()
            case .unreachable:
                unreachableStreak += 1
                guard unreachableStreak >= 2 else { return [] }
                state = .discovering
                return []
            }
        case (.probe(let p), .error):
            switch p {
            case .healthy(let pid): return adopt(pid, now: now)
            case .unreachable: state = .discovering; return []
            case .unauthorized: return []
            }
        case (.probe, _):
            return []

        case (.spawned(let pid), .discovering):
            ownedPID = pid
            state = .starting(pid: pid, since: now)
            return []
        case (.spawned, _):
            return []
        case (.spawnFailed(let message), _):
            state = .error(message)
            return []

        case (.childExited(let pid), .starting(let child, _)) where pid == child:
            return retry()
        case (.childExited(let pid), .running(let current, true)) where pid == current:
            return retry()
        case (.childExited, _):
            return []

        case (.retryDue, .backoff):
            state = .discovering
            return [.probe]
        case (.retryDue, _):
            return []

        case (.userRestart, .running(let pid, _)), (.userRestart, .starting(let pid, _)):
            state = .stopping(pid: pid, then: .restart)
            return [.stop(pid: pid)]
        case (.userRestart, .backoff), (.userRestart, .error), (.userRestart, .discovering):
            attempt = 0
            state = .discovering
            return [.probe]
        case (.userRestart, _):
            return []

        case (.quit, .running(let pid, true)), (.quit, .starting(let pid, _)):
            state = .stopping(pid: pid, then: .quit)
            return [.stop(pid: pid)]
        case (.quit, .stopping(let pid, _)):
            state = .stopping(pid: pid, then: .quit)
            return []
        case (.quit, _):
            return [.terminate]

        case (.stopFinished, .stopping(let pid, .restart)):
            if ownedPID == pid { ownedPID = nil }
            attempt = 0
            state = .discovering
            return [.probe]
        case (.stopFinished, .stopping(let pid, .quit)):
            if ownedPID == pid { ownedPID = nil }
            return [.terminate]
        case (.stopFinished, _):
            return []
        }
    }

    private mutating func adopt(_ pid: Int32, now: Date) -> [Effect] {
        if case .running(let current, _) = state, current == pid {} else { healthySince = now }
        unreachableStreak = 0
        state = .running(pid: pid, owned: pid == ownedPID)
        return []
    }

    private mutating func retry() -> [Effect] {
        attempt += 1
        let delay = Backoff.delay(attempt: attempt)
        state = .backoff(attempt: attempt, delay: delay)
        return [.scheduleRetry(delay)]
    }

    private mutating func fail() -> [Effect] {
        state = .error("cli.token doesn't match the running service")
        return []
    }
}

/// At most one new sign-in link every few seconds, under the service's limit of 10 exchanges a minute.
struct ReauthThrottle {
    static let interval: TimeInterval = 10
    private var last: Date?

    mutating func allow(now: Date = Date()) -> Bool {
        if let last, now.timeIntervalSince(last) < Self.interval { return false }
        last = now
        return true
    }
}

import XCTest

@MainActor
final class ServiceStateMachineTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_000_000)

    func testNotInstalledThenInstalled() {
        var m = ServiceStateMachine()
        XCTAssertEqual(m.handle(.tick(installed: false)), [])
        XCTAssertEqual(m.state, .notInstalled)
        XCTAssertEqual(m.handle(.tick(installed: true)), [.probe])
        XCTAssertEqual(m.state, .discovering)
    }

    func testAdoptsServiceStartedElsewhere() {
        var m = ServiceStateMachine()
        _ = m.handle(.probe(.healthy(pid: 50)))
        XCTAssertEqual(m.state, .running(pid: 50, owned: false))
    }

    func testRecognisesItsOwnServiceAfterRelaunch() {
        var m = ServiceStateMachine(ownedPID: 50)
        _ = m.handle(.probe(.healthy(pid: 50)))
        XCTAssertEqual(m.state, .running(pid: 50, owned: true))
    }

    func testSpawnsWhenNothingAnswers() {
        var m = ServiceStateMachine()
        XCTAssertEqual(m.handle(.probe(.unreachable)), [.spawn])
        _ = m.handle(.spawned(pid: 7), now: t0)
        XCTAssertEqual(m.state, .starting(pid: 7, since: t0))
        XCTAssertEqual(m.ownedPID, 7)
        _ = m.handle(.probe(.healthy(pid: 7)), now: t0 + 2)
        XCTAssertEqual(m.state, .running(pid: 7, owned: true))
    }

    func testTokenMismatchIsAnError() {
        var m = ServiceStateMachine()
        _ = m.handle(.probe(.unauthorized))
        guard case .error = m.state else { return XCTFail("\(m.state)") }
        XCTAssertTrue(m.isFailing)
    }

    func testStartTimeoutStopsTheChildAndRetries() {
        var m = ServiceStateMachine()
        _ = m.handle(.probe(.unreachable))
        _ = m.handle(.spawned(pid: 7), now: t0)
        XCTAssertEqual(m.handle(.probe(.unreachable), now: t0 + 10), [])
        XCTAssertEqual(m.handle(.probe(.unreachable), now: t0 + 31), [.stop(pid: 7), .scheduleRetry(1)])
        XCTAssertEqual(m.state, .backoff(attempt: 1, delay: 1))
    }

    func testCrashLoopBacksOffAndTurnsRed() {
        var m = ServiceStateMachine()
        var delays: [TimeInterval] = []
        for pid in Int32(1)...Int32(4) {
            _ = m.handle(.probe(.unreachable))
            _ = m.handle(.spawned(pid: pid))
            for case .scheduleRetry(let d) in m.handle(.childExited(pid: pid)) { delays.append(d) }
            XCTAssertEqual(m.handle(.retryDue), [.probe])
        }
        XCTAssertEqual(delays, [1, 2, 4, 8])
        _ = m.handle(.probe(.unreachable))
        _ = m.handle(.spawned(pid: 5))
        _ = m.handle(.childExited(pid: 5))
        XCTAssertTrue(m.isFailing)
    }

    func testBackoffDelays() {
        XCTAssertEqual((1...7).map(Backoff.delay(attempt:)), [1, 2, 4, 8, 16, 30, 30])
    }

    func testBackoffResetsAfterAMinuteHealthy() {
        var m = ServiceStateMachine()
        _ = m.handle(.probe(.unreachable))
        _ = m.handle(.spawned(pid: 1))
        _ = m.handle(.childExited(pid: 1))
        _ = m.handle(.retryDue)
        _ = m.handle(.probe(.unreachable))
        _ = m.handle(.spawned(pid: 2), now: t0)
        _ = m.handle(.probe(.healthy(pid: 2)), now: t0)
        XCTAssertEqual(m.attempt, 1)
        _ = m.handle(.probe(.healthy(pid: 2)), now: t0 + 61)
        XCTAssertEqual(m.attempt, 0)
    }

    func testOwnedServiceCrashRetries() {
        var m = ServiceStateMachine(ownedPID: 9)
        _ = m.handle(.probe(.healthy(pid: 9)))
        XCTAssertEqual(m.handle(.childExited(pid: 9)), [.scheduleRetry(1)])
    }

    func testExternalServiceThatDisappearsIsReplaced() {
        var m = ServiceStateMachine()
        _ = m.handle(.probe(.healthy(pid: 50)))
        _ = m.handle(.probe(.unreachable))
        XCTAssertEqual(m.state, .running(pid: 50, owned: false))
        _ = m.handle(.probe(.unreachable))
        XCTAssertEqual(m.state, .discovering)
        XCTAssertEqual(m.handle(.probe(.unreachable)), [.spawn])
    }

    func testNewPidSignsTheDashboardInAgain() {
        var m = ServiceStateMachine()
        _ = m.handle(.probe(.healthy(pid: 50)))
        XCTAssertEqual(m.handle(.probe(.healthy(pid: 50))), [])
        XCTAssertEqual(m.handle(.probe(.healthy(pid: 51))), [.reauthDashboard])
        XCTAssertEqual(m.state, .running(pid: 51, owned: false))
    }

    func testRestartStopsThenRediscovers() {
        var m = ServiceStateMachine()
        _ = m.handle(.probe(.healthy(pid: 50)))
        XCTAssertEqual(m.handle(.userRestart), [.stop(pid: 50)])
        XCTAssertEqual(m.handle(.childExited(pid: 50)), [])
        XCTAssertEqual(m.handle(.stopFinished), [.probe])
        XCTAssertEqual(m.state, .discovering)
    }

    func testQuitStopsOnlyAnOwnedService() {
        var owned = ServiceStateMachine(ownedPID: 9)
        _ = owned.handle(.probe(.healthy(pid: 9)))
        XCTAssertEqual(owned.handle(.quit), [.stop(pid: 9)])
        XCTAssertEqual(owned.handle(.childExited(pid: 9)), [])
        XCTAssertEqual(owned.handle(.stopFinished), [.terminate])
        XCTAssertNil(owned.ownedPID)

        var external = ServiceStateMachine()
        _ = external.handle(.probe(.healthy(pid: 50)))
        XCTAssertEqual(external.handle(.quit), [.terminate])
    }

    func testQuitDuringRestartStillTerminates() {
        var m = ServiceStateMachine(ownedPID: 9)
        _ = m.handle(.probe(.healthy(pid: 9)))
        _ = m.handle(.userRestart)
        XCTAssertEqual(m.handle(.quit), [])
        XCTAssertEqual(m.handle(.stopFinished), [.terminate])
    }

    func testReauthThrottle() {
        var throttle = ReauthThrottle()
        XCTAssertTrue(throttle.allow(now: t0))
        XCTAssertFalse(throttle.allow(now: t0 + 5))
        XCTAssertTrue(throttle.allow(now: t0 + 11))
    }
}

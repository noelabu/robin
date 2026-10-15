"""Local authentication for the browser and the CLI (TR-051, TR-052).

Browser: the service prints a URL with a single-use bootstrap secret in the fragment. The page exchanges
it for an HttpOnly, SameSite=Strict session cookie plus a CSRF token for mutations. The secret has 256
bits, expires after 60 s, and is spent by any exchange attempt, right or wrong; exchanges are rate-limited.
CLI: a 0600 token file in the data directory, sent as a bearer token. Neither path bypasses task policy.
Every request must carry a loopback Host, and a browser Origin, if present, must be this service.
"""

import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from robin.paths import secure_touch

BOOTSTRAP_TTL = 60.0
SESSION_TTL = 12 * 3600.0
EXCHANGE_LIMIT = 10  # attempts per minute
COOKIE = "robin_session"
CSRF_HEADER = "x-robin-csrf"


@dataclass
class _Session:
    csrf: str
    expires: float


@dataclass
class Auth:
    port: int
    cli_token: str
    _bootstrap: str | None = None
    _bootstrap_expires: float = 0.0
    _sessions: dict[str, _Session] = field(default_factory=dict)
    _attempts: list[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def for_data_dir(cls, data_dir: Path, port: int) -> "Auth":
        return cls(port=port, cli_token=load_cli_token(data_dir))

    # -- bootstrap ---------------------------------------------------------

    def new_bootstrap(self) -> str:
        with self._lock:
            self._bootstrap = secrets.token_urlsafe(32)
            self._bootstrap_expires = time.monotonic() + BOOTSTRAP_TTL
            return self._bootstrap

    def exchange(self, secret: str) -> tuple[str, str] | None:
        """Spend the bootstrap secret. Returns (session id, csrf token) on success."""
        now = time.monotonic()
        with self._lock:
            self._attempts = [t for t in self._attempts if now - t < 60]
            if len(self._attempts) >= EXCHANGE_LIMIT:
                return None
            self._attempts.append(now)
            expected, expires = self._bootstrap, self._bootstrap_expires
            self._bootstrap = None  # any attempt spends it
            if expected is None or now > expires or not hmac.compare_digest(expected, secret):
                return None
            sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self._sessions[sid] = _Session(csrf, now + SESSION_TTL)
            return sid, csrf

    # -- per request -------------------------------------------------------

    def browser_session(self, cookie: str | None) -> _Session | None:
        if not cookie:
            return None
        with self._lock:
            s = self._sessions.get(cookie)
            if s is None or time.monotonic() > s.expires:
                self._sessions.pop(cookie, None)
                return None
            return s

    def cli_ok(self, authorization: str | None) -> bool:
        if not authorization or not authorization.startswith("Bearer "):
            return False
        return hmac.compare_digest(authorization.removeprefix("Bearer "), self.cli_token)

    def host_ok(self, host: str | None) -> bool:
        return host in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def origin_ok(self, origin: str | None) -> bool:
        return origin is None or origin in {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}


def load_cli_token(data_dir: Path) -> str:
    """The CLI's credential: created once, readable only by the owner."""
    path = data_dir / "cli.token"
    if path.exists() and path.read_text().strip():
        return path.read_text().strip()
    secure_touch(path)
    token = secrets.token_urlsafe(32)
    path.write_text(token)
    return token

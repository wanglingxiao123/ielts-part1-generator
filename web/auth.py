"""Self-hosted accounts: password hashing, a stateless session cookie, and a user store.

No AWS is involved in *authenticating* anyone. Cognito was ruled out for this deployment because
its hosted login refuses HTTP callbacks for any host but localhost (deploy-plan.md), and the
deployment is plain HTTP on a Fargate task IP. So the web tier owns its own accounts.

Three decisions worth stating, because each of them is the kind of thing that looks arbitrary
later:

1. **The session token is stateless.** It is `payload.signature`, HMAC-SHA256 over the payload,
   and nothing is stored server-side. That means logout is client-side only (the cookie is
   cleared) and a stolen token stays valid until it expires -- acceptable for an internal tool,
   and it removes a whole class of "which instance holds my session" bugs.

2. **A shared user store implies a shared signing key.** If two instances each invent their own
   random key, a cookie minted by A is garbage to B and the user is silently logged out on every
   other request. That failure is invisible in a single-instance test and obvious only in
   production, so `build_auth` refuses to start instead. See `SigningKeyUnavailable`.

3. **The email-domain allowlist is checked at registration and nowhere else.** Narrowing the list
   later must not lock out accounts that already exist; that is a deliberate trade, not an
   oversight. Removing an account is a store edit, not an env-var change.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Protocol

# 200_000 rounds of PBKDF2-HMAC-SHA256. Measured ~55ms per verify on the deployment's ARM64
# Fargate size, which is a tolerable cost on a login-only path.
PBKDF2_ITERATIONS = 200_000
PBKDF2_ALGORITHM = "pbkdf2_sha256"
SALT_BYTES = 16

SESSION_COOKIE = "ielts_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600


class AuthError(Exception):
    """Base for everything in this module that a caller is expected to handle.

    ``code`` is what the HTTP layer turns into the frontend's error code, so it is part of the
    contract rather than a log string.
    """

    code = "auth_error"
    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class EmailNotAllowed(AuthError):
    code = "EMAIL_DOMAIN_NOT_ALLOWED"
    status = 403


class UserExists(AuthError):
    code = "USER_EXISTS"
    status = 409


class InvalidCredentials(AuthError):
    code = "INVALID_CREDENTIALS"
    status = 401


class InvalidSession(AuthError):
    code = "INVALID_SESSION"
    status = 401


class WeakPassword(AuthError):
    code = "WEAK_PASSWORD"
    status = 400


class SigningKeyUnavailable(RuntimeError):
    """Raised at startup, never at request time. See decision 2 in the module docstring."""


# ── password hashing ─────────────────────────────────────────────────────────


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, salt: Optional[bytes] = None,
                  iterations: int = PBKDF2_ITERATIONS) -> str:
    """``pbkdf2_sha256$<iterations>$<salt>$<derived>``, all base64url, no padding.

    The iteration count travels with the hash so raising `PBKDF2_ITERATIONS` later does not
    invalidate stored passwords: old hashes keep verifying at their own cost.
    """
    if not password:
        raise WeakPassword("password must not be empty")
    salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "%s$%d$%s$%s" % (PBKDF2_ALGORITHM, iterations, _b64(salt), _b64(derived))


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time compare. A malformed stored hash is a False, not an exception.

    Treating a corrupt record as "wrong password" keeps a single bad row in the user file from
    turning into a 500 on the login path.
    """
    try:
        algorithm, rounds, salt_b64, digest_b64 = encoded.split("$")
    except (ValueError, AttributeError):
        return False
    if algorithm != PBKDF2_ALGORITHM:
        return False
    try:
        iterations = int(rounds)
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


# ── the stateless session token ──────────────────────────────────────────────


class SessionSigner:
    """Mints and validates `payload.signature` session tokens.

    Expiry lives inside the signed payload, so a client cannot extend its own session by editing
    the cookie -- any edit breaks the signature first.
    """

    def __init__(self, key: bytes, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        if not key:
            raise ValueError("signing key must not be empty")
        self._key = key
        self.ttl_seconds = ttl_seconds

    def _sign(self, payload: bytes) -> str:
        return _b64(hmac.new(self._key, payload, hashlib.sha256).digest())

    def issue(self, email: str, *, now: Optional[float] = None) -> str:
        issued = int(now if now is not None else time.time())
        body = json.dumps(
            {"sub": email, "iat": issued, "exp": issued + self.ttl_seconds},
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        return "%s.%s" % (_b64(body), self._sign(body))

    def verify(self, token: str, *, now: Optional[float] = None) -> str:
        """Returns the email, or raises `InvalidSession`.

        Signature is checked before the payload is parsed or trusted for anything, including
        expiry: reading `exp` off an unverified payload would let a client pick its own.
        """
        if not token or "." not in token:
            raise InvalidSession("malformed session token")
        payload_b64, signature = token.rsplit(".", 1)
        try:
            payload = _unb64(payload_b64)
        except (ValueError, TypeError):
            raise InvalidSession("malformed session token")
        if not hmac.compare_digest(self._sign(payload), signature):
            raise InvalidSession("session signature mismatch")
        try:
            body = json.loads(payload.decode("utf-8"))
            email = str(body["sub"])
            expires = int(body["exp"])
        except (ValueError, KeyError, TypeError):
            raise InvalidSession("malformed session payload")
        if (now if now is not None else time.time()) >= expires:
            raise InvalidSession("session expired")
        return email


# ── the user store ───────────────────────────────────────────────────────────


class UserStore(Protocol):
    """Deliberately four methods. Anything wider invites business logic into the backend.

    ``shared`` is not storage at all -- it answers "can another process be serving the same
    users", which is what decides whether a per-process signing key is safe.
    """

    shared: bool

    def get(self, email: str) -> Optional[Dict[str, Any]]: ...
    def put(self, user: Dict[str, Any]) -> None: ...
    def count(self) -> int: ...
    def list_emails(self) -> List[str]: ...


class _DictBacked:
    """Shared read/modify/write plumbing for both concrete stores.

    Subclasses provide `_read`/`_write` over a single JSON blob. One blob rather than one object
    per user because the store is also the "how many users exist" authority (first user is
    admin), and counting objects in S3 is a list call that can lag.
    """

    shared = False

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def _read(self) -> Dict[str, Dict[str, Any]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _write(self, users: Dict[str, Dict[str, Any]]) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def get(self, email: str) -> Optional[Dict[str, Any]]:
        return self._read().get(normalise_email(email))

    def put(self, user: Dict[str, Any]) -> None:
        with self._lock:
            users = self._read()
            users[normalise_email(str(user["email"]))] = dict(user)
            self._write(users)

    def count(self) -> int:
        return len(self._read())

    def list_emails(self) -> List[str]:
        return sorted(self._read())


class MemoryUserStore(_DictBacked):
    """For tests and `--reload` dev runs. Not shared: nothing survives the process."""

    shared = False

    def __init__(self) -> None:
        super().__init__()
        self._users: Dict[str, Dict[str, Any]] = {}

    def _read(self) -> Dict[str, Dict[str, Any]]:
        return self._users

    def _write(self, users: Dict[str, Dict[str, Any]]) -> None:
        self._users = users


class JsonFileUserStore(_DictBacked):
    """A local JSON file. Single-instance by construction, hence `shared = False`."""

    shared = False

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise RuntimeError("user store %s is not valid JSON: %s" % (self.path, exc))
        return {str(k): dict(v) for k, v in (data.get("users") or {}).items()}

    def _write(self, users: Dict[str, Dict[str, Any]]) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp = "%s.%s.tmp" % (self.path, os.getpid())
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"users": users}, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temp, self.path)  # atomic, so a crash mid-write cannot truncate the file


class S3UserStore(_DictBacked):
    """One JSON object in S3. `shared = True`: any number of tasks read the same accounts.

    boto3 is imported in `__init__`, not at module scope, mirroring
    `audio_storage/object_store.py`: importing this module must never require AWS.
    """

    shared = True

    def __init__(self, bucket: str, key: str = "web/users.json", *, client: Any = None) -> None:
        super().__init__()
        if client is None:
            import boto3  # noqa: PLC0415 - lazy on purpose

            client = boto3.client("s3")
        self._client = client
        self.bucket = bucket
        self.key = key

    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            body = self._client.get_object(Bucket=self.bucket, Key=self.key)["Body"].read()
        except Exception as exc:  # noqa: BLE001 - narrowed by error code below
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return {}
            raise
        data = json.loads(body.decode("utf-8"))
        return {str(k): dict(v) for k, v in (data.get("users") or {}).items()}

    def _write(self, users: Dict[str, Dict[str, Any]]) -> None:
        self._client.put_object(
            Bucket=self.bucket, Key=self.key,
            Body=json.dumps({"users": users}, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
        )


# ── email domain policy ──────────────────────────────────────────────────────


def normalise_email(email: str) -> str:
    return str(email or "").strip().lower()


def parse_allowed_domains(raw: Optional[str]) -> List[str]:
    """``"amazon.com, qq.com"`` -> ``["amazon.com", "qq.com"]``; ``"*"`` -> ``["*"]``.

    An unset or blank value yields ``["*"]``: a fresh dev run should not fail closed on a
    variable nobody set. The deployment sets it explicitly.
    """
    if raw is None or not raw.strip():
        return ["*"]
    domains = [part.strip().lower() for part in raw.split(",")]
    return [d for d in domains if d] or ["*"]


def email_domain_allowed(email: str, allowed: List[str]) -> bool:
    if "*" in allowed:
        return True
    address = normalise_email(email)
    if address.count("@") != 1:
        return False
    return address.rsplit("@", 1)[1] in allowed


# ── the service ──────────────────────────────────────────────────────────────

MIN_PASSWORD_LENGTH = 8


class AuthService:
    """Registration, login, and "who is this cookie". The only stateful part is the store."""

    def __init__(self, store: UserStore, signer: SessionSigner,
                 allowed_domains: Optional[List[str]] = None) -> None:
        self.store = store
        self.signer = signer
        self.allowed_domains = allowed_domains or ["*"]

    def register(self, email: str, password: str) -> Dict[str, Any]:
        address = normalise_email(email)
        if "@" not in address or address.startswith("@") or address.endswith("@"):
            raise EmailNotAllowed("%r is not an email address" % email)
        if not email_domain_allowed(address, self.allowed_domains):
            raise EmailNotAllowed(
                "邮箱域名不在允许列表内（当前允许：%s）" % ", ".join(self.allowed_domains)
            )
        if len(password or "") < MIN_PASSWORD_LENGTH:
            raise WeakPassword("密码至少 %d 位" % MIN_PASSWORD_LENGTH)
        if self.store.get(address) is not None:
            raise UserExists("该邮箱已注册")
        # The first account to exist is the admin. Checked against the store rather than a flag
        # so a wiped store re-bootstraps instead of ending up with no admin at all.
        is_admin = self.store.count() == 0
        user = {
            "email": address,
            "password_hash": hash_password(password),
            "is_admin": is_admin,
            "created_at": int(time.time()),
        }
        self.store.put(user)
        return self.public(user)

    def login(self, email: str, password: str) -> Dict[str, Any]:
        address = normalise_email(email)
        user = self.store.get(address)
        if user is None or not verify_password(password, str(user.get("password_hash", ""))):
            # One message for both branches: distinguishing them tells an attacker which
            # addresses are registered.
            raise InvalidCredentials("邮箱或密码不正确")
        return self.public(user)

    def issue_token(self, email: str) -> str:
        return self.signer.issue(normalise_email(email))

    def identify(self, token: Optional[str]) -> Dict[str, Any]:
        """Cookie -> user record. Raises `InvalidSession` for tampered, expired, or deleted.

        A valid signature over a user who no longer exists is still a rejection: deleting a row
        from the store is the only revocation this design has.
        """
        if not token:
            raise InvalidSession("no session cookie")
        email = self.signer.verify(token)
        user = self.store.get(email)
        if user is None:
            raise InvalidSession("session refers to an unknown account")
        return self.public(user)

    @staticmethod
    def public(user: Dict[str, Any]) -> Dict[str, Any]:
        """The shape sent to the browser. `password_hash` must never appear here."""
        return {
            "email": user["email"],
            "is_admin": bool(user.get("is_admin")),
            "created_at": user.get("created_at"),
        }


# ── construction from the environment ────────────────────────────────────────


def build_user_store(env: Optional[Dict[str, str]] = None) -> UserStore:
    """`USER_STORE_S3_BUCKET` picks S3; otherwise a JSON file at `USER_STORE_PATH`."""
    env = os.environ if env is None else env
    bucket = (env.get("USER_STORE_S3_BUCKET") or "").strip()
    if bucket:
        return S3UserStore(bucket, (env.get("USER_STORE_S3_KEY") or "web/users.json").strip())
    return JsonFileUserStore((env.get("USER_STORE_PATH") or "/tmp/ielts-web-users.json").strip())


def build_signer(store: UserStore, env: Optional[Dict[str, str]] = None) -> SessionSigner:
    """`SESSION_SECRET` if set; a per-process random key only when the store is not shared.

    The `SigningKeyUnavailable` branch is the whole point of this function. Falling back to a
    random key with an S3 store produces a system that works perfectly on one task and drops
    roughly half of all logins on two, with no error anywhere -- so it fails at startup instead.
    """
    env = os.environ if env is None else env
    secret = (env.get("SESSION_SECRET") or "").strip()
    if secret:
        return SessionSigner(secret.encode("utf-8"))
    if getattr(store, "shared", False):
        raise SigningKeyUnavailable(
            "SESSION_SECRET is required when the user store is shared (%s): a per-process "
            "random key would make session cookies invalid on every other instance and "
            "silently log users out. Set SESSION_SECRET in the task definition."
            % type(store).__name__
        )
    return SessionSigner(secrets.token_bytes(32))


def build_auth(env: Optional[Dict[str, str]] = None,
               store: Optional[UserStore] = None) -> AuthService:
    env = os.environ if env is None else env
    resolved = build_user_store(env) if store is None else store
    return AuthService(
        resolved,
        build_signer(resolved, env),
        parse_allowed_domains(env.get("ALLOWED_EMAIL_DOMAINS")),
    )

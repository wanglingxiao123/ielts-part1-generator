"""Password hashing, the session cookie, the domain allowlist, and the startup check."""

from __future__ import annotations

import json
import time

import pytest

from web.auth import (
    PBKDF2_ITERATIONS,
    AuthService,
    EmailNotAllowed,
    InvalidCredentials,
    InvalidSession,
    JsonFileUserStore,
    MemoryUserStore,
    SessionSigner,
    SigningKeyUnavailable,
    UserExists,
    WeakPassword,
    build_signer,
    email_domain_allowed,
    hash_password,
    parse_allowed_domains,
    verify_password,
)


# ── password hashing ─────────────────────────────────────────────────────────


def test_hash_verify_round_trip():
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("Correct horse battery staple", encoded)
    assert not verify_password("", encoded)


def test_hash_records_algorithm_and_iterations():
    algorithm, rounds, salt, digest = hash_password("pw").split("$")
    assert algorithm == "pbkdf2_sha256"
    assert int(rounds) == PBKDF2_ITERATIONS == 200_000
    assert salt and digest


def test_salt_is_per_user():
    """Two identical passwords must not produce identical hashes."""
    assert hash_password("same-password") != hash_password("same-password")


def test_hash_verifies_at_its_own_iteration_count():
    """Raising PBKDF2_ITERATIONS later must not invalidate existing passwords."""
    old = hash_password("pw", iterations=1000)
    assert "$1000$" in old
    assert verify_password("pw", old)


def test_malformed_hash_is_a_rejection_not_a_crash():
    for broken in ["", "garbage", "pbkdf2_sha256$notanint$a$b", "sha1$1$a$b", "a$b$c"]:
        assert verify_password("pw", broken) is False


def test_empty_password_refused_at_hash_time():
    with pytest.raises(WeakPassword):
        hash_password("")


# ── the session token ────────────────────────────────────────────────────────


def test_session_round_trip():
    signer = SessionSigner(b"k" * 32)
    assert signer.verify(signer.issue("a@b.com")) == "a@b.com"


def test_tampered_payload_rejected():
    """Editing the subject invalidates the signature -- the point of signing it."""
    signer = SessionSigner(b"k" * 32)
    token = signer.issue("user@amazon.com")
    payload, signature = token.rsplit(".", 1)
    forged = SessionSigner(b"k" * 32).issue("admin@amazon.com").rsplit(".", 1)[0]
    with pytest.raises(InvalidSession):
        signer.verify("%s.%s" % (forged, signature))


def test_tampered_signature_rejected():
    signer = SessionSigner(b"k" * 32)
    payload, signature = signer.issue("a@b.com").rsplit(".", 1)
    with pytest.raises(InvalidSession):
        signer.verify("%s.%sX" % (payload, signature[:-1]))


def test_token_from_a_different_key_rejected():
    """The multi-instance failure mode, reproduced: key A's cookie is garbage to key B."""
    token = SessionSigner(b"instance-a-random-key").issue("a@b.com")
    with pytest.raises(InvalidSession):
        SessionSigner(b"instance-b-random-key").verify(token)


def test_expired_token_rejected():
    signer = SessionSigner(b"k" * 32, ttl_seconds=60)
    now = time.time()
    token = signer.issue("a@b.com", now=now)
    assert signer.verify(token, now=now + 59) == "a@b.com"
    with pytest.raises(InvalidSession):
        signer.verify(token, now=now + 61)


def test_client_cannot_extend_its_own_expiry():
    """`exp` lives inside the signed payload, so rewriting it breaks the signature."""
    import base64

    signer = SessionSigner(b"k" * 32, ttl_seconds=10)
    now = time.time()
    token = signer.issue("a@b.com", now=now)
    payload_b64, signature = token.rsplit(".", 1)
    raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    body = json.loads(raw)
    body["exp"] = int(now) + 10_000_000
    forged = base64.urlsafe_b64encode(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")
    with pytest.raises(InvalidSession):
        signer.verify("%s.%s" % (forged, signature), now=now + 100)


def test_malformed_tokens_rejected():
    signer = SessionSigner(b"k" * 32)
    for broken in ["", "nodot", "!!!.!!!", "."]:
        with pytest.raises(InvalidSession):
            signer.verify(broken)


def test_empty_signing_key_refused():
    with pytest.raises(ValueError):
        SessionSigner(b"")


# ── the SESSION_SECRET startup check ─────────────────────────────────────────


class _SharedStore(MemoryUserStore):
    shared = True


def test_shared_store_without_session_secret_refuses_to_start():
    """The check that exists so multi-instance never silently drops logins."""
    with pytest.raises(SigningKeyUnavailable) as caught:
        build_signer(_SharedStore(), {})
    assert "SESSION_SECRET" in str(caught.value)


def test_shared_store_with_session_secret_starts():
    signer = build_signer(_SharedStore(), {"SESSION_SECRET": "shared-secret"})
    assert signer.verify(signer.issue("a@b.com")) == "a@b.com"


def test_secret_is_the_key_so_two_instances_agree():
    """Same SESSION_SECRET, two independently built signers, one accepted cookie."""
    env = {"SESSION_SECRET": "the-same-secret"}
    a = build_signer(_SharedStore(), env)
    b = build_signer(_SharedStore(), env)
    assert b.verify(a.issue("user@amazon.com")) == "user@amazon.com"


def test_local_store_falls_back_to_a_random_key():
    """Single-instance dev needs no secret; the store is not shared, so nothing can disagree."""
    signer = build_signer(MemoryUserStore(), {})
    assert signer.verify(signer.issue("a@b.com")) == "a@b.com"


def test_blank_session_secret_counts_as_unset():
    with pytest.raises(SigningKeyUnavailable):
        build_signer(_SharedStore(), {"SESSION_SECRET": "   "})


# ── the email allowlist ──────────────────────────────────────────────────────


def test_parse_allowed_domains():
    assert parse_allowed_domains("amazon.com,qq.com") == ["amazon.com", "qq.com"]
    assert parse_allowed_domains(" amazon.com , QQ.com ") == ["amazon.com", "qq.com"]
    assert parse_allowed_domains("*") == ["*"]
    assert parse_allowed_domains(None) == ["*"]
    assert parse_allowed_domains("") == ["*"]


def test_domain_matching():
    allowed = ["amazon.com", "qq.com"]
    assert email_domain_allowed("a@amazon.com", allowed)
    assert email_domain_allowed("A@AMAZON.COM", allowed)
    assert email_domain_allowed("a@qq.com", allowed)
    assert not email_domain_allowed("a@gmail.com", allowed)
    # A subdomain is a different domain; matching it would let anyone with
    # amazon.com.evil.example register.
    assert not email_domain_allowed("a@mail.amazon.com", allowed)
    assert not email_domain_allowed("a@amazon.com.evil.example", allowed)
    assert not email_domain_allowed("not-an-email", allowed)
    assert not email_domain_allowed("a@b@amazon.com", allowed)


def test_star_disables_the_check():
    assert email_domain_allowed("anyone@anywhere.test", ["*"])


def test_register_enforces_the_allowlist():
    service = AuthService(MemoryUserStore(), SessionSigner(b"k" * 32), ["amazon.com"])
    service.register("ok@amazon.com", "hunter2hunter2")
    with pytest.raises(EmailNotAllowed):
        service.register("no@gmail.com", "hunter2hunter2")


def test_narrowing_the_allowlist_does_not_lock_out_existing_accounts():
    """Deliberate: the check happens at registration only (deploy-plan.md)."""
    store = MemoryUserStore()
    AuthService(store, SessionSigner(b"k" * 32), ["qq.com"]).register("a@qq.com", "hunter2hunter2")
    narrowed = AuthService(store, SessionSigner(b"k" * 32), ["amazon.com"])
    assert narrowed.login("a@qq.com", "hunter2hunter2")["email"] == "a@qq.com"
    assert narrowed.identify(narrowed.issue_token("a@qq.com"))["email"] == "a@qq.com"


# ── registration and login ───────────────────────────────────────────────────


def test_first_user_is_admin_and_the_rest_are_not(auth: AuthService):
    assert auth.register("first@amazon.com", "hunter2hunter2")["is_admin"] is True
    assert auth.register("second@amazon.com", "hunter2hunter2")["is_admin"] is False
    assert auth.register("third@amazon.com", "hunter2hunter2")["is_admin"] is False


def test_admin_is_rebootstrapped_on_an_empty_store():
    """Checked against the store, not a flag, so a wiped store is not left with no admin."""
    store = MemoryUserStore()
    service = AuthService(store, SessionSigner(b"k" * 32), ["*"])
    service.register("a@amazon.com", "hunter2hunter2")
    store._users.clear()
    assert service.register("b@amazon.com", "hunter2hunter2")["is_admin"] is True


def test_public_view_never_leaks_the_hash(auth: AuthService):
    user = auth.register("a@amazon.com", "hunter2hunter2")
    assert "password_hash" not in user
    assert "password_hash" not in auth.login("a@amazon.com", "hunter2hunter2")
    assert "password_hash" not in auth.identify(auth.issue_token("a@amazon.com"))


def test_duplicate_registration_refused(auth: AuthService):
    auth.register("a@amazon.com", "hunter2hunter2")
    with pytest.raises(UserExists):
        auth.register("A@Amazon.com", "different-password")


def test_short_password_refused(auth: AuthService):
    with pytest.raises(WeakPassword):
        auth.register("a@amazon.com", "short")


def test_non_email_refused(auth: AuthService):
    for bad in ["", "nobody", "@amazon.com", "nobody@"]:
        with pytest.raises(EmailNotAllowed):
            auth.register(bad, "hunter2hunter2")


def test_wrong_password_and_unknown_user_are_indistinguishable(auth: AuthService):
    auth.register("a@amazon.com", "hunter2hunter2")
    with pytest.raises(InvalidCredentials) as wrong:
        auth.login("a@amazon.com", "nope-nope-nope")
    with pytest.raises(InvalidCredentials) as missing:
        auth.login("ghost@amazon.com", "nope-nope-nope")
    assert str(wrong.value) == str(missing.value)


def test_email_is_case_and_space_insensitive(auth: AuthService):
    auth.register("  User@Amazon.COM ", "hunter2hunter2")
    assert auth.login("user@amazon.com", "hunter2hunter2")["email"] == "user@amazon.com"


def test_session_for_a_deleted_account_is_rejected(auth: AuthService, store: MemoryUserStore):
    """Deleting the row is this design's only revocation, so it has to work."""
    auth.register("a@amazon.com", "hunter2hunter2")
    token = auth.issue_token("a@amazon.com")
    assert auth.identify(token)["email"] == "a@amazon.com"
    store._users.clear()
    with pytest.raises(InvalidSession):
        auth.identify(token)


def test_identify_without_a_token(auth: AuthService):
    with pytest.raises(InvalidSession):
        auth.identify(None)


# ── the JSON file store ──────────────────────────────────────────────────────


def test_json_file_store_persists_across_instances(tmp_path):
    path = str(tmp_path / "nested" / "users.json")
    AuthService(JsonFileUserStore(path), SessionSigner(b"k" * 32), ["*"]).register(
        "a@amazon.com", "hunter2hunter2"
    )
    reopened = AuthService(JsonFileUserStore(path), SessionSigner(b"k" * 32), ["*"])
    assert reopened.login("a@amazon.com", "hunter2hunter2")["is_admin"] is True
    assert reopened.store.list_emails() == ["a@amazon.com"]


def test_json_file_store_is_not_shared(tmp_path):
    assert JsonFileUserStore(str(tmp_path / "u.json")).shared is False


def test_missing_file_reads_as_empty(tmp_path):
    assert JsonFileUserStore(str(tmp_path / "absent.json")).count() == 0


def test_corrupt_file_is_a_clear_error(tmp_path):
    path = tmp_path / "users.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        JsonFileUserStore(str(path)).count()

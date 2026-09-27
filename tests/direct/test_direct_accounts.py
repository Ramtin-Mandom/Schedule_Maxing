"""Accounts through the direct path (backend/accounts.py via DirectBackend):
the API's normalization and uniqueness rules, a registration race, one
generic failure for unknown accounts and wrong passwords, hash upgrades,
identity output without secrets, secret-free logs, and scope bound to the
verified account."""

from __future__ import annotations

import dataclasses
import logging
import threading
import uuid

import pytest
from argon2 import PasswordHasher
from sqlalchemy import select, update

from app.persistence.direct import AccountSession, DirectBackend
from app.persistence.errors import (
    AccountExistsError,
    AccountValidationError,
    InvalidCredentialsError,
    NotSignedInError,
)
from app.planning.errors import ScopeError
from app.planning.models import Task
from app.planning.scope import OwnerScope
from backend import models
from backend.database import create_backend_engine, session_factory
from backend.migrate import upgrade
from backend.passwords import needs_rehash
from tests.direct.conftest import PASSWORD, account


def stored_hash(engine, email: str) -> str:
    with session_factory(engine)() as session:
        return session.scalars(select(models.User.password_hash).where(models.User.email == email)).one()


def test_registration_normalizes_and_enforces_uniqueness(backend) -> None:
    identity = backend.register(email="  Ｒamtin.Test@Example.COM ", password=PASSWORD, username="Ramtin_1",
                                display_name="  Ramtin ")
    assert (identity.email, identity.username, identity.display_name, identity.version) == (
        "ramtin.test@example.com", "ramtin_1", "Ramtin", 1)
    for duplicate in ({"email": "RAMTIN.test@example.com"}, {"email": "other@example.com", "username": "RAMTIN_1"}):
        with pytest.raises(AccountExistsError):
            backend.register(password=PASSWORD, **duplicate)
    for invalid in ({"email": "not-an-email"}, {"email": "x@example.com", "password": "short"},
                    {"email": "y@example.com", "username": "bad name"}):
        with pytest.raises(AccountValidationError):
            backend.register(**{"password": PASSWORD, **invalid})


def test_concurrent_registrations_create_exactly_one_account(backend, engine) -> None:
    outcomes: list[str] = []
    barrier = threading.Barrier(6)

    def attempt() -> None:
        barrier.wait()
        try:
            backend.register(email="race@example.com", password=PASSWORD)
            outcomes.append("created")
        except AccountExistsError:
            outcomes.append("exists")

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(outcomes) == ["created"] + ["exists"] * 5
    with session_factory(engine)() as session:
        assert len(session.scalars(select(models.User).where(models.User.email == "race@example.com")).all()) == 1


def test_sign_in_fails_identically_for_unknown_accounts_and_wrong_passwords(backend) -> None:
    backend.register(email="real@example.com", password=PASSWORD, username="real")
    messages = []
    for credentials in ({"email": "real@example.com", "password": "wrong password"},
                        {"email": "nobody@example.com", "password": PASSWORD},
                        {"username": "nobody", "password": PASSWORD}):
        with pytest.raises(InvalidCredentialsError) as raised:
            backend.sign_in(**credentials)
        messages.append(str(raised.value))
    assert len(set(messages)) == 1
    assert backend.sign_in(email=" REAL@example.com", password=PASSWORD).identity.email == "real@example.com"
    assert backend.sign_in(username="REAL", password=PASSWORD).identity.username == "real"


def test_an_outdated_hash_is_upgraded_on_sign_in(backend, engine) -> None:
    backend.register(email="old@example.com", password=PASSWORD)
    weak = PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1).hash(PASSWORD)
    with session_factory(engine)() as session:
        session.execute(update(models.User).where(models.User.email == "old@example.com").values(password_hash=weak))
        session.commit()
    assert needs_rehash(stored_hash(engine, "old@example.com"))

    backend.sign_in(email="old@example.com", password=PASSWORD)
    upgraded = stored_hash(engine, "old@example.com")
    assert upgraded != weak and upgraded.startswith("$argon2id$") and not needs_rehash(upgraded)
    assert backend.sign_in(email="old@example.com", password=PASSWORD).identity.email == "old@example.com"


def test_identities_carry_no_secret_and_no_orm_object(backend, engine) -> None:
    identity = backend.register(email="safe@example.com", password=PASSWORD)
    session = backend.sign_in(email="safe@example.com", password=PASSWORD)
    fields = {field.name for field in dataclasses.fields(identity)}
    assert fields == {"id", "email", "username", "display_name", "version", "created_at", "updated_at"}
    for value in (repr(identity), repr(session.identity), repr(session.profile())):
        assert PASSWORD not in value and "$argon2" not in value
    assert not isinstance(identity, models.Base) and session.identity == identity


def test_passwords_never_reach_logs_even_with_sql_logging_on(tmp_path, caplog) -> None:
    engine = create_backend_engine(f"sqlite:///{(tmp_path / 'logs.db').as_posix()}", hide_parameters=True)
    upgrade(engine)
    backend = DirectBackend(engine)
    caplog.set_level(logging.DEBUG)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)
    try:
        backend.register(email="logs@example.com", password=PASSWORD)
        backend.sign_in(email="logs@example.com", password=PASSWORD)
        with pytest.raises(InvalidCredentialsError):
            backend.sign_in(email="logs@example.com", password="wrong " + PASSWORD)
        stored = stored_hash(engine, "logs@example.com")
    finally:
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
        backend.close()
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "INSERT INTO users" in text  # SQL logging really was on
    assert PASSWORD not in text and stored not in text and "$argon2" not in text


def test_the_account_scope_comes_only_from_sign_in(backend, alice, bob) -> None:
    with pytest.raises(TypeError):
        AccountSession(backend, bob.identity, _capability=object())  # a UUID/identity alone grants nothing
    planning = alice.planning_service()
    with pytest.raises(ScopeError):
        planning.scoped(OwnerScope.account(bob.user_id))
    with pytest.raises(ScopeError):  # a record claiming another owner cannot be written in this scope
        planning.create_task(Task(user_id=bob.user_id, name="x", category="c", estimated_duration_minutes=5,
                                  priority=5))

    task = planning.create_task(Task(user_id=alice.user_id, name="mine", category="c", estimated_duration_minutes=5,
                                     priority=5))
    alice.sign_out()
    for call in (planning.list_tasks, alice.execution_service().list_executions, alice.profile):
        with pytest.raises(NotSignedInError):
            call()
    assert bob.planning_service().get_task(task.id) is None  # never visible to another account
    assert account(backend, "carol@example.com").planning_service().list_tasks() == []
    assert uuid.UUID(str(task.user_id)) == alice.user_id

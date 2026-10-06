"""Session renewal and "Keep me signed in": the refresh credential (never the password) is what is kept,
in the vault only, replaced on every renewal, restored after a restart and removed and revoked by sign-out."""

from __future__ import annotations

from app.sync.credentials import MemoryVault
from app.sync.service import SyncService
from app.sync.transport import TransportError, logout_via, refresh_via
from config import settings
from tests.sync.conftest import PASSWORD, InProcessTransport

EMAIL = "alice@example.com"


class RenewingTransport(InProcessTransport):
    def __init__(self, client) -> None:
        super().__init__(client)
        self.refreshes = 0
        self.offline = False

    def refresh(self, refresh_token: str):
        if self.offline:
            raise TransportError("connection refused (injected)")
        self.refreshes += 1
        return refresh_via(self._request, refresh_token)

    def logout(self, refresh_token: str) -> None:
        # 204 with no body (the shared _request helper expects JSON; HttpTransport reads an empty body as {}).
        logout_via(lambda method, path, token, body: self.client.request(method, path, json=body) and {}, refresh_token)


def with_vault(device, vault) -> SyncService:
    device.sync = SyncService(device.connection, device.transport, vault=vault)
    return device.sync


def restart(device, vault) -> SyncService:
    device.reopen(keep_session=False)
    return with_vault(device, vault)


def refresh_status(server, secret: str) -> int:
    return server.client.post("/auth/refresh", json={"refresh_token": secret}).status_code


def test_an_expired_access_token_is_renewed_without_the_password(make_device, alice_server) -> None:
    transport = RenewingTransport(alice_server.client)
    device = make_device("desk", transport)
    device.sign_in(EMAIL)
    device.add_task("Renewed")
    device.sync._token = "expired.access.token"  # what the server answers 401 to

    assert device.sync_now().status == "ok" and device.sync.signed_in and transport.refreshes == 1
    assert [change["record"]["name"] for change in alice_server.changes(EMAIL)] == ["Renewed"]

    device.sync._token = "expired.again"  # the profile path renews the same way
    assert device.sync.profile()["email"] == EMAIL and transport.refreshes == 2


def test_a_session_that_cannot_be_renewed_asks_to_sign_in_again(make_device, alice_server) -> None:
    device = make_device("desk", InProcessTransport(alice_server.client))  # a transport without refresh()
    device.sign_in(EMAIL)
    device.sync._token = "expired.access.token"
    assert device.sync_now().status == "auth_required" and not device.sync.signed_in


def test_keep_me_signed_in_survives_a_restart_and_keeps_no_password(make_device, alice_server) -> None:
    vault = MemoryVault()
    device = make_device("desk", RenewingTransport(alice_server.client))
    sync = with_vault(device, vault)
    assert sync.can_keep_signed_in
    account = sync.sign_in(EMAIL, PASSWORD, keep=True)
    kept = vault.secrets[account.account_key]
    assert list(vault.secrets) == [account.account_key] and PASSWORD not in kept and len(kept) == 43
    stored = " ".join(str(value) for row in device.connection.execute("SELECT * FROM local_settings") for value in row)
    assert kept not in stored and PASSWORD not in stored  # the database holds neither

    sync = restart(device, vault)
    assert not sync.signed_in
    assert sync.sync_now().status == "ok" and sync.signed_in and sync.account.email == EMAIL
    rotated = vault.secrets[account.account_key]
    assert rotated != kept and refresh_status(alice_server, kept) == 401  # single use: the old one is spent


def test_without_the_option_nothing_is_kept(make_device, alice_server) -> None:
    vault = MemoryVault()
    device = make_device("desk", RenewingTransport(alice_server.client))
    sync = with_vault(device, vault)
    sync.sign_in(EMAIL, PASSWORD, keep=True)
    sync.sign_in(EMAIL, PASSWORD)  # signing in again without it ends the kept session
    assert vault.secrets == {}

    sync = restart(device, vault)
    assert sync.sync_now().status == "inert" and not sync.signed_in and sync.status().auth_required is False
    assert sync.workspace_account().email == EMAIL  # the workspace stays; only the session is gone


def test_sign_out_removes_the_kept_session_and_revokes_it(make_device, alice_server) -> None:
    vault = MemoryVault()
    device = make_device("desk", RenewingTransport(alice_server.client))
    sync = with_vault(device, vault)
    account = sync.sign_in(EMAIL, PASSWORD, keep=True)
    kept = vault.secrets[account.account_key]

    sync.sign_out()
    assert vault.secrets == {} and refresh_status(alice_server, kept) == 401

    sync = restart(device, vault)
    assert sync.sync_now().status == "inert" and not sync.signed_in


def test_a_restart_while_offline_keeps_the_session_for_later(make_device, alice_server) -> None:
    vault = MemoryVault()
    transport = RenewingTransport(alice_server.client)
    device = make_device("desk", transport)
    account = with_vault(device, vault).sign_in(EMAIL, PASSWORD, keep=True)
    kept = vault.secrets[account.account_key]

    sync = restart(device, vault)
    transport.offline = True
    assert sync.sync_now().status == "offline" and not sync.signed_in
    assert vault.secrets[account.account_key] == kept  # untouched: nothing was sent

    transport.offline = False
    assert sync.sync_now().status == "ok" and sync.signed_in


def test_a_session_the_server_ended_is_forgotten(make_device, alice_server) -> None:
    vault = MemoryVault()
    device = make_device("desk", RenewingTransport(alice_server.client))
    account = with_vault(device, vault).sign_in(EMAIL, PASSWORD, keep=True)
    logout = alice_server.client.post("/auth/logout", json={"refresh_token": vault.secrets[account.account_key]})
    assert logout.status_code == 204  # e.g. signed out from elsewhere, or a password reset

    sync = restart(device, vault)
    assert sync.sync_now().status == "inert" and not sync.signed_in and vault.secrets == {}
    assert sync.sign_in(EMAIL, PASSWORD, keep=True).email == EMAIL  # signing in again works as usual


def test_no_vault_means_the_option_is_not_available(make_device, alice_server) -> None:
    device = make_device("desk", RenewingTransport(alice_server.client))
    assert not device.sync.can_keep_signed_in
    device.sync.sign_in(EMAIL, PASSWORD, keep=True)  # accepted, simply not kept
    device.reopen(keep_session=False)
    assert not device.sync.signed_in and device.sync.sync_now().status == "inert"


def test_the_backend_address_is_built_in_and_can_be_overridden_or_turned_off() -> None:
    assert settings.resolve_backend_url(None) == settings.resolve_backend_url("  ") == settings.DEFAULT_BACKEND_URL
    assert settings.DEFAULT_BACKEND_URL.startswith("https://")
    assert settings.resolve_backend_url(" http://127.0.0.1:8000 ") == "http://127.0.0.1:8000"
    assert settings.resolve_backend_url("off") is None and settings.resolve_backend_url("OFF") is None

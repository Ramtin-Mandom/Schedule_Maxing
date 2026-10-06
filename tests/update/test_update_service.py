"""
The updater's core (app/update): version comparison, the release feed, the verified download and
the hand-off to the installer. Every test uses the fake GitHub of tests/update_fakes.py; nothing is installed.
"""

from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

import pytest

from app.update import release_feed, transport
from app.update.downloader import VerificationError, download_and_verify, expected_digest
from app.update.release_feed import FeedError, parse_release
from app.update.service import (
    AVAILABLE,
    DISABLED,
    INSTALLER_ARGUMENTS,
    UNAVAILABLE,
    UP_TO_DATE,
    UpdateInstallError,
    UpdateService,
)
from app.update.transport import DownloadCancelled, UpdateTransportError, check_url
from app.update.versioning import is_newer, parse_version
from tests.update_fakes import API, REPOSITORY, asset_url, installer_bytes, release_payload


def service(github, tmp_path: Path, installed: str = "1.3.2", **options) -> UpdateService:
    options.setdefault("launcher", lambda path, arguments: None)
    options.setdefault("frozen", lambda: True)
    return UpdateService(installed_version=installed, repository=REPOSITORY, fetcher=github,
                         updates_dir=tmp_path / "data" / "updates", **options)


# -----------------------------------------------------------------------------
# Versions
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("newer, older", [("1.4.0", "1.3.2"), ("1.10.0", "1.9.9"), ("2.0.0", "1.99.99"),
                                           ("v1.0.1", "1.0.0"), ("1.0.10", "v1.0.9")])
def test_versions_compare_as_numbers(newer, older):
    assert is_newer(newer, older) and not is_newer(older, newer)


def test_equal_and_malformed_versions_are_never_newer():
    assert not is_newer("1.3.2", "1.3.2") and not is_newer("v1.3.2", "1.3.2")
    for text in ("1.3", "1.3.2.1", "1.3.2-rc1", "1.3.2+build", "1.3.2b1", "latest", "", " 1.3.2", "1.03.2", "9" * 9 + ".0.0",
                 None, 132, "1.3.2\n"):
        assert parse_version(text) is None
        assert not is_newer(text, "1.0.0") and not is_newer("9.9.9", text)


# -----------------------------------------------------------------------------
# Checking
# -----------------------------------------------------------------------------


def test_no_update_when_the_installed_version_is_published(github, tmp_path):
    github.publish("1.3.2")
    result = service(github, tmp_path).check()
    assert result.status == UP_TO_DATE and result.release is None
    assert github.requests == [API]  # one small request; nothing is downloaded by a check


def test_a_newer_version_is_offered_with_its_verified_addresses(github, tmp_path):
    github.publish("1.4.0")
    result = service(github, tmp_path).check()
    assert result.status == AVAILABLE and result.available
    assert result.release.version == "1.4.0" and result.release.installer_name == "ScheduleMaxing-Setup-1.4.0.exe"
    assert result.release.installer_url == asset_url("1.4.0", "ScheduleMaxing-Setup-1.4.0.exe")
    assert result.release.notes_url == f"https://github.com/{REPOSITORY}/releases/tag/v1.4.0"


def test_an_older_published_version_is_never_a_downgrade(github, tmp_path):
    github.publish("1.2.0")
    assert service(github, tmp_path, installed="1.3.2").check().status == UP_TO_DATE


@pytest.mark.parametrize("change", [
    {"tag_name": "1.4.0"},                       # not a v-tag
    {"tag_name": "v1.4.0-rc1"},                  # a development version
    {"tag_name": "v1.4"},
    {"tag_name": None},
    {"prerelease": True},
    {"draft": True},
    {"prerelease": None},
    {"assets": None},
    {"assets": []},
    {"assets": "ScheduleMaxing-Setup-1.4.0.exe"},
])
def test_malformed_or_unstable_releases_are_ignored(github, tmp_path, change):
    github.publish("1.4.0", payload=release_payload("1.4.0", **change))
    result = service(github, tmp_path).check()
    assert result.status == UNAVAILABLE and result.release is None


def edit_assets(version: str, edit) -> dict:
    payload = release_payload(version)
    edit(payload["assets"])
    return payload


@pytest.mark.parametrize("edit", [
    lambda assets: assets.pop(0),                                                   # no installer
    lambda assets: assets.pop(1),                                                   # no checksums
    lambda assets: assets.append(dict(assets[0])),                                  # two installers
    lambda assets: assets[0].update(name="ScheduleMaxing-Setup-9.9.9.exe"),         # another version's name
    lambda assets: assets[0].update(name="Setup.exe"),
    lambda assets: assets[0].update(size=10),                                       # implausibly small
    lambda assets: assets[0].update(size="1200000"),
    lambda assets: assets[0].update(browser_download_url="http://github.com/x"),    # not HTTPS
    lambda assets: assets[0].update(browser_download_url="https://evil.example/ScheduleMaxing-Setup-1.4.0.exe"),
    lambda assets: assets[0].update(
        browser_download_url="https://github.com/someone-else/repo/releases/download/v1.4.0/ScheduleMaxing-Setup-1.4.0.exe"),
    lambda assets: assets[1].update(browser_download_url=asset_url("1.3.0", "SHA256SUMS.txt")),  # another release's file
])
def test_releases_with_unexpected_assets_are_refused(edit):
    with pytest.raises(FeedError):
        parse_release(edit_assets("1.4.0", edit), REPOSITORY)


def test_unreadable_feed_answers_are_not_updates(github, tmp_path):
    for body in (b"<html>rate limited</html>", b"[]", b"\xff\xfe", b"null"):
        github.responses[API] = body
        assert service(github, tmp_path).check().status == UNAVAILABLE
    github.responses[API] = b"x" * (release_feed.MAX_FEED_BYTES + 1)
    assert service(github, tmp_path).check().status == UNAVAILABLE


@pytest.mark.parametrize("failure", [
    UpdateTransportError("could not reach the update server (URLError)"),   # offline / DNS
    UpdateTransportError("could not reach the update server (TimeoutError)"),
    UpdateTransportError("the update server answered HTTP 403"),              # rate limited
    UpdateTransportError("the update server answered HTTP 404"),              # no release / private repository
    UpdateTransportError("the update server answered HTTP 503"),
    RuntimeError("anything unexpected"),
])
def test_an_unreachable_github_is_reported_not_raised(github, tmp_path, failure):
    github.responses[API] = failure
    result = service(github, tmp_path).check()
    assert result.status == UNAVAILABLE and result.reason and not result.available


def test_a_repository_without_releases_says_so(github, tmp_path):
    github.responses[API] = UpdateTransportError("the update server answered HTTP 404", status=404)
    result = service(github, tmp_path).check()
    assert result.status == UNAVAILABLE and result.reason == "No released version is published yet."


def test_checks_are_off_without_a_repository(github, tmp_path):
    for repository in ("", "not a repository", "owner/name/extra"):
        off = UpdateService(installed_version="1.0.0", repository=repository, fetcher=github, updates_dir=tmp_path)
        assert off.check().status == DISABLED and not off.enabled
    assert github.requests == []


def test_a_development_build_is_not_offered_updates(github, tmp_path):
    github.publish("1.4.0")
    assert service(github, tmp_path, installed="1.4.0.dev1").check().status == UNAVAILABLE


# -----------------------------------------------------------------------------
# Transport rules
# -----------------------------------------------------------------------------


def test_only_https_github_hosts_are_ever_contacted():
    for good in (API, asset_url("1.4.0", "x.exe"), "https://objects.githubusercontent.com/a/b?c=d",
                 "https://release-assets.githubusercontent.com/a"):
        assert check_url(good) == good
    for bad in ("http://github.com/x", "https://github.com.evil.example/x", "https://evil.example/github.com",
                "https://user:pw@github.com/x", "https://github.com:8443/x", "file:///C:/Windows/system32/calc.exe",
                "ftp://github.com/x", "//github.com/x", ""):
        with pytest.raises(UpdateTransportError):
            check_url(bad)


def test_a_redirect_to_another_host_is_refused():
    handler = transport._CheckedRedirects()
    request = urllib.request.Request(asset_url("1.4.0", "x.exe"))
    with pytest.raises(UpdateTransportError):
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/x.exe")
    with pytest.raises(UpdateTransportError):
        handler.redirect_request(request, None, 302, "Found", {}, "http://objects.githubusercontent.com/x.exe")
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://objects.githubusercontent.com/x.exe")


# -----------------------------------------------------------------------------
# Downloading and verifying
# -----------------------------------------------------------------------------


def available(github, tmp_path, version="1.4.0", **options):
    updater = service(github, tmp_path, **options)
    return updater, updater.check().release


def leftovers(tmp_path: Path) -> list[str]:
    folder = tmp_path / "data" / "updates"
    return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


def test_a_good_download_is_verified_and_kept(github, tmp_path):
    github.publish("1.4.0")
    updater, release = available(github, tmp_path)
    seen: list[tuple[int, int]] = []
    result = updater.download(release, progress=lambda written, total: seen.append((written, total)))
    assert result.path == tmp_path / "data" / "updates" / "ScheduleMaxing-Setup-1.4.0.exe"
    assert result.path.read_bytes() == installer_bytes("1.4.0") and result.version == "1.4.0"
    assert result.sha256 == hashlib.sha256(installer_bytes("1.4.0")).hexdigest()
    assert seen[-1] == (len(installer_bytes("1.4.0")), release.installer_size) and leftovers(tmp_path) == [result.path.name]


def test_a_corrupted_download_is_deleted(github, tmp_path):
    corrupted = bytearray(installer_bytes("1.4.0"))
    corrupted[1000] ^= 0xFF  # same size, one flipped bit
    github.publish("1.4.0", installer=bytes(corrupted))
    updater, release = available(github, tmp_path)
    with pytest.raises(VerificationError, match="does not match its published checksum"):
        updater.download(release)
    assert leftovers(tmp_path) == []


def test_a_truncated_download_is_deleted(github, tmp_path):
    github.publish("1.4.0", installer=installer_bytes("1.4.0")[:500_000])
    updater, release = available(github, tmp_path)
    with pytest.raises(VerificationError, match="incomplete"):
        updater.download(release)
    assert leftovers(tmp_path) == []


def test_a_download_larger_than_announced_is_stopped(github, tmp_path):
    github.publish("1.4.0", installer=installer_bytes("1.4.0") + b"extra payload")
    updater, release = available(github, tmp_path)
    with pytest.raises(UpdateTransportError, match="larger than announced"):
        updater.download(release)
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize("checksums", [
    "",                                                                             # no line at all
    f"{'0' * 64}  ScheduleMaxing-Setup-1.4.0.exe\n",                                # the wrong digest
    f"{hashlib.sha256(installer_bytes('1.4.0')).hexdigest()}  some-other-file.exe\n",  # the right digest, another file
    "not-a-digest  ScheduleMaxing-Setup-1.4.0.exe\n",
    f"{'0' * 64}  ScheduleMaxing-Setup-1.4.0.exe\n{'1' * 64}  ScheduleMaxing-Setup-1.4.0.exe\n",  # ambiguous
])
def test_a_checksum_mismatch_or_missing_checksum_refuses_the_installer(github, tmp_path, checksums):
    github.publish("1.4.0", checksums=checksums)
    updater, release = available(github, tmp_path)
    with pytest.raises(VerificationError):
        updater.download(release)
    assert leftovers(tmp_path) == []


def test_checksum_lines_are_matched_by_exact_filename():
    digest = "a" * 64
    text = f"{digest}  ScheduleMaxing-Setup-1.4.0.exe\n{'b' * 64} *other.exe\n"
    assert expected_digest(text, "ScheduleMaxing-Setup-1.4.0.exe") == digest
    assert expected_digest(text, "other.exe") == "b" * 64
    with pytest.raises(VerificationError):
        expected_digest(text, "ScheduleMaxing-Setup-1.4.0.exe.evil")


def test_a_failed_or_cancelled_download_leaves_nothing_behind(github, tmp_path):
    github.publish("1.4.0")
    updater, release = available(github, tmp_path)
    github.responses[release.installer_url] = UpdateTransportError("the download was interrupted (ConnectionError)")
    with pytest.raises(UpdateTransportError):
        updater.download(release)
    github.publish("1.4.0")
    with pytest.raises(DownloadCancelled):
        updater.download(release, cancelled=lambda: True)
    assert leftovers(tmp_path) == []


def test_stale_downloads_are_removed_but_nothing_else(github, tmp_path):
    folder = tmp_path / "data" / "updates"
    folder.mkdir(parents=True)
    (folder / "ScheduleMaxing-Setup-1.1.0.exe").write_bytes(b"old")
    (folder / "ScheduleMaxing-Setup-1.2.0.exe.partial").write_bytes(b"half")
    (folder / "notes.txt").write_text("not ours", encoding="utf-8")
    github.publish("1.4.0")
    result = download_and_verify(service(github, tmp_path).check().release, folder, github)
    assert leftovers(tmp_path) == ["ScheduleMaxing-Setup-1.4.0.exe", "notes.txt"] and result.path.is_file()


# -----------------------------------------------------------------------------
# Starting the installer
# -----------------------------------------------------------------------------


def test_the_verified_installer_is_started_with_the_documented_switches(github, tmp_path):
    started: list = []
    github.publish("1.4.0")
    updater, release = available(github, tmp_path, launcher=lambda path, arguments: started.append((path, arguments)))
    download = updater.download(release)
    updater.launch_installer(download)
    assert started == [(download.path.resolve(), ("/SILENT", "/NORESTART", "/RELAUNCH=1"))]
    assert INSTALLER_ARGUMENTS == ("/SILENT", "/NORESTART", "/RELAUNCH=1")


def test_nothing_is_started_unless_this_session_verified_it(github, tmp_path):
    started: list = []
    github.publish("1.4.0")
    updater, release = available(github, tmp_path, launcher=lambda path, arguments: started.append(path))
    download = updater.download(release)

    outside = tmp_path / "ScheduleMaxing-Setup-1.4.0.exe"
    outside.write_bytes(installer_bytes("1.4.0"))
    other_session = service(github, tmp_path, launcher=lambda path, arguments: started.append(path))
    for bad_service, bad_download in (
        (updater, type(download)(download.version, outside, download.sha256, download.size)),   # outside the folder
        (updater, type(download)(download.version, download.path, "0" * 64, download.size)),    # another digest
        (other_session, download),                                                               # not verified here
    ):
        with pytest.raises(UpdateInstallError):
            bad_service.launch_installer(bad_download)

    download.path.write_bytes(b"swapped after verification")
    with pytest.raises(UpdateInstallError, match="changed after it was verified"):
        updater.launch_installer(download)
    assert started == []


def test_a_source_checkout_checks_but_never_installs(github, tmp_path):
    started: list = []
    github.publish("1.4.0")
    updater, release = available(github, tmp_path, frozen=lambda: False, launcher=lambda *args: started.append(args))
    assert release is not None and not updater.can_install
    with pytest.raises(UpdateInstallError, match="source code"):
        updater.launch_installer(updater.download(release))
    assert started == []


def test_a_declined_elevation_prompt_is_an_error_not_a_crash(github, tmp_path):
    def declined(path, arguments):
        raise OSError(1223, "The operation was canceled by the user")

    github.publish("1.4.0")
    updater, release = available(github, tmp_path, launcher=declined)
    with pytest.raises(UpdateInstallError, match="could not be started"):
        updater.launch_installer(updater.download(release))


def test_a_release_that_is_not_newer_is_never_downloaded(github, tmp_path):
    github.publish("1.4.0")
    release = service(github, tmp_path, installed="1.3.0").check().release
    with pytest.raises(UpdateInstallError):
        service(github, tmp_path, installed="1.4.0").download(release)


# -----------------------------------------------------------------------------
# The user's data
# -----------------------------------------------------------------------------


def test_checking_downloading_and_launching_never_touch_the_users_data(github, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    files = {"executions.db": b"SQLite format 3\x00 the schedule", "ui_settings.json": b'{"appearance": "dark"}',
             "task_defaults.json": b"{}", "ml_duration_model.joblib": b"model", "logs/schedule-maxing.log": b"log line\n",
             "backups/executions-v14-20260101T000000000000Z.db": b"backup"}
    for name, content in files.items():
        (data / name).parent.mkdir(parents=True, exist_ok=True)
        (data / name).write_bytes(content)

    github.publish("1.4.0")
    updater, release = available(github, tmp_path)
    updater.launch_installer(updater.download(release))

    assert {name: (data / name).read_bytes() for name in files} == files
    everything = sorted(path.relative_to(data).as_posix() for path in data.rglob("*") if path.is_file())
    assert everything == sorted([*files, "updates/ScheduleMaxing-Setup-1.4.0.exe"])

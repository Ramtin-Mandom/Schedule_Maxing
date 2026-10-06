"""Operator smoke check of a running API: python -m backend.smoke_check https://host

Registers two throwaway accounts (random passwords, never printed), then
checks sign-in, an owned task, cross-account rejection, refresh rotation and
logout. It WRITES to the target: two accounts and one task remain afterwards
(there is no account-deletion endpoint). Exit code 0 when every check passes.
"""

from __future__ import annotations

import secrets
import sys

import httpx

TASK = {"name": "Smoke check task", "category": "study", "estimated_duration_minutes": 30, "priority": 5}


def run(base_url: str) -> int:
    results: list[tuple[str, bool, str]] = []

    def check(label: str, response: httpx.Response, expected: int) -> httpx.Response:
        results.append((label, response.status_code == expected, f"{response.status_code} (expected {expected})"))
        return response

    with httpx.Client(base_url=base_url.rstrip("/"), timeout=90, trust_env=False) as client:
        check("health", client.get("/health"), 200)
        check("ready", client.get("/ready"), 200)
        run_id, pairs = secrets.token_hex(6), []
        for name in ("a", "b"):
            credentials = {"email": f"smoke-{run_id}-{name}@example.com", "password": secrets.token_urlsafe(24)}
            check(f"register {name}", client.post("/auth/register", json=credentials), 201)
            if name == "a":
                check("duplicate register", client.post("/auth/register", json=credentials), 409)
                check("wrong password", client.post("/auth/login", json={**credentials, "password": "x" * 20}), 401)
            login = check(f"login {name}", client.post("/auth/login", json=credentials), 200)
            if login.status_code != 200:
                return report(results)
            pairs.append(login.json())
        alice, bob = ({"Authorization": "Bearer " + pair["access_token"]} for pair in pairs)
        check("me", client.get("/auth/me", headers=alice), 200)
        created = check("A creates task", client.post("/tasks", json=TASK, headers=alice), 201)
        if created.status_code != 201:
            return report(results)
        path = "/tasks/" + created.json()["id"]
        check("A reads own task", client.get(path, headers=alice), 200)
        check("B reads A's task", client.get(path, headers=bob), 404)
        check("B updates A's task", client.put(path, json={**TASK, "base_version": 1}, headers=bob), 404)
        check("B deletes A's task", client.delete(path, params={"base_version": 1}, headers=bob), 404)
        listed = client.get("/tasks", headers=bob)
        results.append(("B's task list is empty", listed.status_code == 200 and listed.json()["items"] == [],
                        str(listed.status_code)))
        check("forged user_id rejected", client.post("/tasks", json={**TASK, "user_id": created.json()["id"]},
                                                     headers=bob), 422)
        check("A's task untouched", client.get(path, headers=alice), 200)
        renewed = check("refresh", client.post("/auth/refresh", json={"refresh_token": pairs[0]["refresh_token"]}), 200)
        if renewed.status_code == 200:
            token = renewed.json()["refresh_token"]
            check("logout", client.post("/auth/logout", json={"refresh_token": token}), 204)
            check("access token dead after logout", client.get("/auth/me", headers=alice), 401)
            check("refresh dead after logout", client.post("/auth/refresh", json={"refresh_token": token}), 401)
        check("B unaffected by A's logout", client.get("/auth/me", headers=bob), 200)
    return report(results)


def report(results: list[tuple[str, bool, str]]) -> int:
    for label, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'}  {label}: {detail}")
    failed = sum(not passed for _, passed, _ in results)
    print(f"{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith(("http://", "https://")):
        raise SystemExit("usage: python -m backend.smoke_check https://your-service.example.com")
    raise SystemExit(run(sys.argv[1]))

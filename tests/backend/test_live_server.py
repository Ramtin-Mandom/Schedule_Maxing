"""Exercise the actual ASGI entry point through a loopback HTTP socket."""

import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest
from sqlalchemy import func, select

from backend import models
from backend.database import create_backend_engine, session_factory
from backend.inspect_db import inspect_database
from backend.migrate import upgrade
from tests.backend.conftest import task_payload


@pytest.mark.slow
def test_real_uvicorn_auth_storage_and_isolation(tmp_path):
    root = Path(__file__).resolve().parents[2]
    database_url = "sqlite:///" + (tmp_path / "live.db").as_posix()
    env = {**os.environ, "DATABASE_URL": database_url, "JWT_SECRET": secrets.token_urlsafe(48),
           "ENVIRONMENT": "test", "ALLOWED_HOSTS": "127.0.0.1", "RATE_LIMIT_ENABLED": "true",
           "CORS_ORIGINS": "", "ALLOWED_ORIGINS": "", "TRUSTED_PROXIES": ""}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    log = tmp_path / "uvicorn.log"
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT,
        )
        engine = create_backend_engine(database_url)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
                deadline = time.monotonic() + 40
                while True:
                    if process.poll() is not None:
                        pytest.fail("ASGI process exited before startup: " + log.read_text())
                    try:
                        healthy = client.get("/health")
                        break
                    except httpx.ConnectError:
                        if time.monotonic() >= deadline:
                            pytest.fail("ASGI process did not start within 40 seconds")
                        time.sleep(0.1)
                assert healthy.status_code == 200 and healthy.json() == {"status": "ok"}
                assert client.get("/ready").status_code == 503
                upgrade(engine)
                assert client.get("/ready").status_code == 200
                assert client.get("/docs").status_code == 200
                assert "/auth/refresh" in client.get("/openapi.json").json()["paths"]
                pairs = []
                for email in ("alice@example.com", "bob@example.com"):
                    credentials = {"email": email, "password": secrets.token_urlsafe(24)}
                    assert client.post("/auth/register", json=credentials).status_code == 201
                    login = client.post("/auth/login", json=credentials)
                    assert login.status_code == 200
                    pairs.append(login.json())
                alice, bob = ({"Authorization": "Bearer " + pair["access_token"]} for pair in pairs)
                owner = client.get("/auth/me", headers=alice).json()["id"]
                response = client.post("/tasks", headers=alice, json=task_payload())
                assert response.status_code == 201
                task = response.json()
                path = "/tasks/" + task["id"]
                assert client.get(path, headers=alice).status_code == 200
                assert client.get(path, headers=bob).status_code == 404
                assert client.put(path, headers=bob, json=task_payload(base_version=1)).status_code == 404
                assert client.delete(path + "?base_version=1", headers=bob).status_code == 404
                with session_factory(engine)() as session:
                    stored = session.get(models.Task, (uuid.UUID(owner), uuid.UUID(task["id"])))
                    assert stored is not None and stored.name == task["name"]
                    assert session.scalar(select(func.count()).select_from(models.User)) == 2
                assert inspect_database(engine)["tables"]["tasks"]["count"] == 1
                renewed = client.post("/auth/refresh", json={"refresh_token": pairs[0]["refresh_token"]})
                assert renewed.status_code == 200
                assert client.post("/auth/logout", json={"refresh_token": renewed.json()["refresh_token"]}).status_code == 204
                assert client.get("/auth/me", headers=alice).status_code == 401
        finally:
            engine.dispose()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    logged = log.read_text(encoding="utf-8")
    for pair in pairs:
        assert pair["access_token"] not in logged and pair["refresh_token"] not in logged

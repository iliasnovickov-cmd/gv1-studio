from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from gv1_helper.app import create_app
from gv1_helper.config import Settings


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "var",
        max_input_bytes=4 * 1024 * 1024,
        max_duration_seconds=60,
        result_ttl_seconds=300,
    )
    return TestClient(create_app(settings))


def test_health_and_github_pages_cors(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get(
            "/api/health", headers={"Origin": "https://sample-user.github.io"}
        )
        assert response.status_code == 200
        assert response.json()["loopback_only"] is True
        assert response.headers["access-control-allow-origin"] == "https://sample-user.github.io"


def test_private_network_preflight(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.options(
            "/api/jobs/file",
            headers={
                "Origin": "https://sample-user.github.io",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        assert response.status_code == 204
        assert response.headers["access-control-allow-private-network"] == "true"
        assert response.headers["access-control-allow-origin"] == "https://sample-user.github.io"


def test_untrusted_browser_origin_is_rejected(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/api/health", headers={"Origin": "https://evil.test"})
        assert response.status_code == 403


def test_youtube_requires_authorization_and_safe_domain(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        denied = client.post(
            "/api/jobs/youtube",
            json={"url": "https://youtu.be/abc", "authorized": False},
        )
        assert denied.status_code == 403
        spoof = client.post(
            "/api/jobs/youtube",
            json={"url": "https://youtube.com.evil.test/watch?v=x", "authorized": True},
        )
        assert spoof.status_code == 422


def test_file_job_matches_frontend_contract(tmp_path: Path) -> None:
    fake_mp4 = b"\x00\x00\x00\x18ftypisom" + bytes(128)
    required = {
        "id",
        "status",
        "stage",
        "progress",
        "message",
        "error",
        "output_name",
        "size",
        "crc32",
        "sha256",
        "download_url",
    }
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/jobs/file",
            files={"file": ("clip.mp4", fake_mp4, "video/mp4")},
            data={"auto_send": "false", "output_name": "Demo"},
        )
        assert response.status_code == 202, response.text
        payload = response.json()
        assert required <= payload.keys()
        assert payload["output_name"] == "Demo.gv1"
        status_response = client.get(f"/api/jobs/{payload['id']}")
        assert status_response.status_code == 200
        assert required <= status_response.json().keys()

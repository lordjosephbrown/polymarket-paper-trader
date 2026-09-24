from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "test.db"),
        secret_key="test-secret",
        base_url="http://testserver",
        paystack_secret_key="",
    )


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def signup(client: TestClient, name: str = "Adjoa's Beauty Bar", phone: str = "0241234567") -> None:
    resp = client.post(
        "/signup",
        data={
            "name": name,
            "phone": phone,
            "password": "secret123",
            "category": "salon",
            "location": "Osu, Accra",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def open_all_days(client: TestClient) -> None:
    data: dict[str, str] = {}
    for day in range(7):
        data[f"enabled_{day}"] = "on"
        data[f"open_{day}"] = "09:00"
        data[f"close_{day}"] = "17:00"
    resp = client.post("/dashboard/hours", data=data, follow_redirects=False)
    assert resp.status_code == 303


def add_service(
    client: TestClient,
    name: str = "Knotless braids",
    duration_min: int = 60,
    price_ghs: float = 250,
    deposit_ghs: float = 50,
) -> None:
    resp = client.post(
        "/dashboard/services",
        data={
            "name": name,
            "duration_min": str(duration_min),
            "price_ghs": str(price_ghs),
            "deposit_ghs": str(deposit_ghs),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def tomorrow() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

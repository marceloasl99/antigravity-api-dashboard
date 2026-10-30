import json
import os
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="agy-tests-")
HOMES = {pid: os.path.join(_TMP, f"home-{pid}") for pid in ("p1", "p2", "p3")}
for h in HOMES.values():
    os.makedirs(h, exist_ok=True)

PROFILES_PATH = os.path.join(_TMP, "profiles.json")
with open(PROFILES_PATH, "w") as f:
    json.dump([{"id": pid, "name": f"Profile {pid}", "email": f"{pid}@example.com", "home": h, "concurrency": 2}
               for pid, h in HOMES.items()], f)

os.environ.update({
    "AGY_COMMAND": f'"{sys.executable}" -I "{os.path.join(HERE, "fake_agy.py")}"',
    "AGY_CWD": _TMP,
    "CONFIG_FILE": os.path.join(_TMP, "agy_config.json"),
    "TOKEN_USAGE_FILE": os.path.join(_TMP, "agy_token_usage.json"),
    "PROFILES_CONFIG": PROFILES_PATH,
    "AGY_POOL_SIZE": "1",
    "AGY_PREWARM_ON_START": "false",
    "AGY_INIT_TIMEOUT": "20",
    "AGY_TURN_TIMEOUT": "20",
    "DEBUG": "false",
})

import api_service_multi as svc  # noqa: E402  (after env setup)


@pytest.fixture
def client():
    svc.app.config["TESTING"] = True
    return svc.app.test_client()


@pytest.fixture
def config():
    """Reset config to a known default before each test and return a setter."""
    def set_config(**kwargs):
        cfg = svc.get_config()
        cfg.update(kwargs)
        return svc.save_config(cfg)
    set_config(enabled_profiles=["p1"], active_effort="low", agy_internal_tools=True)
    yield set_config


@pytest.fixture(autouse=True)
def _drain_pool():
    yield
    svc.POOL.shutdown()


def sse_events(body: str):
    """Parse an SSE body into a list of JSON objects (plus the string '[DONE]')."""
    out = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block.startswith("data: "):
            continue
        data = block[len("data: "):]
        out.append("[DONE]" if data == "[DONE]" else json.loads(data))
    return out

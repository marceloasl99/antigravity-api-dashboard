import time

import pytest

import agy_runner
from agy_runner import AgyError, AgyProcess, WarmPool
from conftest import HOMES

P1 = {"id": "p1", "home": HOMES["p1"]}
P2 = {"id": "p2", "home": HOMES["p2"]}
LOW = "gemini-3.8-flash-low"


def _wait(cond, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_process_init_and_single_turn():
    proc = AgyProcess(P1, "gemini-3.8-flash-low")
    try:
        assert proc.wait_ready(10)
        assert proc.init_event["permission_mode"] == "always-proceed"
        events = list(proc.run_turn("hello"))
        assert events[-1]["event"] == "result"
        assert events[-1]["result"]["status"] == "SUCCESS"
        deltas = [e["step_update"]["text_delta"] for e in events if e["event"] == "step_update" and e["step_update"].get("text_delta")]
        assert "".join(deltas) == "Hello from fake agy (home-p1)"
    finally:
        proc.close()


def test_process_crash_reports_stderr(monkeypatch):
    monkeypatch.setenv("FAKE_AGY_CRASH_HOMES", "home-p1")
    proc = AgyProcess(P1, "gemini-3.8-flash-low")
    try:
        assert proc.wait_ready(10) is False
        assert "authentication required" in proc.stderr_text
    finally:
        proc.close()


def test_pool_serves_prewarmed_process_and_refills():
    pool = WarmPool(size=1, max_idle=600, enabled=True)
    try:
        pool.prewarm([P1], [LOW])
        assert _wait(lambda: pool.stats()["ready_total"] == 1)
        ready_pid = pool._ready[("p1", LOW, True)][0].proc.pid

        proc = pool.acquire(P1, LOW)
        assert proc.proc.pid == ready_pid          # served from the pool, no cold start
        assert proc.ready
        proc.close()
        s = pool.stats()
        assert s["hits"] == 1 and s["misses"] == 0
        assert _wait(lambda: pool.stats()["ready"].get(f"p1/{LOW}") == 1)   # refilled in background

        other = pool.acquire(P2, "gemini-3.8-flash-high")   # different key -> miss, spawned on demand
        other.close()
        assert pool.stats()["misses"] == 1
    finally:
        pool.shutdown()


def test_pool_recycles_stale_processes():
    pool = WarmPool(size=1, max_idle=0.2, enabled=True)
    try:
        pool.prewarm([P1], [LOW])
        assert _wait(lambda: pool.stats()["ready_total"] == 1)
        stale_pid = pool._ready[("p1", LOW, True)][0].proc.pid
        time.sleep(0.3)
        proc = pool.acquire(P1, LOW)
        assert proc.proc.pid != stale_pid
        proc.close()
        assert pool.stats()["recycled"] == 1
    finally:
        pool.shutdown()


def test_pool_disabled_spawns_every_time():
    pool = WarmPool(size=1, enabled=False)
    p = pool.acquire(P1, LOW)
    p.close()
    assert pool.stats()["ready_total"] == 0 and pool.stats()["enabled"] is False


def test_turn_timeout_raises(monkeypatch):
    monkeypatch.setenv("FAKE_AGY_SLOW", "3")
    proc = AgyProcess(P1, LOW)
    try:
        assert proc.wait_ready(10)
        with pytest.raises(AgyError, match="timed out"):
            list(proc.run_turn("SLOW", timeout=0.5))
    finally:
        proc.close()


def test_build_cmd_model_and_tools():
    cmd = agy_runner.build_cmd("gemini-3.8-flash-high", internal_tools=False)
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "gemini-3.8-flash-high"
    assert "--effort" not in cmd                       # agy 1.4.10: conflicts with suffixed ids
    assert "--dangerously-skip-permissions" not in cmd
    assert "--dangerously-skip-permissions" in agy_runner.build_cmd(LOW, True)
    assert "--effort" not in agy_runner.build_cmd("claude-opus-4-6-thinking", True)


def test_build_env_sets_both_home_vars():
    env = agy_runner.build_env(HOMES["p2"])
    assert env["HOME"] == HOMES["p2"] and env["USERPROFILE"] == HOMES["p2"]


def test_gateway_agent_installed_and_selected():
    import os
    path = os.path.join(HOMES["p1"], ".gemini", "config", "agents", "gateway.md")
    if os.path.exists(path):
        os.remove(path)
    proc = AgyProcess(P1, LOW)           # the fake agy exits 1 if --agent names a missing file
    try:
        assert proc.wait_ready(10)
        assert open(path, encoding="utf-8").read().startswith("---\nname: gateway")
        events = list(proc.run_turn("ECHO_AGENT"))
        assert events[-1]["result"]["response"] == "gateway"
    finally:
        proc.close()
    cmd = agy_runner.build_cmd(LOW)
    assert cmd[cmd.index("--agent") + 1] == "gateway"

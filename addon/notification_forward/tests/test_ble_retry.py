import asyncio

from app.ble import advertise_with_retry


def run(fail_first: bool, failing_retries: int):
    calls = []

    async def first():
        calls.append("start")
        if fail_first:
            raise RuntimeError("Failed to register advertisement")

    async def retry():
        calls.append("retry")
        if calls.count("retry") <= failing_retries:
            raise RuntimeError("Failed to register advertisement")

    async def power_cycle():
        calls.append("power")

    async def no_sleep(_):
        pass

    attempts = asyncio.run(advertise_with_retry(first, retry, power_cycle, sleep=no_sleep))
    return attempts, calls


def test_first_start_ok():
    assert run(False, 0) == (1, ["start"])


def test_retries_then_power_cycles_until_it_works():
    attempts, calls = run(True, 2)
    assert attempts == 4
    # attempt 2 retries plainly; from attempt 3 the adapter is power-cycled first
    assert calls == ["start", "retry", "power", "retry", "power", "retry"]


def test_pick_adapter_falls_back_when_the_dongle_was_renumbered():
    from app.agent import pick_adapter

    assert pick_adapter("hci0", ["hci0"]) == "hci0"
    assert pick_adapter("hci0", ["hci1"]) == "hci1"            # USB dongle came back as hci1
    assert pick_adapter("hci1", ["hci0", "hci1"]) == "hci1"     # configured one exists: keep it
    assert pick_adapter("hci0", []) == "hci0"                   # nothing found: let BlueZ report it

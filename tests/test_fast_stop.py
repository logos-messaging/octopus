import types
from typing import ClassVar

import pytest
import typer

from octopus import process
from octopus.transport import logos
from octopus.transport.logos import LogosTransport


class Res:
    def __init__(self, value=None):
        self.ok_value = value

    def is_ok(self):
        return True

    def unwrap(self):
        return self.ok_value


class FakeNode:
    instances: ClassVar[list] = []

    def __init__(self):
        self.destroyed = 0
        FakeNode.instances.append(self)

    @classmethod
    def create_and_start(cls, config, event_cb=None):
        return Res(cls())

    def get_connection_status(self):
        return Res("Connected")

    def stop_and_destroy(self):
        self.destroyed += 1
        return Res(0)


@pytest.fixture(autouse=True)
def fake_bindings(monkeypatch):
    FakeNode.instances = []
    monkeypatch.setattr(
        logos, "_load_bindings", lambda: types.SimpleNamespace(NodeWrapper=FakeNode)
    )
    monkeypatch.setattr(process, "_native_node_left", False)


def test_stop_destroys_the_node_by_default():
    transport = LogosTransport({"mode": "Core"})
    transport.start()
    transport.stop()
    assert FakeNode.instances[0].destroyed == 1
    assert process._native_node_left is False


def test_fast_stop_leaves_the_node_and_marks_the_process():
    transport = LogosTransport({"mode": "Core"}, fast_stop=True)
    transport.start()
    transport.stop()
    assert FakeNode.instances[0].destroyed == 0
    assert process._native_node_left is True


def test_fast_stop_can_be_enabled_after_construction():
    transport = LogosTransport({"mode": "Core"})
    transport.fast_stop = True
    transport.start()
    transport.stop()
    assert FakeNode.instances[0].destroyed == 0


def test_leave_exits_the_process_only_when_a_node_was_left(monkeypatch):
    exits = []
    monkeypatch.setattr(process, "exit_now", exits.append)
    process.leave(0)
    assert exits == []
    process.mark_native_node_left()
    process.leave(3)
    assert exits == [3]


def test_leave_without_a_node_raises_exit_for_a_failure(monkeypatch):
    monkeypatch.setattr(process, "exit_now", lambda code: pytest.fail("must not exit"))
    with pytest.raises(typer.Exit) as e:
        process.leave(4)
    assert e.value.exit_code == 4

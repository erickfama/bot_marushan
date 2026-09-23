import random

import pytest

from src.queue import LoopMode, QueueFullError, SessionQueue


def test_add_insert_move_remove_and_clear() -> None:
    queue = SessionQueue[str](max_size=5)
    assert queue.add(["b", "c"]) == 2
    queue.add(["a"], next_up=True)
    assert list(queue.items) == ["a", "b", "c"]
    queue.move(3, 1)
    assert list(queue.items) == ["c", "a", "b"]
    assert queue.remove(2) == "a"
    assert queue.clear() == 2
    assert not queue.items


def test_queue_limit_partially_accepts_then_rejects() -> None:
    queue = SessionQueue[int](max_size=2)
    assert queue.add([1, 2, 3]) == 2
    with pytest.raises(QueueFullError):
        queue.add([4])


def test_history_previous_and_track_loop() -> None:
    queue = SessionQueue[str]()
    queue.add(["one", "two"])
    assert queue.take_next() == "one"
    queue.loop_mode = LoopMode.TRACK
    queue.finish_current()
    assert queue.take_next() == "one"
    queue.loop_mode = LoopMode.OFF
    queue.finish_current()
    assert queue.take_next() == "two"
    assert queue.previous() == "one"
    assert list(queue.items) == ["two"]


def test_queue_loop_and_failed_track() -> None:
    queue = SessionQueue[str]()
    queue.add(["one"])
    queue.loop_mode = LoopMode.QUEUE
    queue.take_next()
    queue.finish_current()
    assert list(queue.items) == ["one"]
    queue.take_next()
    queue.finish_current(failed=True)
    assert not queue.items


def test_jump_and_seeded_shuffle() -> None:
    queue = SessionQueue[int]()
    queue.add([1, 2, 3, 4])
    assert queue.jump(3) == [1, 2]
    assert list(queue.items) == [3, 4]
    queue.add([5, 6])
    queue.shuffle(random.Random(1))
    assert sorted(queue.items) == [3, 4, 5, 6]

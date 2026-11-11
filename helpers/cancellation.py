"""Cancellation checkpoints for transfers whose SDK may swallow cancellation."""
import asyncio


def check_cancelled():
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError()

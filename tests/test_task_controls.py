import ast
import asyncio
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from helpers.cancellation import check_cancelled


def load_handlers(*names, **context):
    """Execute production handlers without starting Telegram clients."""
    tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    context.update(asyncio=asyncio, Message=object, logger=logging.getLogger(__name__))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main_handlers", "exec"), context)
    return context


class StopPropagation(Exception):
    pass


class TaskControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_cancel_calls_user_cancellation(self):
        cancel = AsyncMock(return_value=2)
        env = load_handlers("handle_ui_callback", CallbackQuery=object,
                            user_language=lambda uid: "en", cancel_user_tasks=cancel,
                            db=SimpleNamespace(clear_conversation_state=Mock()),
                            tr=lambda *a, **k: "Stopped")
        query = SimpleNamespace(data="ui:cancel_jobs", from_user=SimpleNamespace(id=12),
                                answer=AsyncMock(), message=SimpleNamespace(reply=AsyncMock()))
        await env["handle_ui_callback"](query)
        cancel.assert_awaited_once_with(12)

    async def test_old_cancel_button_still_cancels(self):
        cancel = AsyncMock(return_value=1)
        env = load_handlers("handle_callback", Client=object, CallbackQuery=object,
                            user_language=lambda uid: "en", cancel_user_tasks=cancel,
                            db=SimpleNamespace(clear_conversation_state=Mock()),
                            tr=lambda *a, **k: "Stopped")
        query = SimpleNamespace(data="cancel_all", from_user=SimpleNamespace(id=12),
                                answer=AsyncMock(), message=SimpleNamespace(reply=AsyncMock()))
        await env["handle_callback"](None, query)
        cancel.assert_awaited_once_with(12)

    async def test_stop_aliases_cancel_instead_of_opening_menu(self):
        for command in ("stopdownload", "killall", "cancel"):
            cancel = AsyncMock(return_value=1)
            db = SimpleNamespace(clear_conversation_state=Mock())
            env = load_handlers("route_legacy_commands_to_menu", user_language=lambda uid: "en",
                                cancel_user_tasks=cancel, db=db, tr=lambda *a, **k: "Stopped")
            message = SimpleNamespace(from_user=SimpleNamespace(id=12), command=[command],
                                      reply=AsyncMock(), stop_propagation=Mock(side_effect=StopPropagation))
            with self.assertRaises(StopPropagation):
                await env["route_legacy_commands_to_menu"](None, message)
            cancel.assert_awaited_once_with(12)

    async def test_setforward_argument_is_saved(self):
        save = AsyncMock(return_value=SimpleNamespace(id=-10012, title="Target"))
        env = load_handlers("route_legacy_commands_to_menu", user_language=lambda uid: "en",
                            save_forward_destination=save, db=SimpleNamespace(clear_conversation_state=Mock()),
                            tr=lambda *a, **k: "Saved")
        message = SimpleNamespace(from_user=SimpleNamespace(id=12), command=["setforward", "-10012"],
                                  text="/setforward -10012", reply=AsyncMock(),
                                  stop_propagation=Mock(side_effect=StopPropagation))
        with self.assertRaises(StopPropagation):
            await env["route_legacy_commands_to_menu"](None, message)
        save.assert_awaited_once_with(12, "-10012")

    async def test_cancelling_one_user_does_not_stop_another(self):
        one = asyncio.create_task(asyncio.sleep(60))
        two = asyncio.create_task(asyncio.sleep(60))
        await asyncio.sleep(0)
        env = load_handlers("cancel_user_tasks", USER_TASKS={1: {one}, 2: {two}})
        try:
            self.assertEqual(await env["cancel_user_tasks"](1), 1)
            self.assertTrue(one.cancelled())
            self.assertFalse(two.done())
        finally:
            two.cancel()
            await asyncio.gather(two, return_exceptions=True)

    async def test_swallowed_transfer_cancellation_stops_before_next_item(self):
        started = asyncio.Event()
        continued = []
        async def transfer():
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                pass  # Simulate an SDK that swallows task cancellation.
            check_cancelled()
            continued.append(True)
        task = asyncio.create_task(transfer())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(continued, [])

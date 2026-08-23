import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def calls_method(nodes, method_name):
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method_name
        for parent in nodes
        for node in ast.walk(parent)
    )


class DispatcherRegressionTests(unittest.TestCase):
    def test_state_input_stops_propagation_only_after_processing(self):
        """stop_propagation raises, so it must live in the try/finally tail."""
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        handler = next(
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "handle_conversation_input"
        )
        state_machine = next(node for node in handler.body if isinstance(node, ast.Try))
        self.assertTrue(calls_method(state_machine.finalbody, "stop_propagation"))
        statements_before_state_machine = handler.body[:handler.body.index(state_machine)]
        self.assertFalse(calls_method(statements_before_state_machine, "stop_propagation"))

    def test_plain_telegram_links_are_handled_by_catchall(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        handler = next(
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "handle_any_message"
        )
        called_names = {
            node.func.id
            for node in ast.walk(handler)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("is_valid_telegram_url", called_names)
        self.assertIn("get_user_downloader", called_names)
        self.assertIn("track_task", called_names)


if __name__ == "__main__":
    unittest.main()

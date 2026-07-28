"""kilix-calculator: arithmetic, and the sandbox around it."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools" / "calculator"))

import main as calc  # noqa: E402
from kilix_tui import app  # noqa: E402


class ArithmeticTests(unittest.TestCase):
    def test_operators_and_precedence(self):
        for expression, expected in [
            ("2+2", 4), ("2 + 3 * 4", 14), ("(2 + 3) * 4", 20),
            ("7 // 2", 3), ("7 % 2", 1), ("2 ** 10", 1024),
            ("-5 + 3", -2), ("10 / 4", 2.5),
        ]:
            self.assertEqual(calc.evaluate(expression), expected, expression)

    def test_integral_results_print_without_a_trailing_zero(self):
        self.assertEqual(calc.format_result(calc.evaluate("10 / 5")), "2")
        self.assertEqual(calc.format_result(calc.evaluate("10 / 4")), "2.5")


class SandboxTests(unittest.TestCase):
    """The calculator is reachable from a desktop menu, so a hostile
    expression must be rejected by parsing rather than caught after the fact."""

    def test_code_execution_attempts_are_refused(self):
        for expression in [
            "__import__('os').system('touch /tmp/pwned')",
            "open('/etc/passwd').read()",
            "().__class__.__bases__[0].__subclasses__()",
            "exec('x=1')",
            "os.getcwd()",
            "lambda: 1",
            "[1,2,3]",
            "'a' * 10",
        ]:
            with self.assertRaises(calc.CalculatorError, msg=expression):
                calc.evaluate(expression)

    def test_resource_exhaustion_is_bounded(self):
        # A valid expression that would otherwise hang the pane it runs in.
        with self.assertRaises(calc.CalculatorError):
            calc.evaluate("2 ** 2 ** 30")
        with self.assertRaises(calc.CalculatorError):
            calc.evaluate("1" * 5000)

    def test_division_by_zero_is_a_message_not_a_traceback(self):
        for expression in ("1/0", "1//0", "1%0"):
            with self.assertRaises(calc.CalculatorError):
                calc.evaluate(expression)


class InterfaceTests(unittest.TestCase):
    def test_typing_and_submitting_updates_the_frame(self):
        state = calc.State()
        for char in "12+30":
            calc.handle(ord(char), state)
        calc.handle(ord("\n"), state)
        self.assertEqual(state.result, "42")
        frame = app.render_to_text(calc.render, state)
        self.assertIn("= 42", frame)
        self.assertIn("12+30 = 42", frame)

    def test_a_rejected_expression_reports_instead_of_crashing(self):
        state = calc.State()
        for char in "1/0":
            calc.handle(ord(char), state)
        calc.handle(ord("\n"), state)
        self.assertIn("division by zero", app.render_to_text(calc.render, state))

    def test_backspace_and_quit(self):
        state = calc.State()
        calc.handle(ord("9"), state)
        calc.handle(263, state)
        self.assertEqual(state.entry, "")
        self.assertFalse(calc.handle(ord("q"), state))

    def test_quit_key_is_typed_into_a_nonempty_entry(self):
        # 'q' is a quit key, but not while the user is mid-expression.
        state = calc.State(entry="4")
        self.assertTrue(calc.handle(ord("q"), state))


class HeadlessSurfaceTests(unittest.TestCase):
    def test_render_to_text_clips_to_the_surface(self):
        state = calc.State(entry="x" * 200)
        frame = app.render_to_text(calc.render, state, height=10, width=40)
        self.assertTrue(all(len(line) <= 40 for line in frame.splitlines()))
        self.assertLessEqual(len(frame.splitlines()), 10)


if __name__ == "__main__":
    unittest.main()

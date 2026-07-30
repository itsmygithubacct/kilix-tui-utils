"""kilix-calculator — a calculator that does not evaluate arbitrary Python.

The obvious implementation is `eval()`, and it is wrong: this ships on an OS
where the terminal is the desktop, so the calculator is reachable from a menu
by anyone who can reach a pane. `eval("__import__('os').system(...)")` in a
"calculator" is a real hazard, not a theoretical one.

Instead the expression is parsed with `ast` and walked over an explicit
allowlist of node types and operators. Anything outside it — a name, a call, an
attribute, a subscript — is rejected before evaluation.
"""
from __future__ import annotations

import ast
import operator
import sys
from dataclasses import dataclass, field

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0] + "/src")

from kilix_tui import app, keys as keymap, shell  # noqa: E402

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}

# Guards against a pasted expression turning into a denial of service: 2**2**30
# is a valid expression that would hang the pane it runs in.
_MAX_EXPONENT = 1024
_MAX_DIGITS = 4096


class CalculatorError(Exception):
    """A rejected or unevaluable expression."""


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(
                node.value, (int, float)):
            raise CalculatorError("only numbers are allowed")
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
            raise CalculatorError(f"exponent above {_MAX_EXPONENT}")
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise CalculatorError("division by zero")
        return _BINARY[type(node.op)](left, right)
    raise CalculatorError("unsupported expression")


def evaluate(expression: str) -> float:
    """Evaluate an arithmetic expression, or raise CalculatorError."""
    if len(expression) > _MAX_DIGITS:
        raise CalculatorError("expression too long")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise CalculatorError("syntax error") from error
    try:
        return _eval(tree)
    except CalculatorError:
        raise
    except (ArithmeticError, ValueError) as error:
        raise CalculatorError(str(error)) from error


def format_result(value: float) -> str:
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.10g}"


@dataclass
class State:
    entry: str = ""
    result: str = ""
    error: str = ""
    history: list[str] = field(default_factory=list)


def submit(state: State) -> None:
    expression = state.entry.strip()
    if not expression:
        return
    try:
        value = evaluate(expression)
    except CalculatorError as error:
        state.error, state.result = str(error), ""
        return
    state.error = ""
    state.result = format_result(value)
    state.history.insert(0, f"{expression} = {state.result}")
    del state.history[10:]
    state.entry = ""


def render(surface, state: State) -> None:
    body = shell.draw(
        surface,
        title="Calculator",
        sections=("Calculate",),
        summary="Safe arithmetic · standard operators and powers",
        footer="Enter evaluate · Backspace · q quit",
    )
    row = body.top
    shell.put(surface, row, body.left, f"> {state.entry}",
              shell.tango.attr("accent"))
    if state.error:
        shell.put(surface, row + 1, body.left, f"  {state.error}",
                  shell.tango.attr("alert"))
    elif state.result:
        shell.put(surface, row + 1, body.left, f"= {state.result}",
                  shell.tango.attr("title"))
    for index, item in enumerate(state.history):
        history_row = row + 3 + index
        if history_row >= body.bottom:
            break
        shell.put(surface, history_row, body.left + 1, item,
                  shell.tango.attr("muted"))


def handle(key: int, state: State) -> bool:
    if keymap.is_quit(key) and not state.entry:
        return False
    if key in (ord("\n"), ord("\r")):
        submit(state)
    elif key in (263, 127, 8):                      # Backspace
        state.entry = state.entry[:-1]
    elif 32 <= key < 127:
        state.entry += chr(key)
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = State()
    if argv and argv[0] not in ("--screenshot",):
        # Non-interactive use: `kilix-calculator '2+2'` prints and exits, which
        # makes the tool scriptable and keeps the safe evaluator reusable.
        try:
            print(format_result(evaluate(" ".join(argv))))
            return 0
        except CalculatorError as error:
            print(f"kilix-calculator: {error}", file=sys.stderr)
            return 1
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as handle_:
            handle_.write(app.render_to_text(render, state) + "\n")
        return 0
    return app.run(render, state, handle=handle)


if __name__ == "__main__":
    raise SystemExit(main())

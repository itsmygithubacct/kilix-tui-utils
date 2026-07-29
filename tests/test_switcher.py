"""kilix-switch: the tree it builds, and the things it must not do.

The switcher is one keystroke from a menu and it can close panes, so the tests
that matter most are the ones about restraint: it never closes without asking,
it never talks to anything but the terminal, and it never reaches the terminal
at all from a plain constructor.

The terminal itself is not available in a test run, so the model is exercised
against a recorded `kitten @ ls` payload — the same shape a live session
returns, which is what `parse()` exists to be given.
"""
import ast
import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_tui import app, chrome, kitty_rc, panel, theme  # noqa: E402


def load():
    path = ROOT / "tools" / "switcher" / "main.py"
    spec = importlib.util.spec_from_file_location("tool_switcher", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# One OS window, three pages, the second page active and its middle pane
# focused — enough to exercise scope, nesting and the focus marker.
PAYLOAD = [{
    "id": 1,
    "is_focused": True,
    "tabs": [
        {"id": 1, "title": "kilix", "windows": [
            {"id": 11, "title": "nvim", "cwd": "/tmp/kilix/config",
             "foreground_processes": [{"cmdline": ["/usr/bin/nvim"]}]},
            {"id": 12, "title": "bash", "cwd": "/tmp/kilix",
             "foreground_processes": [{"cmdline": ["/bin/bash"]}]},
        ]},
        {"id": 2, "title": "notes", "is_active": True, "windows": [
            {"id": 21, "title": "less", "cwd": "/tmp/research",
             "foreground_processes": [{"cmdline": ["/usr/bin/less"]}]},
            {"id": 22, "title": "bash", "cwd": "/tmp/research/deep",
             "is_focused": True,
             "foreground_processes": [{"cmdline": ["/bin/bash"]}]},
        ]},
        {"id": 3, "title": "build", "windows": [
            {"id": 31, "title": "make", "cwd": "/tmp/build",
             "foreground_processes": [{"cmdline": ["/usr/bin/make"]}]},
        ]},
    ],
}]


def tree():
    return kitty_rc.parse(PAYLOAD)


def state(module, **kw):
    st = module.State(tree=tree(), live=True)
    for key, value in kw.items():
        setattr(st, key, value)
    return st


class ModelTests(unittest.TestCase):
    def test_payload_becomes_pages_and_panes(self):
        got = tree()
        self.assertEqual([p.id for p in got.pages], [1, 2, 3])
        self.assertEqual(len(got.panes), 5)
        self.assertEqual([p.index for p in got.pages], [1, 2, 3])

    def test_process_name_comes_from_the_last_foreground_process(self):
        got = tree()
        self.assertEqual(got.pages[0].panes[0].process, "nvim")
        self.assertEqual(got.pages[2].panes[0].label, "make")

    def test_only_the_active_pages_focused_pane_counts_as_focused(self):
        got = tree()
        focused = got.focused_pane()
        self.assertIsNotNone(focused)
        self.assertEqual(focused.id, 22)
        self.assertEqual(got.active_page().id, 2)

    def test_home_page_prefers_the_callers_own_pane(self):
        got = tree()
        os.environ["KITTY_WINDOW_ID"] = "31"
        try:
            # Page 3 holds pane 31 and is not the active page — the point of
            # the distinction.
            self.assertEqual(got.home_page().id, 3)
        finally:
            os.environ.pop("KITTY_WINDOW_ID", None)
        self.assertEqual(got.home_page().id, 2)   # falls back to active

    def test_matching_searches_title_process_and_directory(self):
        pane = tree().pages[0].panes[0]
        for needle in ("nvim", "NVIM", "config", "kilix"):
            self.assertTrue(pane.matches(needle), needle)
        self.assertFalse(pane.matches("nothing-like-this"))


class RowTests(unittest.TestCase):
    def test_rows_nest_panes_under_their_page(self):
        module = load()
        rows = state(module).rows()
        self.assertEqual(
            [(r.kind, r.page.id, r.pane.id if r.pane else None) for r in rows],
            [("page", 1, None), ("pane", 1, 11), ("pane", 1, 12),
             ("page", 2, None), ("pane", 2, 21), ("pane", 2, 22),
             ("page", 3, None), ("pane", 3, 31)],
        )

    def test_collapsing_a_page_hides_only_its_panes(self):
        module = load()
        rows = state(module, collapsed={1}).rows()
        self.assertNotIn(11, [r.pane.id for r in rows if r.pane])
        self.assertIn(21, [r.pane.id for r in rows if r.pane])

    def test_scope_page_keeps_only_the_home_page(self):
        module = load()
        rows = state(module, scope=module.SCOPES.index("page")).rows()
        self.assertEqual({r.page.id for r in rows}, {2})

    def test_scope_elsewhere_drops_the_home_page(self):
        module = load()
        rows = state(module, scope=module.SCOPES.index("other")).rows()
        self.assertEqual({r.page.id for r in rows}, {1, 3})

    def test_filter_narrows_to_matching_panes(self):
        module = load()
        rows = state(module, filter="nvim").rows()
        self.assertEqual([r.pane.id for r in rows if r.pane], [11])

    def test_filter_that_matches_nothing_yields_no_rows(self):
        module = load()
        self.assertEqual(state(module, filter="zzzz").rows(), [])


class PathTests(unittest.TestCase):
    def test_long_paths_elide_from_the_left_and_fit(self):
        module = load()
        for budget in range(2, 40):
            got = module._short_path("/tmp/one/two/three/four/five", budget)
            self.assertLessEqual(len(got), budget, f"budget={budget}")

    def test_home_becomes_a_tilde(self):
        module = load()
        home = os.path.expanduser("~")
        self.assertEqual(module._short_path(home, 40), "~")
        self.assertTrue(
            module._short_path(os.path.join(home, "x"), 40).startswith("~/"))


class RenderTests(unittest.TestCase):
    def test_the_tree_and_the_selection_marker_appear(self):
        module = load()
        frame = app.render_to_text(module.render, state(module, cursor=1),
                                   height=24, width=100)
        self.assertIn("kilix", frame)
        self.assertIn("nvim", frame)
        self.assertIn("▶", frame)      # the cursor, in text, not only colour

    def test_focused_pane_is_marked_in_text_not_only_in_colour(self):
        module = load()
        frame = app.render_to_text(module.render, state(module),
                                   height=24, width=100)
        self.assertIn("●", frame)

    def test_collapse_marker_flips_with_the_collapsed_set(self):
        module = load()
        open_frame = app.render_to_text(module.render, state(module),
                                        height=24, width=100)
        shut_frame = app.render_to_text(module.render,
                                        state(module, collapsed={1, 2, 3}),
                                        height=24, width=100)
        self.assertIn("▾", open_frame)
        self.assertIn("▸", shut_frame)

    def test_every_mode_renders_at_every_awkward_size(self):
        module = load()
        for mode in ("browse", "filter", "rename", "confirm"):
            for height, width in ((24, 100), (24, 80), (14, 60), (10, 40),
                                  (5, 20), (3, 8), (2, 4)):
                with self.subTest(mode=mode, size=(height, width)):
                    frame = app.render_to_text(
                        module.render, state(module, mode=mode, cursor=3),
                        height=height, width=width)
                    for line in frame.splitlines():
                        self.assertLessEqual(len(line), width)
                    self.assertLessEqual(len(frame.splitlines()), height)

    def test_the_footer_never_runs_under_the_node_tag(self):
        module = load()
        # The node tag is decoration and the footer is instruction, so the tag
        # may only appear at widths where the footer is already whole.
        st = state(module)
        footer = module.footer(st)
        for width in range(60, 140):
            frame = app.render_to_text(module.render, st, height=20, width=width)
            last = frame.splitlines()[-1]
            self.assertLessEqual(len(last), width)
            if module.NODE in last:
                self.assertIn(
                    footer, last,
                    f"at width {width} the tag displaced part of the footer")

    def test_it_says_so_when_there_is_no_terminal(self):
        module = load()
        blank = module.State(message="not running inside a Kilix terminal")
        frame = app.render_to_text(module.render, blank, height=20, width=90)
        self.assertIn("not running inside a Kilix terminal", frame)


class InputTests(unittest.TestCase):
    def test_q_quits_from_browse(self):
        module = load()
        self.assertFalse(module.handle(ord("q"), state(module)))

    def test_movement_stays_inside_the_list(self):
        module = load()
        st = state(module)
        for _ in range(50):
            module.handle(258, st)          # KEY_DOWN
        self.assertIsNotNone(st.current())
        self.assertLess(st.cursor, len(st.rows()))
        for _ in range(50):
            module.handle(259, st)          # KEY_UP
        self.assertEqual(st.cursor, 0)

    def test_slash_starts_filtering_and_escape_clears_it(self):
        module = load()
        st = state(module)
        module.handle(ord("/"), st)
        self.assertEqual(st.mode, "filter")
        for char in "nvim":
            module.handle(ord(char), st)
        self.assertEqual(st.filter, "nvim")
        self.assertEqual([r.pane.id for r in st.rows() if r.pane], [11])
        module.handle(27, st)
        self.assertEqual((st.filter, st.mode), ("", "browse"))

    def test_typing_q_while_filtering_does_not_quit(self):
        module = load()
        st = state(module, mode="filter")
        self.assertTrue(module.handle(ord("q"), st))
        self.assertEqual(st.filter, "q")

    def test_tab_cycles_scope(self):
        module = load()
        st = state(module)
        seen = []
        for _ in range(len(module.SCOPES) + 1):
            seen.append(st.scope_name)
            module.handle(ord("\t"), st)
        self.assertEqual(seen[:-1], list(module.SCOPES))
        self.assertEqual(seen[-1], module.SCOPES[0])

    def test_left_and_right_collapse_and_expand(self):
        module = load()
        st = state(module, cursor=0)
        module.handle(260, st)              # KEY_LEFT
        self.assertIn(1, st.collapsed)
        module.handle(261, st)              # KEY_RIGHT
        self.assertNotIn(1, st.collapsed)


class SafetyTests(unittest.TestCase):
    """The properties that make this safe to bind to a key."""

    def test_x_only_arms_a_confirmation_and_never_closes(self):
        module = load()
        st = state(module)
        calls = []
        original = kitty_rc.close_pane
        kitty_rc.close_pane = lambda pane_id: calls.append(pane_id)
        try:
            module.handle(ord("x"), st)
            self.assertEqual(st.mode, "confirm")
            self.assertEqual(calls, [], "pressing x must not close anything")
            module.handle(ord("n"), st)     # anything but y
            self.assertEqual(st.mode, "browse")
            self.assertEqual(calls, [], "only y may close")
        finally:
            kitty_rc.close_pane = original

    def test_confirming_closes_exactly_what_was_selected(self):
        module = load()
        st = state(module, cursor=1, mode="confirm")   # first pane of page 1
        calls = []
        original_pane, original_page = kitty_rc.close_pane, kitty_rc.close_page
        kitty_rc.close_pane = lambda pane_id: calls.append(("pane", pane_id))
        kitty_rc.close_page = lambda page_id: calls.append(("page", page_id))
        try:
            module.handle(ord("y"), st)
        finally:
            kitty_rc.close_pane, kitty_rc.close_page = original_pane, original_page
        self.assertEqual(calls, [("pane", 11)])

    def test_a_refused_close_becomes_a_message_not_a_crash(self):
        module = load()
        st = state(module, cursor=1, mode="confirm")
        original = kitty_rc.close_pane

        def refuse(pane_id):
            raise kitty_rc.Unavailable("Remote control is not allowed")

        kitty_rc.close_pane = refuse
        try:
            self.assertTrue(module.handle(ord("y"), st))
        finally:
            kitty_rc.close_pane = original
        self.assertIn("refused", st.message)

    def test_constructing_a_state_never_talks_to_the_terminal(self):
        module = load()
        original = kitty_rc.tree
        kitty_rc.tree = lambda: self.fail("State() must not query the terminal")
        try:
            module.State()
        finally:
            kitty_rc.tree = original

    # Matched on the qualified call, not the bare name: this repository's own
    # `app.run()` is the event loop, and counting it as an exec would make the
    # check meaningless.
    EXEC_NAMES = ("run", "Popen", "call", "check_output", "check_call")

    @staticmethod
    def _subprocess_sites(relative):
        tree_ = ast.parse((ROOT / relative).read_text())
        out = []
        for node in ast.walk(tree_):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in SafetyTests.EXEC_NAMES:
                if getattr(func.value, "id", "") in ("subprocess", "os"):
                    out.append(node)
        return out

    def test_the_tool_itself_never_shells_out(self):
        source = (ROOT / "tools/switcher/main.py").read_text()
        self.assertEqual(self._subprocess_sites("tools/switcher/main.py"), [],
                         "the switcher must go through kitty_rc, not exec")
        for forbidden in ("import subprocess", "os.system", "os.exec", "os.spawn"):
            self.assertNotIn(forbidden, source)

    def test_the_only_external_command_is_the_terminals_own_kitten(self):
        """One call site, and the command it runs is the terminal's kitten."""
        sites = self._subprocess_sites("src/kilix_tui/kitty_rc.py")
        self.assertEqual(len(sites), 1, "exactly one place may exec")

        # That site runs a list built in the same function; its first element
        # must be the kitten, never a literal command name.
        tree_ = ast.parse((ROOT / "src/kilix_tui/kitty_rc.py").read_text())
        commands = [
            node.value for node in ast.walk(tree_)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.List)
            and node.value.elts
            and any(getattr(t, "id", "") == "command" for t in node.targets)
        ]
        self.assertEqual(len(commands), 1)
        first = commands[0].elts[0]
        self.assertIsInstance(first, ast.Call)
        self.assertEqual(getattr(first.func, "id", ""), "_kitten")

    def test_the_preview_never_reads_scrollback(self):
        source = (ROOT / "src/kilix_tui/kitty_rc.py").read_text()
        self.assertIn('"--extent", "screen"', source)
        self.assertNotIn('"all"', source)


class PanelTests(unittest.TestCase):
    """The drawing vocabulary, asserted as pictures rather than coordinates."""

    def setUp(self):
        os.environ["KILIX_PANEL"] = "1"
        theme.reset_panel_pairs()

    def tearDown(self):
        os.environ.pop("KILIX_PANEL", None)
        theme.reset_panel_pairs()

    def test_a_block_is_filled_and_its_corners_are_shaved(self):
        surface = app.TextSurface(height=5, width=12)
        panel.block(surface, 1, 1, 3, 8, "primary", label="AB", ident="07")
        shape = surface.attr_shape().splitlines()
        # Every cell of the block carries an attribute; the corners carry the
        # shave glyph, which is what rounds them.
        self.assertEqual(len(shape[1].strip()), 8)
        self.assertEqual(surface.lines[1][1], "▗")
        self.assertEqual(surface.lines[3][1], "▝")
        self.assertIn("AB", surface.lines[1])
        self.assertIn("07", surface.lines[3])

    def test_the_elbow_joins_a_leg_to_a_bar(self):
        surface = app.TextSurface(height=8, width=30)
        panel.elbow(surface, 0, 0, 6, 24, "quaternary", thickness=2, stem=5)
        rows = [line.rstrip() for line in surface.attr_shape().splitlines()]
        # The bar runs the full width on the top two rows...
        self.assertEqual(len(rows[0]), 24)
        self.assertEqual(len(rows[1]), 24)
        # ...and below it only the leg remains.
        self.assertEqual(len(rows[3].rstrip()), 5)
        # The outer corner is shaved and the inner corner is notched.
        self.assertEqual(surface.lines[0][0], "▗")
        self.assertEqual(surface.lines[2][5], "▘")

    def test_segments_are_separated_by_a_black_gap(self):
        surface = app.TextSurface(height=1, width=20)
        panel.bar(surface, 0, 0, 20, [(4, "primary"), (4, "tertiary")])
        row = surface.attrs[0]
        self.assertNotEqual(row[0], 0)
        self.assertEqual(row[4], 0, "a gap must separate the segments")
        self.assertNotEqual(row[5], 0)

    def test_a_pill_has_rounded_caps_around_its_label(self):
        surface = app.TextSurface(height=1, width=20)
        used = panel.pill(surface, 0, 0, "GO", "primary")
        self.assertEqual(surface.lines[0][0], "▐")
        self.assertIn("GO", surface.lines[0])
        self.assertEqual(surface.lines[0][used - 1], "▌")

    def test_the_readout_is_stable_for_a_seed(self):
        one, two = app.TextSurface(height=3, width=20), app.TextSurface(height=3, width=20)
        panel.readout(one, 0, 0, 3, 20, seed=5)
        panel.readout(two, 0, 0, 3, 20, seed=5)
        self.assertEqual(str(one), str(two))
        three = app.TextSurface(height=3, width=20)
        panel.readout(three, 0, 0, 3, 20, seed=6)
        self.assertNotEqual(str(one), str(three))

    def test_nothing_is_styled_when_the_palette_is_unavailable(self):
        os.environ["KILIX_PANEL"] = "0"
        theme.reset_panel_pairs()
        surface = app.TextSurface(height=5, width=12)
        panel.block(surface, 1, 1, 3, 8, "primary", label="AB")
        self.assertTrue(all(not value for row in surface.attrs for value in row))
        self.assertIn("AB", surface.lines[1], "the text must survive anyway")

    def test_the_active_spine_section_is_marked_in_text(self):
        # Without the palette the fill swap is invisible, so which scope is on
        # has to survive as a character.
        os.environ["KILIX_PANEL"] = "0"
        theme.reset_panel_pairs()
        page = chrome.Page("T", ["ONE", "TWO", "THREE"])
        surface = app.TextSurface(height=24, width=100)
        page.render(surface, 1)
        text = str(surface)
        self.assertIn("▶TWO", text)
        self.assertNotIn("▶ONE", text)
        self.assertNotIn("▶THREE", text)

    def test_the_spine_is_dropped_rather_than_squeezed(self):
        page = chrome.Page("T", ["ONE", "TWO"])
        wide = app.TextSurface(height=24, width=100)
        page.render(wide, 0)
        self.assertTrue(page.spined)
        narrow = app.TextSurface(height=24, width=50)
        page.render(narrow, 0)
        self.assertFalse(page.spined)
        top, left, _, _ = page.content_box()
        self.assertEqual((top, left), (1, 0), "the well takes the whole width")


if __name__ == "__main__":
    unittest.main(verbosity=2)

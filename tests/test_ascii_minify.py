"""tools/ascii_minify.py -- the ASCII pass the pipe build ships through.

The pass exists because a sandboxed QMT reads strategy files with the locale
codec and its editor re-encodes pasted text, so a single non-ASCII byte can
fail the load (2026-10-13: ``SyntaxError: 'gbk' codec can't decode byte 0xfe
... (<string>, line 0)``). It is a source-to-source rewrite, so the contract
these tests pin down is "same program, no comments, no docstrings, ASCII only".
"""

import ast
import io
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import ascii_minify


def _run(source):
    """Exec minified source and return its namespace."""
    namespace = {}
    exec(compile(source, "<minified>", "exec"), namespace)
    return namespace


class StripTest(unittest.TestCase):
    def test_comments_gone_code_kept(self):
        source = "# leading\nx = 1  # 中文注释\n# trailing\n"
        result, report = ascii_minify.minify(source)
        self.assertNotIn("#", result)
        self.assertEqual(report["comments_removed"], 3)
        self.assertEqual(_run(result)["x"], 1)

    def test_coding_cookie_on_line_one_survives(self):
        source = "#coding:gbk\nx = 1\n"
        result, _report = ascii_minify.minify(source)
        self.assertTrue(result.startswith("#coding:gbk"))

    def test_docstrings_gone_and_emptied_blocks_get_pass(self):
        source = '"""module doc"""\n\n\nclass A:\n    """class doc"""\n\n\ndef f():\n    """fn doc"""\n    return 1\n'
        result, report = ascii_minify.minify(source)
        self.assertNotIn("doc", result)
        self.assertEqual(report["pass_inserted"], 1)  # only class A was emptied
        namespace = _run(result)
        self.assertEqual(namespace["f"](), 1)
        self.assertIsInstance(namespace["A"], type)

    def test_stray_string_statement_is_dropped_like_a_docstring(self):
        # The flat builder prepends ``global`` lines, which turns real module
        # docstrings into plain string statements -- they must go too.
        source = "def f():\n    global g\n    g = 1\n    \"\"\"stray doc\"\"\"\n    return g\n"
        result, report = ascii_minify.minify(source)
        self.assertEqual(report["string_statements_removed"], 1)
        self.assertNotIn("stray", result)
        self.assertEqual(_run(result)["f"](), 1)

    def test_string_used_by_code_is_kept(self):
        source = 'def f():\n    return __name__ + "suffix"\nX = f()\n'
        result, _report = ascii_minify.minify(source)
        self.assertIn('"suffix"', result)

    def test_expression_statement_string_becomes_pass_keyword_kept(self):
        source = "def f():\n    \"doc\"\n"
        result, _report = ascii_minify.minify(source)
        self.assertEqual(_run(result)["f"](), None)


class AsciiTest(unittest.TestCase):
    def test_non_ascii_literals_are_escaped_and_keep_their_value(self):
        source = 'MSG = "中文消息"\nT = ("多行"\n     "文本")\n'
        result, report = ascii_minify.minify(source)
        self.assertEqual(report["literals_escaped"], 3)  # two strings + one join
        self.assertTrue(result.isascii())
        namespace = _run(result)
        self.assertEqual(namespace["MSG"], "中文消息")
        self.assertEqual(namespace["T"], "多行文本")

    def test_escaped_output_parses_to_the_same_ast(self):
        source = 'A = "中文"\nB = 1 + 2\n'
        result, _report = ascii_minify.minify(source)
        self.assertEqual(
            ast.dump(ast.parse(result)), ast.dump(ast.parse(source)))

    def test_f_string_literal_part_is_escaped(self):
        source = 'NAME = "世界"\nMSG = f"你好 {NAME}"\n'
        result, _report = ascii_minify.minify(source)
        self.assertTrue(result.isascii())
        self.assertEqual(_run(result)["MSG"], "你好 世界")

    def test_non_ascii_inside_f_string_field_is_refused(self):
        source = 'MSG = f"{\'中文\'}"\n'
        with self.assertRaises(ascii_minify.MinifyError):
            ascii_minify.minify(source)

    def test_non_ascii_in_raw_string_is_refused_instead_of_silently_wrong(self):
        source = 'PATTERN = r"\\d中文"\n'
        with self.assertRaises(ascii_minify.MinifyError):
            ascii_minify.minify(source)

    def test_non_ascii_outside_a_literal_is_refused(self):
        source = "中文变量 = 1\n"
        with self.assertRaises(ascii_minify.MinifyError):
            ascii_minify.minify(source)


class BlankLineTest(unittest.TestCase):
    def test_blank_lines_outside_strings_are_dropped(self):
        source = "x = 1\n\n\ny = 2\n"
        result, report = ascii_minify.minify(source)
        self.assertEqual(result, "x = 1\ny = 2\n")
        self.assertEqual(report["blank_lines_removed"], 2)

    def test_blank_lines_inside_a_multiline_string_survive(self):
        source = 'TEXT = """line1\n\nline3"""\n'
        result, report = ascii_minify.minify(source)
        self.assertIn("line1\n\nline3", result)
        self.assertEqual(report["blank_lines_removed"], 0)

    def test_keep_blank_lines_flag(self):
        source = "x = 1\n\n\ny = 2\n"
        result, _report = ascii_minify.minify(source, drop_blank_lines=False)
        self.assertEqual(result, source)


class CliTest(unittest.TestCase):
    def test_cli_writes_ascii_and_check_mode_leaves_the_file_alone(self):
        import tempfile

        handle, path = tempfile.mkstemp(suffix=".py")
        os.close(handle)
        try:
            with io.open(path, "wb") as stream:
                stream.write('X = 1  # 注释\nY = "中文"\n'.encode("utf-8"))
            self.assertEqual(ascii_minify.main([path]), 0)
            with io.open(path, "rb") as stream:
                data = stream.read()
            self.assertTrue(data.isascii())
            self.assertEqual(ascii_minify.main([path, "--check"]), 0)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()

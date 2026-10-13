# -*- coding: utf-8 -*-
"""Turn a generated single-file build into pure ASCII (no comments, no docstrings).

Why: a broker QMT sandbox reads strategy files with the locale codec (GBK)
whatever the file declares, and its editor re-encodes pasted text before it
saves. Any non-ASCII byte in the build is therefore a coin flip
(2026-10-13 incident: ``SyntaxError: 'gbk' codec can't decode byte 0xfe in
position 199496: illegal multibyte sequence (<string>, line 0)`` on the 1.5 MB
all-in-one build). A file that contains no byte above 0x7F decodes identically
under every codec, which removes that entire failure class -- and it is also
~15% smaller, which helps sandboxes with a size limit.

Steps, in order:

1. drop ``COMMENT`` tokens (a coding cookie on line 1/2 survives);
2. drop standalone string-expression statements -- real docstrings, plus the
   "stray" strings the flat builder turns them into when it moves a module
   body into ``def _mod_N():`` -- inserting ``pass`` when that would otherwise
   empty a block;
3. escape every remaining non-ASCII character inside a string literal as
   ``\\uXXXX`` / ``\\UXXXXXXXX`` (identical runtime value, ASCII source);
4. drop blank lines that are not inside a multi-line string.

Every run is verified before the result is handed back: the AST must be
unchanged once the same bare-string statements are removed from the input AST,
the output must compile, and the output must contain no byte above 0x7F.

CLI: ``python tools/ascii_minify.py <in.py> [<out.py>]`` (in place when
``out`` is omitted), ``--check`` verifies without writing, ``--keep-blank-lines``
disables step 4.
"""
import argparse
import ast
import io
import os
import re
import sys
import tokenize


CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-_.a-zA-Z0-9]+)")
PREFIX_RE = re.compile(r"^([A-Za-z]*)('''|\"\"\"|'|\")")
_SKIP_KINDS = (tokenize.NL, tokenize.INDENT, tokenize.DEDENT)


class MinifyError(Exception):
    """Raised instead of writing a build that did not verify."""


def _line_starts(text):
    """Offset of every line start, plus a sentinel for "one past the end"."""
    starts = []
    total = 0
    for line in text.splitlines(True):
        starts.append(total)
        total += len(line)
    starts.append(total)
    return starts


def _offset(starts, pos):
    row, col = pos
    if row - 1 >= len(starts):
        return starts[-1]
    return min(starts[row - 1] + col, starts[-1])


def _apply(text, edits):
    """Apply ``(start, end, replacement)`` edits, right to left."""
    out = text
    for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
        out = out[:start] + replacement + out[end:]
    return out


def _tokens(text):
    return list(tokenize.generate_tokens(io.StringIO(text).readline))


def _literal_prefix(literal):
    match = PREFIX_RE.match(literal)
    if not match:
        raise MinifyError("cannot parse string literal: %r" % literal[:80])
    return match.group(1).lower()


def _next_significant(toks, index):
    """First token at/after ``index`` that is neither COMMENT nor NL."""
    while index < len(toks):
        if toks[index][0] not in (tokenize.COMMENT, tokenize.NL):
            return index
        index += 1
    return None


def _pass1(text):
    """Drop comments and standalone string statements."""
    starts = _line_starts(text)
    lines = text.splitlines(True)
    toks = _tokens(text)
    edits = []
    stats = {"comments": 0, "string_statements": 0, "passes": 0}
    at_line_start = True
    index = 0
    while index < len(toks):
        tok = toks[index]
        kind = tok[0]
        if kind == tokenize.COMMENT:
            row = tok[2][0]
            line = lines[row - 1] if row - 1 < len(lines) else ""
            cookie = (
                row <= 2
                and line.lstrip().startswith("#")
                and CODING_RE.match(line) is not None
            )
            if not cookie:
                edits.append((_offset(starts, tok[2]), _offset(starts, tok[3]), ""))
                stats["comments"] += 1
            index += 1
            continue
        if kind == tokenize.NEWLINE:
            at_line_start = True
            index += 1
            continue
        if kind in _SKIP_KINDS:
            index += 1
            continue
        if kind == tokenize.STRING and at_line_start:
            prefix = _literal_prefix(tok[1])
            # f-strings evaluate their replacement fields, raw strings would
            # keep the \uXXXX escape we are about to add: leave both alone.
            if "f" not in prefix and "r" not in prefix:
                after = _next_significant(toks, index + 1)
                if after is not None and toks[after][0] == tokenize.NEWLINE:
                    end_row = toks[after][2][0]
                    start_row = tok[2][0]
                    indent = lines[start_row - 1][: tok[2][1]]
                    replacement = ""
                    following = _next_significant(toks, after + 1)
                    if following is None or toks[following][0] in (
                        tokenize.DEDENT,
                        tokenize.ENDMARKER,
                    ):
                        # docstring was the only statement of its block
                        replacement = indent + "pass\n"
                        stats["passes"] += 1
                    edits.append(
                        (
                            _offset(starts, (start_row, 0)),
                            _offset(starts, (end_row + 1, 0)),
                            replacement,
                        )
                    )
                    stats["string_statements"] += 1
                    at_line_start = True
                    index = after + 1
                    continue
        at_line_start = False
        index += 1
    return _apply(text, edits), stats


def _escape_text(chunk):
    out = []
    for char in chunk:
        code = ord(char)
        if code < 0x80:
            out.append(char)
        elif code <= 0xFFFF:
            out.append("\\u%04x" % code)
        else:
            out.append("\\U%08x" % code)
    return "".join(out)


def _escape_fstring_body(body):
    """Escape literal text of an f-string, refusing non-ASCII in {expressions}."""
    out = []
    depth = 0
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char == "{" and depth == 0:
            if body.startswith("{{", index):
                out.append("{{")
                index += 2
                continue
            depth = 1
            out.append(char)
            index += 1
            continue
        if char == "}" and depth == 0:
            if body.startswith("}}", index):
                out.append("}}")
                index += 2
                continue
            out.append(char)
            index += 1
            continue
        if depth:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            if ord(char) > 127:
                raise MinifyError(
                    "non-ASCII inside an f-string replacement field: %r" % body
                )
            out.append(char)
            index += 1
            continue
        out.append(_escape_text(char))
        index += 1
    return "".join(out)


def _escape_literal(literal):
    match = PREFIX_RE.match(literal)
    prefix, quote = match.group(1), match.group(2)
    body = literal[match.end():]
    if not body.endswith(quote):
        raise MinifyError("unterminated string literal: %r" % literal[:80])
    body = body[: -len(quote)]
    low = prefix.lower()
    if "b" in low:
        raise MinifyError("non-ASCII in a bytes literal: %r" % literal[:80])
    if "r" in low:
        raise MinifyError("non-ASCII in a raw literal (needs a manual fix): %r" % literal[:80])
    if "f" in low:
        return prefix + quote + _escape_fstring_body(body) + quote
    return prefix + quote + _escape_text(body) + quote


def _pass2(text):
    """Escape every non-ASCII character left inside a string literal.

    Python 3.12 tokenizes f-strings as FSTRING_START / FSTRING_MIDDLE /
    FSTRING_END instead of one STRING token (PEP 701), and this tool is run by
    whatever interpreter the developer has -- 3.10 in one shell, 3.12 in the
    venv. Both must reach the same decision, because the escape has to stay
    valid for QMT's 3.6 too: a non-ASCII character inside a replacement field
    means escaping it would put a backslash in an f-string expression, which
    3.6-3.11 reject, so it is refused on every interpreter.
    """
    starts = _line_starts(text)
    edits = []
    escaped = 0
    fstring_start = getattr(tokenize, "FSTRING_START", None)
    fstring_middle = getattr(tokenize, "FSTRING_MIDDLE", None)
    fstring_end = getattr(tokenize, "FSTRING_END", None)
    in_field = 0
    in_fstring = False
    for tok in _tokens(text):
        if fstring_start is not None and tok[0] == fstring_start:
            in_fstring = True
            continue
        if fstring_end is not None and tok[0] == fstring_end:
            in_fstring = False
            in_field = 0
            continue
        if fstring_middle is not None and tok[0] == fstring_middle:
            if any(ord(char) > 127 for char in tok[1]):
                edits.append(
                    (_offset(starts, tok[2]), _offset(starts, tok[3]), _escape_text(tok[1]))
                )
                escaped += 1
            continue
        if in_fstring:
            if tok[0] == tokenize.OP and tok[1] == "{":
                in_field += 1
                continue
            if tok[0] == tokenize.OP and tok[1] == "}":
                in_field -= 1
                continue
            if in_field and any(ord(char) > 127 for char in tok[1]):
                raise MinifyError(
                    "non-ASCII inside an f-string replacement field: %r at %s"
                    % (tok[1][:60], tok[2])
                )
            continue
        if tok[0] == tokenize.STRING:
            if any(ord(char) > 127 for char in tok[1]):
                edits.append(
                    (_offset(starts, tok[2]), _offset(starts, tok[3]), _escape_literal(tok[1]))
                )
                escaped += 1
            continue
        if tok[0] == tokenize.COMMENT:
            continue
        if any(ord(char) > 127 for char in tok[1]):
            raise MinifyError(
                "non-ASCII outside a comment or string literal: %r at %s"
                % (tok[1][:60], tok[2])
            )
    return _apply(text, edits), escaped


def _pass3(text):
    """Drop blank lines, keeping the ones that sit inside a multi-line string."""
    lines = text.splitlines(True)
    protected = set()
    for tok in _tokens(text):
        if tok[0] == tokenize.STRING and tok[2][0] != tok[3][0]:
            protected.update(range(tok[2][0], tok[3][0] + 1))
    kept = []
    dropped = 0
    for number, line in enumerate(lines, 1):
        if number in protected or line.strip():
            kept.append(line)
        else:
            dropped += 1
    return "".join(kept), dropped


class _DropBareStrings(ast.NodeTransformer):
    """Remove ``Expr(Constant(str))`` statements -- what step 2 deletes."""

    def visit_Expr(self, node):
        self.generic_visit(node)
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return None
        return node


class _DropPass(ast.NodeTransformer):
    """Remove ``pass`` statements -- step 2 inserts them into emptied blocks."""

    def visit_Pass(self, node):
        return None


def _normalized_dump(text):
    tree = ast.parse(text)
    for transformer in (_DropBareStrings(), _DropPass()):
        transformer.visit(tree)
    ast.fix_missing_locations(tree)
    return ast.dump(tree)


def _first_difference(before, after):
    for index, (left, right) in enumerate(zip(before, after)):
        if left != right:
            return index
    return min(len(before), len(after))


def _diff_window(before, after, width=160):
    index = _first_difference(before, after)
    start = max(0, index - width)
    return (
        "at %d\n  before: %s\n  after:  %s"
        % (index, before[start : index + width], after[start : index + width])
    )


def minify(text, drop_blank_lines=True, verify=True):
    """Return ``(ascii_source, report)`` for a generated single-file build."""
    if verify:
        before_dump = _normalized_dump(text)
    after_comments, stats = _pass1(text)
    after_escapes, escaped = _pass2(after_comments)
    if drop_blank_lines:
        result, blanks = _pass3(after_escapes)
    else:
        result, blanks = after_escapes, 0

    bad = [char for char in result if ord(char) > 127]
    if bad:
        raise MinifyError("non-ASCII survived minification: %r" % "".join(bad[:20]))
    if not result.endswith("\n"):
        result += "\n"
    if verify:
        after_dump = _normalized_dump(result)
        if after_dump != before_dump:
            raise MinifyError(
                "AST changed -- refusing to hand back a different program; first difference "
                + _diff_window(before_dump, after_dump)
            )
    compile(result, "<ascii-minify>", "exec")

    report = {
        "bytes_in": len(text.encode("utf-8", "surrogateescape")),
        "bytes_out": len(result.encode("ascii")),
        "comments_removed": stats["comments"],
        "string_statements_removed": stats["string_statements"],
        "pass_inserted": stats["passes"],
        "literals_escaped": escaped,
        "blank_lines_removed": blanks,
    }
    return result, report


def format_report(report):
    return (
        "ASCII build: %(bytes_in)d -> %(bytes_out)d bytes (-%(saved)d), "
        "comments=%(comments_removed)d string-statements=%(string_statements_removed)d "
        "pass=%(pass_inserted)d escaped-literals=%(literals_escaped)d "
        "blank-lines=%(blank_lines_removed)d"
        % dict(report, saved=report["bytes_in"] - report["bytes_out"])
    )


def _read(path):
    with open(path, "rb") as handle:
        data = handle.read()
    for encoding in ("utf-8-sig", "gbk", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise MinifyError("cannot decode %s as utf-8 or gbk" % path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source")
    parser.add_argument("target", nargs="?")
    parser.add_argument("--check", action="store_true", help="verify only, write nothing")
    parser.add_argument("--keep-blank-lines", action="store_true")
    args = parser.parse_args(argv)

    text = _read(args.source)
    result, report = minify(text, drop_blank_lines=not args.keep_blank_lines)
    print(format_report(report))
    if args.check:
        return 0
    target = args.target or args.source
    with open(target, "wb") as handle:
        handle.write(result.encode("ascii"))
    print("WROTE %s" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())

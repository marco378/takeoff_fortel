"""A small, deliberately narrow Excel evaluator for the quotation workbook.

There is no LibreOffice on this box and openpyxl stores formulas as text, so a test that only
reads the workbook proves nothing about what the client opens: the #DIV/0! that Aryan found on
16 Sep 2026 lived entirely in the formulas and every unit test passed straight through it.

This evaluates the exact grammar the quotation writes and NOTHING else -- cell references,
ranges, + - * / ( ), numbers, and SUM / ROUND / IF with an ``=`` comparison. Anything it does
not recognise raises, so a new formula shape cannot be silently skipped; that is the failure
mode this file exists to prevent, and a widened grammar is a deliberate act.
"""
import re

_REF = re.compile(r"^\$?([A-Z]{1,3})\$?([0-9]{1,7})$")


class FormulaError(Exception):
    """An Excel error value (#DIV/0!, #REF!, #VALUE!) produced while evaluating."""


def _col_index(letters):
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index


def _split_top_level(text, sep=","):
    parts, depth, current = [], 0, ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == sep and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return parts


def _balanced(text):
    """True when ``text`` is one parenthesised group, e.g. the argument list of one call."""
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index == len(text) - 1
    return False


class Sheet:
    """Evaluates a worksheet's formulas, caching results and detecting reference cycles."""

    def __init__(self, ws):
        self.ws = ws
        self._cache = {}
        self._active = set()

    # -- public -----------------------------------------------------------------
    def value(self, coordinate):
        """The computed value of a cell. Blank cells are 0, as Excel treats them."""
        coordinate = coordinate.replace("$", "").upper()
        if coordinate in self._cache:
            return self._cache[coordinate]
        if coordinate in self._active:
            raise FormulaError(f"circular reference at {coordinate}")
        match = _REF.match(coordinate)
        if not match:
            raise FormulaError(f"unparsable reference {coordinate!r}")
        raw = self.ws.cell(int(match.group(2)), _col_index(match.group(1))).value
        self._active.add(coordinate)
        try:
            result = self._evaluate(raw)
        finally:
            self._active.discard(coordinate)
        self._cache[coordinate] = result
        return result

    def error_cells(self):
        """Every cell whose formula does not evaluate, as {coordinate: reason}."""
        errors = {}
        for row in self.ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    try:
                        self.value(cell.coordinate)
                    except FormulaError as exc:
                        errors[cell.coordinate] = str(exc)
        return errors

    # -- internals --------------------------------------------------------------
    def _evaluate(self, raw):
        if raw is None or raw == "":
            return 0
        if isinstance(raw, (int, float)):
            return raw
        if isinstance(raw, str) and raw.startswith("="):
            return self._expression(raw[1:])
        return raw

    def _range(self, text):
        start, end = text.split(":")
        first, last = _REF.match(start.replace("$", "")), _REF.match(end.replace("$", ""))
        if not first or not last:
            raise FormulaError(f"unparsable range {text!r}")
        values = []
        for column in range(_col_index(first.group(1)), _col_index(last.group(1)) + 1):
            for row in range(int(first.group(2)), int(last.group(2)) + 1):
                cell = self.ws.cell(row, column)
                values.append(self.value(cell.coordinate))
        return values

    def _numbers(self, values):
        return [v for v in values if isinstance(v, (int, float))]

    def _call(self, name, argument_text):
        args = _split_top_level(argument_text)
        if name == "SUM":
            total = 0
            for arg in args:
                arg = arg.strip()
                if ":" in arg:
                    total += sum(self._numbers(self._range(arg)))
                else:
                    value = self._expression(arg)
                    total += value if isinstance(value, (int, float)) else 0
            return total
        if name == "ROUND":
            if len(args) != 2:
                raise FormulaError("ROUND takes two arguments")
            return round(self._expression(args[0]), int(self._expression(args[1])))
        if name == "IF":
            if len(args) != 3:
                raise FormulaError("IF takes three arguments")
            return (self._expression(args[1]) if self._condition(args[0])
                    else self._expression(args[2]))
        raise FormulaError(f"unsupported function {name}")

    def _condition(self, text):
        parts = _split_top_level(text, "=")
        if len(parts) != 2:
            raise FormulaError(f"unsupported condition {text!r}")
        left_text, right_text = parts[0].strip(), parts[1].strip()
        # Excel reads an EMPTY cell as "" in a comparison and as 0 in arithmetic. The
        # quotation writes =IF(D53="","",...) for an assessor-rate row, so getting this wrong
        # would report the blank-rate row as priced.
        if right_text == '""' and _REF.match(left_text.replace("$", "")):
            match = _REF.match(left_text.replace("$", ""))
            raw = self.ws.cell(int(match.group(2)), _col_index(match.group(1))).value
            return raw is None or raw == ""
        return self._expression(left_text) == self._expression(right_text)

    def _resolve_calls(self, text):
        """Replace every function call in ``text`` with its computed value."""
        while True:
            match = re.search(r"\b([A-Z]+)\(", text)
            if not match:
                return text
            depth, index = 0, match.end() - 1
            for index in range(match.end() - 1, len(text)):
                if text[index] == "(":
                    depth += 1
                elif text[index] == ")":
                    depth -= 1
                    if depth == 0:
                        break
            else:
                raise FormulaError(f"unbalanced parentheses in {text!r}")
            value = self._call(match.group(1), text[match.end():index])
            if isinstance(value, str):
                if text.strip() == text[match.start():index + 1].strip():
                    return value
                raise FormulaError(f"text result inside an expression: {text!r}")
            text = text[:match.start()] + repr(float(value)) + text[index + 1:]

    def _expression(self, text):
        text = text.strip()
        if not text:
            return 0
        if text.startswith('"') and text.endswith('"'):
            return text[1:-1]
        whole = re.fullmatch(r"([A-Z]+)\((.*)\)", text, re.S)
        if whole and _balanced(text[len(whole.group(1)):]):
            # A single call spanning the whole expression may return text ("OK"), which no
            # arithmetic path can carry.
            return self._call(whole.group(1), whole.group(2))
        resolved = self._resolve_calls(text)
        if isinstance(resolved, str) and not re.fullmatch(
                r"[0-9eE.+\-*/()$A-Z: ]*", resolved):
            raise FormulaError(f"unsupported expression {text!r}")
        if not isinstance(resolved, str):
            return resolved
        tokens = re.split(r"([+\-*/()])", resolved)
        rebuilt = []
        for token in tokens:
            stripped = token.strip()
            if not stripped:
                continue
            if stripped in "+-*/()":
                rebuilt.append(stripped)
            elif _REF.match(stripped.replace("$", "")):
                value = self.value(stripped)
                rebuilt.append(repr(float(value) if isinstance(value, (int, float)) else 0.0))
            else:
                try:
                    rebuilt.append(repr(float(stripped)))
                except ValueError:
                    raise FormulaError(f"unsupported token {stripped!r} in {text!r}")
        joined = "".join(rebuilt)
        try:
            return eval(joined, {"__builtins__": {}}, {})  # noqa: S307 - grammar above
        except ZeroDivisionError:
            raise FormulaError("#DIV/0!")
        except SyntaxError:
            raise FormulaError(f"unparsable expression {text!r}")

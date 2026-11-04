"""Tests for parse() in parsers/sexpr.py: the rules of the KiCad lexer."""

import re

import pytest

from kicad_workbench.parsers.sexpr import (
    Atom,
    Node,
    SExprError,
    indent,
    parse,
    property_atoms,
    property_field,
)


def tokens(node: Node) -> list:
    """The items of a list: the text of a token, or the tokens of a list."""
    return [item.text if isinstance(item, Atom) else tokens(item) for item in node.items]


class TestTokens:
    def test_strings_numbers_and_bare_tokens(self):
        root = parse("(at -1.27 2e-3 90) ")
        assert tokens(root) == ["at", "-1.27", "2e-3", "90"]
        assert root.number(0) == -1.27 and root.number(1) == 0.002

    def test_string_with_escapes(self):
        root = parse(r'(label "A \"B\" \\C")')
        atom = root.atoms()[0]
        assert atom.quoted and atom.value == 'A "B" \\C'

    def test_parentheses_in_a_string_are_text(self):
        root = parse('(property "Value" "(1) x)")')
        assert root.value(1) == "(1) x)"

    def test_quote_in_a_bare_token_starts_no_string(self):
        # DSNLEXER ends a bare token only at a space or a parenthesis.
        root = parse('(x a"b c" (y"z) "s")')
        assert tokens(root) == ["x", 'a"b', 'c"', ['y"z'], '"s"']
        assert root.children()[0].head == 'y"z'

    def test_spaces(self):
        root = parse("(a\tb\r\nc\0d)")
        assert tokens(root) == ["a", "b", "c", "d"]

    def test_carriage_return_is_no_line_break_in_a_string(self):
        # The lexer reads lines that end at LF.
        assert parse('(a "x\ry")').value() == "x\ry"


class TestComments:
    def test_line_that_starts_with_hash_is_a_comment(self):
        root = parse('(a\n  # (b "c\n d)\n')
        assert tokens(root) == ["a", "d"]

    def test_hash_after_a_token_is_a_token(self):
        assert tokens(parse("(a #b)")) == ["a", "#b"]
        assert parse('(property "Reference" "#PWR01")').value(1) == "#PWR01"

    def test_comment_line_before_the_head(self):
        root = parse("(\n  # note (x\n  foo 1 2)")
        assert root.head == "foo"
        assert [atom.text for atom in root.atoms()] == ["1", "2"]

    def test_indented_comment_outside_the_list(self):
        assert parse("  # header\n(a)\n\t# end").head == "a"

    def test_comments_outside_the_list(self):
        assert parse("# header\n(a)\n# end\n").head == "a"


class TestErrors:
    @pytest.mark.parametrize(
        ("text", "message", "offset"),
        [
            ("(a (b)", "'(' with no ')'", 0),
            ("(a))", "')' with no '('", 3),
            ('(a "b)', "a string with no end on its line", 3),
            ('(a "b\nc")', "a string with no end on its line", 3),
            ('(a "b\\\nc")', "a string with no end on its line", 3),
            ("(a) (b)", "a second top-level list", 4),
            ("x (a)", "a token outside the top-level list", 0),
            ("(a) x", "a token outside the top-level list", 4),
            ("", "no list", 0),
            ("# only a comment\n", "no list", 17),
        ],
    )
    def test_error_and_offset(self, text, message, offset):
        with pytest.raises(SExprError, match=re.escape(message)) as error:
            parse(text)
        assert error.value.offset == offset


class TestTree:
    TEXT = (
        "(kicad_pcb (version 20260206)\n"
        '  (footprint "R:R_0603" (layer "F.Cu") (at 10 -20 90)\n'
        '    (property "Reference" "R1") (pad "1" smd) (pad "2" smd))\n'
        '  (segment (start 0 0) (end 1 1) (net "GND")))\n'
    )

    def test_offsets_are_those_of_the_source_text(self):
        root = parse(self.TEXT)
        for node in root.walk():
            assert self.TEXT[node.start] == "(" and self.TEXT[node.end - 1] == ")"
            for item in node.items:
                if isinstance(item, Atom):
                    assert self.TEXT[item.start : item.end] == item.text

    def test_queries(self):
        root = parse(self.TEXT)
        footprint = root.first("footprint")
        assert footprint.value() == "R:R_0603"
        assert footprint.first("layer").value() == "F.Cu"
        assert [footprint.first("at").number(i) for i in range(3)] == [10, -20, 90]
        assert footprint.first("at").number(3) is None
        assert [pad.value() for pad in footprint.children("pad")] == ["1", "2"]
        assert root.first("zone") is None
        assert [node.head for node in root.children()] == ["version", "footprint", "segment"]

    def test_walk_is_in_the_order_of_the_text(self):
        root = parse(self.TEXT)
        assert [node.head for node in root.walk("pad")] == ["pad", "pad"]
        heads = [node.head for node in root.first("segment").walk()]
        assert heads == ["segment", "start", "end", "net"]

    def test_number_that_is_not_a_number(self):
        with pytest.raises(ValueError):
            parse("(layer F.Cu)").number()

    @pytest.mark.parametrize(
        "text, value", [("1.", 1.0), (".5", 0.5), ("-.5", -0.5), ("1e3", 1000.0), ("2E-2", 0.02)]
    )
    def test_numbers_that_all_kicad_versions_read(self, text, value):
        assert parse(f"(at {text})").number() == value

    @pytest.mark.parametrize(
        "text", ["+1", "1_0", "0x1p3", "inf", "nan", "1e", "1e999", "1e-400", '" 1"', "1,5"]
    )
    def test_numbers_that_kicad_versions_read_in_different_ways(self, text):
        # strtod, std::from_chars and fast_float read only the start of a token,
        # and they do not agree on these forms or on a value out of range.
        with pytest.raises(ValueError, match="is not a number that KiCad reads"):
            parse(f"(at {text})").number()

    def test_list_with_no_head(self):
        root = parse('(a ("x" 1))')
        assert root.children()[0].head is None
        assert [atom.text for atom in root.children()[0].atoms()] == ['"x"', "1"]


class TestIndent:
    def test_tabs_for_each_level(self):
        text = '(symbol "A"\n      (pin\n  (at 0 0)\n\n   )\n (property "P" ")(")\n)'
        # A line that starts with ")" gets the indent of the list that it closes.
        assert indent(text, 2) == (
            '\t\t(symbol "A"\n\t\t\t(pin\n\t\t\t\t(at 0 0)\n\t\t\t)\n'
            '\t\t\t(property "P" ")(")\n\t\t)'
        )

    def test_same_tree(self):
        text = '(a (b "x\\"(" c)\n# (comment\n  (d e))'
        assert indent(text, 1).startswith("\t(a")
        tree = parse(indent(text, 1))
        assert [node.head for node in tree.walk()] == ["a", "b", "d"]
        assert tree.first("b").value(0) == 'x"('


class TestProperty:
    def test_private_field(self):
        prop = parse('(property private "Name" "v" (at 0 0 0))')
        assert [atom.value for atom in property_atoms(prop)] == ["Name", "v"]
        assert property_field(prop) == ("Name", "v")

    def test_source(self):
        text = '(a (property "N" ""))'
        prop = parse(text).first("property")
        assert prop.source is text
        assert property_field(prop) == ("N", "")

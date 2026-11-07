"""Tests for the model of the confirmed MPNs (parsers/confirmed_mpns.py).

The tests of split_value, field_list, format_mpns and symbol_state are the tests
of datasheet-lens (tests/test_project.py: test_split_value, test_symbol_state,
test_symbol_state_sixth_review_cases, test_review_cases_of_2026_11_05), with no
change of the cases: the two projects must give the same results.
"""

from kicad_workbench.parsers.confirmed_mpns import (
    CadSymbol,
    ValueParts,
    _words,
    bom_symbols,
    check_symbols,
    field_items,
    field_list,
    format_mpns,
    split_value,
    symbol_state,
)


def state(value, mpn="", confirmed=None, mpn_confirmed=None, **kw):
    return symbol_state(
        CadSymbol(
            reference="Q1",
            value=value,
            mpn=mpn,
            value_confirmed=confirmed,
            mpn_confirmed=mpn_confirmed,
            **kw,
        )
    )


class TestSplitValue:
    def test_split_value(self):
        """The convention of the parentheses: the MPNs are the text in parentheses, the
        other text is the general value."""
        assert split_value("2N7002 (2N7002LT1G, BSS138LT1G)") == ValueParts(
            "2N7002", ["2N7002LT1G", "BSS138LT1G"]
        )
        assert split_value("100nF  10V (GRM155R71A104KA01D) X7R") == ValueParts(
            "100nF 10V X7R", ["GRM155R71A104KA01D"]
        )
        assert split_value('("BC847B,215"; BC857B)') == ValueParts("", ["BC847B,215", "BC857B"])
        # One MPN with a packing code.
        assert split_value("Q (BC847B,215)") == ValueParts("Q", ["BC847B,215"])
        # Quotes: each character in an MPN, "" is a quote; a quote only at the start of
        # an item.
        assert split_value('J ("A(1)", "B;2", "5"" LCD")').mpns == ["A(1)", "B;2", '5" LCD']
        assert split_value('5" LCD (ICL7660"CPA)') == ValueParts('5" LCD', ['ICL7660"CPA'])
        assert split_value("U (ABC 123)").mpns == ["ABC 123"]  # a space needs no quotes
        assert split_value("4,7k 10V") == ValueParts("4,7k 10V", None)  # no parentheses
        assert split_value("10k (1%)").mpns == ["1%"]  # each text in parentheses: MPNs
        assert split_value("10k ()") == ValueParts("10k", [])
        assert split_value("~") == ValueParts("", None)
        for text, error in (
            ("a (b) (c)", "more than one text in parentheses"),
            ("a (b", "not closed"),
            ("a ) b (", "not opened"),
            ("a (b))", "not opened"),
            ("a (b(1))", "a parenthesis in an MPN"),
            ('a ("b)', "a quote that is not closed"),
            ('a ("b" c)', "a text after a quote"),
        ):
            parts = split_value(text)
            assert parts.mpns is None and error in parts.error
            assert "Write the MPNs in one text" in parts.error
        # A list that is not clear: an error, and one item, the full text (no MPN is
        # lost).
        parts = split_value("Q (BC847B,215, BSS138) X")
        assert (parts.general, parts.mpns) == ("Q X", ["BC847B,215, BSS138"])
        assert "a comma before three digits can be a separator" in parts.error
        # The spaces around an item do not count, also in quotes; an item of spaces is
        # empty.
        assert split_value('Q (" A ", " ", B)').mpns == ["A", "B"]

    def test_field_list_and_words(self):
        assert field_list("A,100), B") == (["A", "100)", "B"], "")  # ")" is text here
        # A general value compares by its words (decision 26): the spaces between words
        # do not count, the words and their case count.
        assert _words(" 100nF   10V ") == _words("100nF 10V") == "100nF 10V"
        assert _words("10 k") != _words("10k") and _words("4m7") != _words("4M7")
        assert _words("10k (1%)") != _words("10k(1%)")
        assert field_items('2N7002,2N7000; "BC847B,215"') == ["2N7002", "2N7000", "BC847B,215"]
        assert field_items("A|B or C") == ["A|B or C"]  # the separators are , and ;
        assert field_items("ABC(1)") == ["ABC(1)"]  # no quotes for a parenthesis here
        # A list that is not clear: one item, the full text (no MPN is lost), and an
        # error.
        items, error = field_list("BC847B,215, BSS138")
        assert items == ["BC847B,215, BSS138"]
        assert "write an MPN with , ; or a parenthesis in quotes" in error
        assert field_list('"BC847B,215", "BC857B,215"') == (["BC847B,215", "BC857B,215"], "")
        assert field_items("BC847B,215") == ["BC847B,215"]  # one MPN with a packing code
        assert field_items("100nF, 100") == ["100nF", "100"]  # no comma in the code: a space

    def test_format_mpns(self):
        """Quotes only where they are necessary."""
        assert format_mpns(["BC847B,215"]) == "BC847B,215"
        assert format_mpns(["A", "B"]) == "A, B"
        assert format_mpns(["BC847B,215", "BSS138"]) == '"BC847B,215", BSS138'
        assert format_mpns(["A(1)", 'B"C']) == 'A(1), B"C'
        assert format_mpns(['"A', "B;C"]) == '"""A", "B;C"'
        # The text gives the items again.
        assert format_mpns(["~"]) == '"~"' and field_list('"~"') == (["~"], "")


class TestSymbolState:
    def test_symbol_state(self):
        """The model of plans/confirmed-mpns.md (decisions 26 to 30): Value is the
        general value, MPN the MPNs of the designer, Value_Confirmed and MPN_Confirmed
        the copies of the last confirmation. The confirmation of the user is the truth:
        until the user confirms a change, the confirmed MPNs give the parts."""
        both = "2N7002LT1G, BSS138LT1G"
        # Confirmed: the same general value (spaces do not count) and the same list (the
        # separators and the quotes that are not necessary do not count).
        s = state("2N7002", both, "2N7002 ", "2N7002LT1G; BSS138LT1G")
        assert (s.parts, s.change, s.unconfirmed, s.move) == (
            ["2N7002LT1G", "BSS138LT1G"],
            "",
            False,
            {},
        )
        assert not state("Q", '"A", B', "Q", "A, B").change
        # Not confirmed: an empty MPN_Confirmed or Value_Confirmed.
        s = state("2N7002", both, "", "")
        assert (s.parts, s.unconfirmed, s.write_mpn, s.new) == (
            ["2N7002LT1G", "BSS138LT1G"],
            True,
            both,
            ["2N7002LT1G", "BSS138LT1G"],
        )
        assert state("2N7002", both, "2N7002", "").unconfirmed
        assert state("2N7002", both, "", both).unconfirmed
        assert state("2N7002", both, None, "").unconfirmed  # no column Value_Confirmed
        assert not state("2N7002", both).unconfirmed  # no confirmed columns: nothing to write
        # A change of the part: the confirmed MPNs stay the parts until the confirmation.
        s = state("2N7002", "2N7002LT1G", "2N7002", both)
        assert (s.parts, s.new, s.removed, s.write_mpn) == (
            ["2N7002LT1G", "BSS138LT1G"],
            [],
            ["BSS138LT1G"],
            "2N7002LT1G",
        )
        assert s.change == (
            "MPN '2N7002LT1G', MPN_Confirmed '2N7002LT1G, BSS138LT1G': the MPN is not the "
            "confirmed MPN"
        )
        assert state("2N7002", "BSS138LT1G, 2N7002LT1G", "2N7002", both).change  # other order
        assert state("Q", "bss138lt1g", "Q", "BSS138LT1G").change  # case counts
        s = state("2N7002", "", "2N7002", both)  # the field MPN is empty after a confirmation
        assert (s.parts, s.removed) == (
            ["2N7002LT1G", "BSS138LT1G"],
            ["2N7002LT1G", "BSS138LT1G"],
        )
        assert s.change
        # A change of the value and of the part.
        s = state("220nF", "GRM2", "100nF", "GRM1")
        assert (s.parts, s.new, s.removed, s.select) == (["GRM1"], ["GRM2"], ["GRM1"], False)
        assert "the value and the part are not the confirmed ones" in s.change
        # A new general value with the confirmed MPN: a new part, or the same part.
        s = state("220nF", "GRM1", "100nF", "GRM1")
        assert (s.parts, s.select, s.write_mpn) == (["GRM1"], True, "GRM1")
        assert s.change == "Value '220nF', Value_Confirmed '100nF': the MPN 'GRM1' can be wrong now"
        assert state("10 k", "A", "10k", "A").change  # the words count
        assert state("220nF", "", "100nF", "").change.endswith(": no MPN is confirmed")
        s = state("TestPoint2", "-", "TestPoint", "-")
        assert "the symbol needs no part (MPN '-'): this can be wrong now" in s.change
        assert s.write_mpn == "-"
        assert not s.select  # no new part for a symbol that needs no part
        # The old convention (decision 30): a list of MPNs in the parentheses of Value and
        # no MPN_Confirmed. The move keeps the state: the old field MPN is the confirmed
        # copy.
        s = state("2N7002 (2N7002LT1G, BSS138LT1G)", "2N7002LT1G", "2N7002 (2N7002LT1G)")
        assert s.move == {
            "value": "2N7002",
            "mpn": both,
            "mpn_confirmed": "2N7002LT1G",
            "value_confirmed": "2N7002",
            "old": "Value",
        }
        assert (s.parts, s.change, s.unconfirmed) == (["2N7002LT1G"], "", False)
        assert state("LM358 (LM358DR)").move == {
            "value": "LM358",
            "mpn": "LM358DR",
            "mpn_confirmed": "",
            "value_confirmed": None,
            "old": "Value",
        }
        assert state('J ("A(1)")', "A(1)", "", "").move["mpn"] == "A(1)"
        assert not state("10k (1%)", "", "10k (1%)", "-").move  # MPN_Confirmed: new convention
        # Other MPNs in the field MPN: the new convention (review of 2026-11-07), also
        # with no MPN_Confirmed: no move, and "1%" is no part.
        s = state("10k (1%)", "RC0603FR-0710KL", "", "")
        assert (s.move, s.parts, s.unconfirmed) == ({}, ["RC0603FR-0710KL"], True)
        assert state("10k (1%)", "RC0603FR-0710KL").parts == ["RC0603FR-0710KL"]
        # "(TBD)": the old convention with an empty list.
        assert state("10k (TBD)").move == {
            "value": "10k",
            "mpn": "",
            "mpn_confirmed": "",
            "value_confirmed": None,
            "old": "Value",
        }
        assert not state("10k (A) (B)").move and not state("10k (GRM1, -)").move  # no list
        # A symbol with an MPN_Confirmed: no move, the parentheses are text.
        assert state("10k (1%)", "RC1", "10k (1%)", "RC1").parts == ["RC1"]
        # No part selected; no part needed.
        assert state("100nF", confirmed="", mpn_confirmed="").no_mpn
        assert state("100nF", "TBD", "100nF", "TBD").no_mpn
        s = state("TestPoint", "-", "TestPoint", "-")
        assert (s.parts, s.no_mpn, s.no_part, s.conflicts, s.change) == (
            [],
            False,
            True,
            [],
            "",
        )
        assert state("TestPoint", "-", "", "").unconfirmed  # a no-part text, not confirmed yet
        # Conflicts: a list error, a no-part text and an MPN in one list, more MPN columns.
        assert "a comma before three digits" in state("Q", "BC847B,215, BSS138").conflicts[0]
        s = state("Q", "X", "Q", "BC847B,215, BSS138")
        assert "MPN_Confirmed 'BC847B,215, BSS138': a comma before three digits" in s.conflicts[0]
        assert s.parts == ["BC847B,215, BSS138"]  # the confirmed MPNs (one item: none lost)
        s = state("Q", '"BC847B,215", BSS138', other_mpns=[("MP", "BC847B,215, BSS138")])
        assert "MP 'BC847B,215, BSS138': a comma before three digits" in s.conflicts[0]
        assert state("Q", "GRM1, -").conflicts == [
            "MPN GRM1, -: - tells that the symbol needs no part"
        ]
        s = state("2N7002", "2N7002LT1G", "2N7002", "2N7002LT1G", other_mpns=[("MP", "BSS138LT1G")])
        assert (s.parts, s.extra, s.conflicts) == (
            ["2N7002LT1G", "BSS138LT1G"],
            ["BSS138LT1G"],
            ["MPN 2N7002LT1G, MP BSS138LT1G"],
        )
        # Value holds no MPN.
        assert not state("Q", "X", "Q", "X", other_values=["Q (BC847B,215)"]).extra
        # No MPN_Confirmed: the MPNs of the old convention (also of an other Value
        # column) are parts (no MPN is lost).
        assert state("Q (X)", "X", other_values=["Q (Y)"]).parts == ["X", "Y"]

    def test_review_cases(self):
        """The cases of the reviews of datasheet-lens (2026-11-05), in the model of the
        four fields."""
        s = symbol_state(
            CadSymbol(
                value="74HC595D (74HC595D,118)",
                mpn="74HC595D,118",
                value_confirmed="74HC595D (74HC595D,118)",
            )
        )
        assert (s.parts, s.change, s.conflicts) == (["74HC595D,118"], "", [])
        s = symbol_state(CadSymbol(value="74HC595D", mpn="74HC595D/T3", value_confirmed="74HC595D"))
        assert (s.parts, s.conflicts) == (["74HC595D/T3"], [])
        # TBD: an empty field MPN (no part selected yet); N/A: the symbol needs no part.
        s = state("100nF", "TBD", "100nF", "TBD")
        assert (s.no_part, s.no_mpn, s.parts, s.conflicts) == (False, True, [], [])
        s = state("100nF", "N/A", "100nF", "N/A")
        assert (s.no_part, s.no_mpn, s.conflicts) == (True, False, [])
        s = state("2N7002", "TBD, 2N7002LT1G", "2N7002", "TBD, 2N7002LT1G")
        assert (s.parts, s.conflicts) == (["2N7002LT1G"], [])  # TBD does not tell "no part"
        # A list of MPNs has no decimal comma; an MPN can have no digit; the text of
        # KiCad for different values is not an MPN.
        assert state("Q", "2N7002,2N7000").parts == ["2N7002", "2N7000"]
        assert state("J", "PJ-SMD").parts == ["PJ-SMD"]
        assert state("10k", "-- mixed values --").conflicts == [
            "MPN -- mixed values --: not an MPN"
        ]
        # TBD is an empty field: it has nothing to confirm.
        s = state("10k", "TBD", "", "")
        assert (s.unconfirmed, s.no_mpn) == (False, True)
        # A field MPN that is empty now, after a confirmation: a change, not a first one.
        s = state("10k", "", "10k", "A")
        assert (s.parts, s.removed, s.unconfirmed) == (["A"], ["A"], False)
        assert s.change == "MPN '(empty)', MPN_Confirmed 'A': the MPN is not the confirmed MPN"
        # A symbol with a conflict gets no proposal, and no "no part selected".
        s = state("10k", "-- mixed values --", "", "")
        assert (s.no_mpn, s.unconfirmed, s.change) == (False, False, "")
        s = symbol_state(CadSymbol(value="10k", mpn="A", other_mpns=[("MP", "B")]))
        assert (s.parts, s.unconfirmed) == (["A", "B"], False)
        # A new general value with the confirmed MPN: a new part, or the same part.
        s = state("220nF", "GRM1", "100nF", "GRM1")
        assert (s.select, s.removed, s.parts) == (True, [], ["GRM1"])
        # The confirmation writes the text of MPN: MPN_Confirmed is a copy.
        assert state("10k", "A, A", "", "").write_mpn == "A, A"
        # Two equal MPNs are one part, and no change.
        s = state("2N7002", "2N7002LT1G, 2N7002LT1G", "2N7002", "2N7002LT1G, 2N7002LT1G")
        assert (s.parts, s.change, s.conflicts) == (["2N7002LT1G"], "", [])
        # A change that removes the MPNs names them.
        s = state("100nF", "", "100nF", "2N7002LT1G")
        assert s.removed == ["2N7002LT1G"]

    def test_decisions_32_and_33(self):
        """Decision 32: the old convention only in Value_Confirmed (the user removed the
        parentheses from Value) gets a move of the confirmed copies. Decision 33: a new
        general value with an MPN that the user never confirmed is a change, not select
        (test_decisions_32_and_33 of datasheet-lens)."""
        move = {
            "value": None,
            "mpn": None,
            "mpn_confirmed": "A1",
            "value_confirmed": "x",
            "old": "Value_Confirmed",
        }
        for mpn_confirmed in (None, ""):
            s = state("x", "A1", "x (A1)", mpn_confirmed)
            assert (s.move, s.parts, s.change, s.select) == (move, ["A1"], "", False)
        # A new value: a change after the move.
        assert state("y", "A1", "x (A1)").move["value_confirmed"] == "x"
        assert state("x", "A1, B1", "x (A1; B1)").move["mpn_confirmed"] == "A1, B1"
        s = state("x", "B1", "x (A1)")  # an other MPN: no move, a change of the value
        assert (s.move, bool(s.change)) == ({}, True)
        assert not state("x", "A1", "x (A1) (B)").move  # no valid list
        assert not state("x", "A1", "x (A1)", "A1").move  # MPN_Confirmed: the new convention
        assert not state("x (A1)", "A1", "x (A1)", "A1").move
        # An other Value column shows the old convention itself (decision 32: Value has no
        # parentheses).
        s = symbol_state(
            CadSymbol(value="x", mpn="A1", value_confirmed="x (A1)", other_values=["x (-, B1)"])
        )
        assert (s.move["old"], s.extra) == ("Value_Confirmed", [])
        # An empty Value is a removal: a change, no move (review of 2026-11-07).
        s = state("", "A1", "x (A1)", "")
        assert (s.move, bool(s.change)) == ({}, True)
        # A variant line with an other Value writes the same confirmed copies: one move.
        result = check_symbols(
            [
                CadSymbol(reference="Q1", value="x", mpn="A1", value_confirmed="x (A1)"),
                CadSymbol(
                    reference="Q1", value="y", mpn="A1", value_confirmed="x (A1)", variant="b"
                ),
            ]
        )
        assert [(m.reference, m.variant) for m in result.move] == [("Q1", "")]
        s = state("220nF", "D1", "100nF", "")
        assert (s.select, s.new, s.parts) == (False, ["D1"], ["D1"])
        assert s.change == "Value '220nF', Value_Confirmed '100nF': the MPN 'D1' is not confirmed"
        assert state("220nF", "D1", "100nF").select  # no field MPN_Confirmed: not known
        # The move writes only the confirmed copies; the change has the new MPN.
        result = check_symbols(
            [
                CadSymbol(
                    reference="Q1", value="x", mpn="A1", value_confirmed="x (A1)", mpn_confirmed=""
                ),
                CadSymbol(
                    reference="C1",
                    value="220nF",
                    mpn="D1",
                    value_confirmed="100nF",
                    mpn_confirmed="",
                ),
            ]
        )
        (q1,) = result.move
        assert (q1.fields, q1.old) == (
            {"MPN_Confirmed": "A1", "Value_Confirmed": "x"},
            "Value_Confirmed",
        )
        (c1,) = result.changes
        assert (c1.select, c1.new, c1.text) == (
            False,
            ["D1"],
            "Value '220nF', Value_Confirmed '100nF': the MPN 'D1' is not confirmed. New: D1.",
        )


class TestBomSymbols:
    def test_columns(self):
        """The roles of the columns of a BOM of kicad-cli, as bom_parts of
        datasheet-lens reads them."""
        rows = [
            [
                "Reference",
                "Value",
                "MPN",
                "Manufacturer Part Number",
                "Value_Confirmed",
                "mpn",
                "MPN_Confirmed",
            ],
            ["Q1", "2N7002", "2N7002LT1G", "", "2N7002", "", "2N7002LT1G"],
            ["Q2", "BSS138", "", "BSS138LT1G", "~", "", "~"],
            ["Q3", "BSS138", "BSS138LT1G", "BSS138", "", "", ""],
            ["TP1", "TestPoint", "-", "", "", "", ""],
            ["R1", "", "", "", "", "", ""],
            ["R2", "10k", "-", "RC0603", "", "", ""],
            ["R3", "10k", "A", "", "", "B", ""],
        ]
        symbols = bom_symbols(rows, "Lite")
        assert [s.reference for s in symbols] == ["Q1", "Q2", "Q3", "TP1", "R2", "R3"]
        q1, q2, q3, tp1, r2, r3 = symbols
        assert (q1.mpn, q1.value_confirmed, q1.mpn_confirmed, q1.variant, q1.conflict) == (
            "2N7002LT1G",
            "2N7002",
            "2N7002LT1G",
            "Lite",
            "",
        )
        # The first filled MPN column is the field MPN; "~" is empty.
        assert (q2.mpn, q2.value_confirmed, q2.mpn_confirmed, q2.other_mpns) == (
            "BSS138LT1G",
            "",
            "",
            [],
        )
        assert q3.other_mpns == [("Manufacturer Part Number", "BSS138")]
        assert symbol_state(q3).conflicts == ["MPN BSS138LT1G, Manufacturer Part Number BSS138"]
        assert (tp1.mpn, tp1.conflict) == ("-", "")
        # A column with "no part" and a column with an MPN: a conflict.
        assert (r2.mpn, r2.conflict) == ("RC0603", "Manufacturer Part Number RC0603, MPN -")
        # Two columns of one name with no regard to case: MPN and mpn.
        assert r3.other_mpns == [("mpn", "B")]
        # No column of a confirmed field: None.
        (r4,) = bom_symbols([["Reference", "Value", "MPN"], ["R4", "10k", "A"]])
        assert (r4.value_confirmed, r4.mpn_confirmed) == (None, None)

    def test_field_names_of_a_write(self):
        """A write goes into the field that holds the MPN of the line, or into the
        name that the project uses (test_write_field_names of datasheet-lens)."""
        rows = [
            [
                "Reference",
                "Value",
                "Manufacturer Part Number",
                "Value Confirmed",
                "MPN_Confirmed",
            ],
            ["U1", "LM358", "LM358DR", "", ""],
            ["U2", "LM358 (LM358DT)", "", "", ""],
        ]
        result = check_symbols(bom_symbols(rows))
        (u1,) = result.unconfirmed
        assert list(u1.fields.items()) == [
            ("MPN_Confirmed", "LM358DR"),
            ("Value Confirmed", "LM358"),
        ]
        (u2,) = result.move
        assert list(u2.fields.items()) == [
            ("Value", "LM358"),
            ("Manufacturer Part Number", "LM358DT"),
            ("MPN_Confirmed", ""),
            ("Value Confirmed", ""),
        ]
        # Two filled MPN columns: the move goes into the two, in the order of MPN_FIELDS
        # (as datasheet-lens). No MPN in the line and more names: the user selects the
        # field.
        rows = [
            ["Reference", "Value", "MP", "Manufacturer Part Number"],
            ["U1", "X", "A", "A"],
            ["U2", "X (B)", "", ""],
        ]
        u1, u2 = bom_symbols(rows)
        assert (u1.mpn_fields, u1.mpn_known) == (["Manufacturer Part Number", "MP"], True)
        assert (u2.mpn_fields, u2.mpn_known) == (["Manufacturer Part Number", "MP"], False)
        assert (u1.confirmed_fields, u1.confirmed_known) == (["Value_Confirmed"], True)
        assert (u1.mpn_confirmed_fields, u1.mpn_confirmed_known) == (["MPN_Confirmed"], True)
        result = check_symbols([u1, u2])
        (m2,) = result.move
        assert (m2.fields, m2.choices) == (
            {"Value": "X", "MPN_Confirmed": ""},
            [("the MPN", "B", ["Manufacturer Part Number", "MP"])],
        )
        assert (result.changes, result.unconfirmed) == ([], [])
        # One name of the role (also in an other spelling): known.
        rows = [["Reference", "Value", "mpn"], ["U2", "X (B)", ""]]
        (u2,) = bom_symbols(rows)
        assert (u2.mpn_fields, u2.mpn_known) == (["mpn"], True)

    def test_write_fields_of_the_roles(self):
        """A "no part" mark, no MPN column, two names of Value_Confirmed or of
        MPN_Confirmed (test_write_fields_of_the_roles of datasheet-lens)."""
        rows = [
            ["Reference", "Value", "MP", "Value_Confirmed", "Value Confirmed", "MPN_Confirmed"],
            ["TP1", "TP", "-", "", "", "-"],
            ["U1", "X (A)", "", "", "", ""],
        ]
        tp1, u1 = bom_symbols(rows)
        assert (tp1.mpn_fields, tp1.mpn_known) == (["MP"], True)  # the column of the mark
        assert (tp1.mpn_confirmed_fields, tp1.mpn_confirmed_known) == (["MPN_Confirmed"], True)
        assert (u1.confirmed_fields, u1.confirmed_known) == (
            ["Value_Confirmed", "Value Confirmed"],
            False,
        )
        (u1,) = bom_symbols([["Reference", "Value", "Value_Confirmed"], ["U1", "X (A)", ""]])
        assert (u1.mpn_fields, u1.mpn_known) == (["MPN"], True)  # no MPN column
        rows = [["Reference", "Value", "MPN_Confirmed", "MPN Confirmed"], ["U1", "X", ""]]
        (u1,) = bom_symbols(rows)
        assert (u1.mpn_confirmed_fields, u1.mpn_confirmed_known) == (
            ["MPN_Confirmed", "MPN Confirmed"],
            False,
        )

    def test_write_targets_of_a_variant(self):
        """A variant line that writes into other fields than the default line gives its
        own message (test_write_targets_of_a_variant of datasheet-lens)."""
        result = check_symbols(
            [
                CadSymbol(
                    reference="U1",
                    value="LM358",
                    mpn="LM358DR",
                    value_confirmed="",
                    mpn_confirmed="",
                ),
                CadSymbol(
                    reference="U1",
                    value="LM358",
                    mpn="LM358DR",
                    value_confirmed="",
                    mpn_confirmed="",
                    variant="b",
                    mpn_confirmed_fields=["MC"],
                ),
            ]
        )
        assert [list(c.fields)[0] for c in result.unconfirmed] == ["MPN_Confirmed", "MC"]

    def test_no_confirmed_columns(self):
        """With no confirmed columns, the tool has nothing to write: no change of a
        part is found."""
        (symbol,) = bom_symbols([["Reference", "Value", "MPN"], ["R1", "10k", "A"]])
        s = symbol_state(symbol)
        assert (s.parts, s.change, s.unconfirmed, s.move) == (["A"], "", False, {})

    def test_confirmed_columns_with_different_texts(self):
        rows = [
            ["Reference", "Value", "Value_Confirmed", "Value Confirmed"],
            ["R1", "10k", "10k", "1k"],
        ]
        (symbol,) = bom_symbols(rows)
        assert symbol.conflict == "Value_Confirmed 10k, Value Confirmed 1k"
        # MPN_Confirmed compares the items of the list.
        rows = [
            ["Reference", "Value", "MPN", "MPN_Confirmed", "MPN Confirmed"],
            ["R1", "10k", "A, B", "A, B", "A; B"],
            ["R2", "10k", "A", "A", "B"],
        ]
        r1, r2 = bom_symbols(rows)
        assert (r1.conflict, r2.conflict) == ("", "MPN_Confirmed A, MPN Confirmed B")

    def test_line_with_only_confirmed_fields(self):
        """The user removed Value and MPN of a confirmed symbol: a change (review of
        2026-11-07)."""
        rows = [
            ["Reference", "Value", "MPN", "MPN_Confirmed", "Value_Confirmed"],
            ["R1", "", "", "A1", "10k"],
            ["R2", "", "", "", ""],
        ]
        (r1,) = bom_symbols(rows)
        (change,) = check_symbols([r1]).changes
        assert change.text == (
            "Value '', Value_Confirmed '10k'; MPN '(empty)', MPN_Confirmed 'A1': the value and "
            "the part are not the confirmed ones. Not in MPN now: A1."
        )
        assert symbol_state(r1).parts == ["A1"]  # the confirmed part until the confirmation


class TestCheckSymbols:
    def test_messages(self):
        """The messages of check_parts of datasheet-lens: a change, a symbol that is not
        confirmed, the move of the old convention, a symbol with no part, the variants
        and the power symbols (test_check_tells_about_changes of datasheet-lens)."""

        def symbol(reference, value, mpn, confirmed, mpn_confirmed, variant=""):
            return CadSymbol(
                reference=reference,
                value=value,
                mpn=mpn,
                value_confirmed=confirmed,
                mpn_confirmed=mpn_confirmed,
                variant=variant,
            )

        result = check_symbols(
            [
                symbol("Q1", "2N7002", "2N7002LT1G, BSS138LT1G", "2N7002", "2N7002LT1G"),
                symbol("U1", "ESP32-S3", "ESP32-S3-WROOM-1-N16R8", "", ""),
                symbol("TP1", "TestPoint_Small", "-", "TestPoint", "-"),
                symbol("#PWR01", "+3V3", "", "", ""),
                symbol("R1", "10k", "", "", ""),
                symbol(
                    "R5", "10k (RC0603FR-0710KL)", "RC0603FR-0710KL", "10k (RC0603FR-0710KL)", None
                ),
                symbol("C5", "220nF", "GRM1", "100nF", "GRM1"),
                symbol("Q2", "BSS138", "BSS138LT1G", "BSS138", "BSS138LT1G", "b"),
                symbol("Q2", "BSS138", "BSS138LT1G", "BSS138", "BSS138LT1G"),
                symbol("Q3", "BSS138", "BSS138LT1G", "BSS138", "BSS138LT1G"),
                symbol("Q3", "2N7002", "2N7002LT1G", "BSS138", "BSS138LT1G", "b"),
            ]
        )
        q1, tp1, c5, q3 = result.changes
        assert (q1.reference, q1.variant, q1.select) == ("Q1", "", False)
        assert q1.text == (
            "MPN '2N7002LT1G, BSS138LT1G', MPN_Confirmed '2N7002LT1G': the MPN is not the "
            "confirmed MPN. New: BSS138LT1G."
        )
        assert q1.fields == {
            "MPN_Confirmed": "2N7002LT1G, BSS138LT1G",
            "Value_Confirmed": "2N7002",
        }
        assert (tp1.reference, tp1.select, tp1.fields["MPN_Confirmed"]) == ("TP1", False, "-")
        assert c5.select and c5.fields == {"MPN_Confirmed": "GRM1", "Value_Confirmed": "220nF"}
        assert c5.text == "Value '220nF', Value_Confirmed '100nF': the MPN 'GRM1' can be wrong now."
        assert (q3.reference, q3.variant) == ("Q3", "b")  # Q2: the same as the default
        assert q3.text.endswith("New: 2N7002LT1G. Not in MPN now: BSS138LT1G.")
        (u1,) = result.unconfirmed
        assert u1.text == " Not confirmed: ESP32-S3-WROOM-1-N16R8."
        assert u1.fields == {
            "MPN_Confirmed": "ESP32-S3-WROOM-1-N16R8",
            "Value_Confirmed": "ESP32-S3",
        }
        (r5,) = result.move
        assert list(r5.fields.items()) == [
            ("Value", "10k"),
            ("MPN", "RC0603FR-0710KL"),
            ("MPN_Confirmed", "RC0603FR-0710KL"),
            ("Value_Confirmed", "10k"),
        ]
        assert result.no_mpn == ["R1"]  # not #PWR01
        assert (result.lines, result.no_value_confirmed, result.no_mpn_confirmed) == (6, 0, 1)
        assert result.conflicts == []

    def test_no_confirmed_fields(self):
        """With no field Value_Confirmed or MPN_Confirmed, a confirmation does not
        write it. A reference of more variants counts one time."""
        result = check_symbols(
            [
                CadSymbol(reference="C1", value="100nF", mpn="GRM188R71H104KA93D"),
                CadSymbol(reference="C1", value="220nF", mpn="GRM188R71H104KA93D", variant="b"),
                CadSymbol(reference="C2", value="1uF (GRM188R61A105KA61D)"),
                CadSymbol(
                    reference="C3", value="1uF", mpn="GRM188R61A105KA61D", value_confirmed=""
                ),
            ]
        )
        assert (result.no_value_confirmed, result.no_mpn_confirmed) == (2, 2)
        (c2,) = result.move
        assert c2.fields == {"Value": "1uF", "MPN": "GRM188R61A105KA61D", "MPN_Confirmed": ""}
        (c3,) = result.unconfirmed
        assert c3.fields == {"Value_Confirmed": "1uF"}
        assert result.changes == []

    def test_move_of_a_variant_with_other_confirmed_copies(self):
        """A variant line with the same Value and other confirmed copies than the default
        line gives its own move (review of 2026-11-07)."""
        result = check_symbols(
            [
                CadSymbol(reference="U1", value="LM358 (X)", mpn="X", value_confirmed="LM358 (X)"),
                CadSymbol(
                    reference="U1",
                    value="LM358 (X)",
                    mpn="",
                    value_confirmed="LM358 (X)",
                    variant="b",
                ),
                CadSymbol(reference="U2", value="LM358 (Y)", mpn="Y", value_confirmed="LM358 (Y)"),
                CadSymbol(
                    reference="U2",
                    value="LM358 (Y)",
                    mpn="Y",
                    value_confirmed="LM358 (Y)",
                    variant="b",
                ),
            ]
        )
        assert [(m.reference, m.variant, m.fields["MPN_Confirmed"]) for m in result.move] == [
            ("U1", "", "X"),
            ("U2", "", "Y"),
            ("U1", "b", ""),
        ]

    def test_conflict_of_a_variant(self):
        """A variant line gives only the conflicts that the default line does not give."""
        conflict = "the units of one package have different fields MPN: A = 'X', B = 'Y'"
        result = check_symbols(
            [
                CadSymbol(reference="U1", value="LM358", mpn="X,Y", conflict=conflict),
                CadSymbol(reference="U1", value="LM358", mpn="X,Y", conflict=conflict, variant="b"),
                CadSymbol(reference="U2", value="LM358", mpn="X,Y", conflict=conflict, variant="b"),
            ]
        )
        # U1 of the variant b has the conflict of the default variant: no second message.
        assert result.conflicts == [f"U1: {conflict}", f"U2 (variant b): {conflict}"]
        assert (result.changes, result.unconfirmed, result.move) == ([], [], [])

    def test_unit_conflict_and_an_other_conflict_of_a_variant(self):
        """A unit conflict is a conflict of its own: a variant line with an other
        conflict too does not repeat the unit conflict of the default line."""
        units = "the units of one package have different fields MPN: A = 'X', B = 'Y'"
        result = check_symbols(
            [
                CadSymbol(reference="U1", value="LM358", mpn="X,Y", unit_conflicts=[units]),
                CadSymbol(
                    reference="U1",
                    value="LM358",
                    mpn="X,Y",
                    conflict="Value_Confirmed 10k, Value Confirmed 1k",
                    unit_conflicts=[units],
                    variant="b",
                ),
            ]
        )
        assert result.conflicts == [
            f"U1: {units}",
            "U1 (variant b): Value_Confirmed 10k, Value Confirmed 1k",
        ]
        assert (result.changes, result.unconfirmed, result.move) == ([], [], [])

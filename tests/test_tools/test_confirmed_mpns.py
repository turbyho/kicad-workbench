"""Tests for the model of the confirmed MPNs (parsers/confirmed_mpns.py).

The tests of split_value, field_list, format_mpns and symbol_state are the tests
of datasheet-lens (tests/test_project.py: test_split_value, test_symbol_state,
test_symbol_state_sixth_review_cases, test_review_cases_of_2026_11_05), with no
change of the cases: the two projects must give the same results.
"""

from kicad_workbench.parsers.confirmed_mpns import (
    NEW_MPN,
    NEW_VALUE,
    CadSymbol,
    ValueParts,
    _value_key,
    bom_symbols,
    check_symbols,
    field_items,
    field_list,
    format_mpns,
    split_value,
    symbol_state,
)


def state(value, mpn="", confirmed=None, **kw):
    return symbol_state(
        CadSymbol(reference="Q1", value=value, mpn=mpn, value_confirmed=confirmed, **kw)
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

    def test_field_list_and_value_key(self):
        assert field_list("A,100), B") == (["A", "100)", "B"], "")  # ")" is text here
        # The comparison: the words of the general value and the list of the MPNs.
        assert _value_key("2N7002 (A; B)") == _value_key('2N7002  ("A",B)')
        assert _value_key("10 k") != _value_key("10k")
        assert _value_key("4m7") != _value_key("4M7")
        assert _value_key("10k") != _value_key("10k (A)") != _value_key("10k (B)")
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
        assert format_mpns(["A(1)"], value=True) == '"A(1)"'
        assert format_mpns(['"A', "B;C"]) == '"""A", "B;C"'
        # The text gives the items again.
        assert format_mpns(["~"]) == '"~"' and field_list('"~"') == (["~"], "")


class TestSymbolState:
    def test_symbol_state(self):
        """The model of plans/confirmed-mpns.md (decisions 13 to 16). The confirmation
        of the user is the truth: until the user confirms a change, the confirmed
        field MPN gives the parts."""
        both = "2N7002 (2N7002LT1G, BSS138LT1G)"
        # Confirmed: the same MPNs, and Value equal to Value_Confirmed (spaces and the
        # separators of the list do not count).
        s = state(both, "2N7002LT1G; BSS138LT1G", "2N7002  (2N7002LT1G; BSS138LT1G)")
        assert (s.parts, s.change, s.unconfirmed, s.complete) == (
            ["2N7002LT1G", "BSS138LT1G"],
            "",
            False,
            "",
        )
        # Not confirmed: an empty Value_Confirmed, or an empty field MPN.
        assert state(both, "2N7002LT1G, BSS138LT1G", "").unconfirmed
        s = state(both, "", None)
        assert (s.parts, s.unconfirmed, s.write_mpn) == (
            ["2N7002LT1G", "BSS138LT1G"],
            True,
            "2N7002LT1G, BSS138LT1G",
        )
        # An other list in the field MPN: a change; the confirmed MPN stays the part.
        s = state(both, "2N7002LT1G")
        assert (s.parts, s.new, s.removed, s.write_mpn) == (
            ["2N7002LT1G"],
            ["BSS138LT1G"],
            [],
            "2N7002LT1G, BSS138LT1G",
        )
        assert s.change.endswith("the MPNs in the parentheses are not the confirmed MPNs")
        assert state(both, "BSS138LT1G, 2N7002LT1G").change  # an other order
        assert state("Q (bss138lt1g)", "BSS138LT1G").change  # the field is a copy: case
        # Value is not equal to Value_Confirmed: a change.
        s = state("220nF (GRM2)", "GRM1", "100nF (GRM1)")
        assert (s.parts, s.new, s.removed, s.write_mpn, s.select) == (
            ["GRM1"],
            ["GRM2"],
            ["GRM1"],
            "GRM2",
            False,
        )
        s = state("220nF", "GRM1", "100nF (GRM1)")  # a new general value: select the part
        assert (s.parts, s.select, s.removed) == (["GRM1"], True, ["GRM1"])
        assert state("220nF", "GRM1", "100nF").select  # a confirmed Value with no MPN in it
        assert state("10 k (A)", "A", "10k (A)").change  # the words count
        assert state("220nF", "", "100nF").change.endswith(": no MPN is confirmed")
        s = state("TestPoint2", "-", "TestPoint")
        assert "the symbol needs no part (MPN '-'): this can be wrong now" in s.change
        assert s.write_mpn == "-"
        # No parentheses and an MPN in the field: complete the fields by the convention.
        s = state("10k", "RC0603FR-0710KL", "10k")
        assert (s.parts, s.change, s.complete, s.write_mpn) == (
            ["RC0603FR-0710KL"],
            "",
            "10k (RC0603FR-0710KL)",
            "RC0603FR-0710KL",
        )
        assert state("10k", "RC0603FR-0710KL").complete == "10k (RC0603FR-0710KL)"
        assert not state("", "RC0603FR-0710KL").complete  # no Value
        assert not state("10k", "RC0603FR-0710KL, -").complete  # a conflict: not into Value
        # Value_Confirmed with parentheses and a Value with none: the user removed the
        # MPNs.
        s = state("10k", "RC0603FR-0710KL", "10k (RC0603FR-0710KL)")
        assert (s.select, s.removed, s.complete) == (True, ["RC0603FR-0710KL"], "")
        # No part selected; no part needed.
        assert state("100nF", confirmed="").no_mpn
        assert state("100nF (TBD)", "TBD", "100nF (TBD)").no_mpn
        s = state("TestPoint", "-", "TestPoint")
        assert (s.parts, s.no_mpn, s.no_part, s.conflicts, s.change) == (
            [],
            False,
            True,
            [],
            "",
        )
        assert state("TestPoint (-)", "", "").no_part  # a no-part text in Value
        # Conflicts: a Value that does not follow the convention, a no-part text and an
        # MPN in one list.
        s = state("10k (A) (B)", "A", "10k (A)")
        assert s.conflicts[0].startswith("Value '10k (A) (B)': more than one text in parentheses")
        assert not s.change and s.parts == ["A"]
        assert "a comma before three digits" in state("Q (X)", "BC847B,215, BSS138").conflicts[0]
        s = state("Q (BC847B,215, BSS138)", "", "")
        assert (s.parts, s.unconfirmed) == (["BC847B,215, BSS138"], False)
        assert "a comma before" in s.conflicts[0]
        s = state(
            'Q ("BC847B,215", BSS138)',
            '"BC847B,215", BSS138',
            other_mpns=[("MP", "BC847B,215, BSS138")],
        )
        assert "MP 'BC847B,215, BSS138': a comma before three digits" in s.conflicts[0]
        s = state("Q (X)", "X", other_values=["Q (BC847B,215, BSS138)"])
        assert s.extra == ["BC847B,215, BSS138"]  # the MPNs of an other Value column
        # Quotes: the fields are equal also with quotes that are not necessary; a
        # completion writes quotes only where they are necessary.
        assert not state('Q ("A", B)', "A, B", "Q (A, B)").change
        s = state("Q", '"BC847B,215", BSS138')
        assert (s.complete, s.write_mpn) == ('Q ("BC847B,215", BSS138)', '"BC847B,215", BSS138')
        assert state("Q", "BC847B,215").complete == "Q (BC847B,215)"
        s = state("J", "A(1)")
        assert (s.complete, s.write_mpn) == ('J ("A(1)")', "A(1)")
        assert not state('J ("A(1)")', "A(1)", 'J ("A(1)")').change
        assert state("Q (GRM1, -)", "GRM1, -").conflicts == [
            "MPN GRM1, -: - tells that the symbol needs no part",
            "Value GRM1, -: - tells that the symbol needs no part",
        ]
        # More columns: a conflict, and the MPNs of the other column are parts too.
        s = state(
            "2N7002 (2N7002LT1G)",
            "2N7002LT1G",
            "2N7002 (2N7002LT1G)",
            other_mpns=[("MP", "BSS138LT1G")],
        )
        assert (s.parts, s.extra, s.conflicts) == (
            ["2N7002LT1G", "BSS138LT1G"],
            ["BSS138LT1G"],
            ["MPN 2N7002LT1G, MP BSS138LT1G"],
        )
        s = state(
            "2N7002 (2N7002LT1G)",
            "2N7002LT1G",
            "2N7002 (2N7002LT1G)",
            other_values=["BSS138 (BSS138LT1G)"],
        )
        assert s.parts == ["2N7002LT1G", "BSS138LT1G"]
        s = state("2N7002 (2N7002LT1G)", "2N7002LT1G", other_values=["2N7002 (2N7002LT1G)"])
        assert s.extra == []

    def test_review_cases(self):
        """The cases of the reviews of datasheet-lens (2026-11-05)."""
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
        s = state("100nF", "TBD", "100nF")
        assert (s.no_part, s.no_mpn, s.parts, s.conflicts) == (False, True, [], [])
        s = state("100nF", "N/A", "100nF")
        assert (s.no_part, s.no_mpn, s.conflicts) == (True, False, [])
        s = state("2N7002 (TBD, 2N7002LT1G)", "TBD, 2N7002LT1G", "2N7002 (TBD, 2N7002LT1G)")
        assert (s.parts, s.conflicts) == (["2N7002LT1G"], [])  # TBD does not tell "no part"
        # A list of MPNs has no decimal comma; an MPN can have no digit; the text of
        # KiCad for different values is not an MPN.
        assert state("Q (2N7002,2N7000)", "2N7002,2N7000").parts == ["2N7002", "2N7000"]
        assert state("J (PJ-SMD)", "PJ-SMD").parts == ["PJ-SMD"]
        assert state("10k", "-- mixed values --").conflicts == [
            "MPN -- mixed values --: not an MPN"
        ]
        # TBD is an empty field, also in the parentheses: (TBD) has nothing to confirm.
        s = state("10k (A)", "TBD", "")
        assert (s.parts, s.unconfirmed, s.change, s.write_mpn) == (["A"], True, "", "A")
        s = state("10k (TBD)", "", "")
        assert (s.unconfirmed, s.no_mpn, s.write_mpn) == (False, True, "")
        # A field MPN that is empty now, after a confirmation: a change, not a first one.
        s = state("10k (A)", "", "10k (A)")
        assert (s.parts, s.new, s.unconfirmed) == ([], ["A"], False)
        assert s.change == (
            "Value '10k (A)', confirmed MPN '(empty)': the MPNs in the parentheses are not the "
            "confirmed MPNs"
        )
        # A symbol with a conflict gets no proposal, and no "no part selected".
        s = state("10k", "-- mixed values --")
        assert (s.no_mpn, s.complete) == (False, "")
        s = state("10k (A)", "-- mixed values --", "")
        assert (s.parts, s.change, s.unconfirmed, s.write_mpn) == (["A"], "", False, "")
        s = symbol_state(CadSymbol(value="10k", mpn="A", other_mpns=[("MP", "B")]))
        assert (s.parts, s.complete) == (["A", "B"], "")
        # A new general value: the confirmed MPN goes, also when the old Value had no
        # MPN.
        assert state("220nF", "GRM1", "100nF").removed == ["GRM1"]
        # The completion writes each MPN one time.
        s = state("10k", "A, A")
        assert (s.complete, s.write_mpn) == ("10k (A)", "A")
        # Two equal MPNs are one part, and no change.
        both = "2N7002 (2N7002LT1G, 2N7002LT1G)"
        s = state(both, "2N7002LT1G, 2N7002LT1G", both)
        assert (s.parts, s.change, s.conflicts) == (["2N7002LT1G"], "", [])
        # A change that removes the MPNs from Value names them.
        s = state("100nF", "2N7002LT1G", "2N7002 (2N7002LT1G)")
        assert (s.select, s.removed) == (True, ["2N7002LT1G"])


class TestBomSymbols:
    def test_columns(self):
        """The roles of the columns of a BOM of kicad-cli, as bom_parts of
        datasheet-lens reads them."""
        rows = [
            ["Reference", "Value", "MPN", "Manufacturer Part Number", "Value_Confirmed", "mpn"],
            ["Q1", "2N7002 (2N7002LT1G)", "2N7002LT1G", "", "2N7002 (2N7002LT1G)", ""],
            ["Q2", "BSS138", "", "BSS138LT1G", "~", ""],
            ["Q3", "BSS138", "BSS138LT1G", "BSS138", "", ""],
            ["TP1", "TestPoint", "-", "", "", ""],
            ["R1", "", "", "", "", ""],
            ["R2", "10k", "-", "RC0603", "", ""],
            ["R3", "10k", "A", "", "", "B"],
        ]
        symbols = bom_symbols(rows, "Lite")
        assert [s.reference for s in symbols] == ["Q1", "Q2", "Q3", "TP1", "R2", "R3"]
        q1, q2, q3, tp1, r2, r3 = symbols
        assert (q1.mpn, q1.value_confirmed, q1.variant, q1.conflict) == (
            "2N7002LT1G",
            "2N7002 (2N7002LT1G)",
            "Lite",
            "",
        )
        # The first filled MPN column is the field MPN; "~" is empty.
        assert (q2.mpn, q2.value_confirmed, q2.other_mpns) == ("BSS138LT1G", "", [])
        assert q3.other_mpns == [("Manufacturer Part Number", "BSS138")]
        assert symbol_state(q3).conflicts == ["MPN BSS138LT1G, Manufacturer Part Number BSS138"]
        assert (tp1.mpn, tp1.conflict) == ("-", "")
        # A column with "no part" and a column with an MPN: a conflict.
        assert (r2.mpn, r2.conflict) == ("RC0603", "Manufacturer Part Number RC0603, MPN -")
        # Two columns of one name with no regard to case: MPN and mpn.
        assert r3.other_mpns == [("mpn", "B")]

    def test_field_names_of_a_write(self):
        """A write goes into the field that holds the MPN of the line, or into the
        name that the project uses."""
        rows = [
            ["Reference", "Value", "Manufacturer Part Number", "Value Confirmed"],
            ["U1", "LM358", "LM358DR", ""],
            ["U2", "LM358 (LM358DT)", "", ""],
        ]
        result = check_symbols(bom_symbols(rows))
        (u1,) = result.complete
        assert u1.fields == {
            "Value": "LM358 (LM358DR)",
            "Manufacturer Part Number": "LM358DR",
            "Value Confirmed": "LM358 (LM358DR)",
        }
        (u2,) = result.unconfirmed
        assert u2.fields == {
            "Manufacturer Part Number": "LM358DT",
            "Value Confirmed": "LM358 (LM358DT)",
        }
        # Two filled MPN columns: the write goes into the two, in the order of MPN_FIELDS
        # (as datasheet-lens). No MPN in the line and more names: the user selects the
        # field (test_write_field_names of datasheet-lens).
        rows = [
            ["Reference", "Value", "MP", "Manufacturer Part Number"],
            ["U1", "X", "A", "A"],
            ["U2", "X (B)", "", ""],
        ]
        u1, u2 = bom_symbols(rows)
        assert (u1.mpn_fields, u1.mpn_known) == (["Manufacturer Part Number", "MP"], True)
        assert (u2.mpn_fields, u2.mpn_known) == (["Manufacturer Part Number", "MP"], False)
        assert (u1.confirmed_fields, u1.confirmed_known) == (["Value_Confirmed"], True)
        result = check_symbols([u1, u2])
        (c1,) = result.complete
        assert c1.fields == {"Value": "X (A)", "Manufacturer Part Number": "A", "MP": "A"}
        (c2,) = result.unconfirmed
        assert (c2.fields, c2.choices) == (
            {},
            [("the MPN", "B", ["Manufacturer Part Number", "MP"])],
        )
        # One name of the role (also in an other spelling): known.
        rows = [["Reference", "Value", "mpn"], ["U2", "X (B)", ""]]
        (u2,) = bom_symbols(rows)
        assert (u2.mpn_fields, u2.mpn_known) == (["mpn"], True)

    def test_write_fields_of_the_roles(self):
        """A "no part" mark, no MPN column, two names of Value_Confirmed
        (test_write_fields_of_the_roles of datasheet-lens)."""
        rows = [
            ["Reference", "Value", "MP", "Value_Confirmed", "Value Confirmed"],
            ["TP1", "TP", "-", "", ""],
            ["U1", "X (A)", "", "", ""],
        ]
        tp1, u1 = bom_symbols(rows)
        assert (tp1.mpn_fields, tp1.mpn_known) == (["MP"], True)  # the column of the mark
        assert (u1.confirmed_fields, u1.confirmed_known) == (
            ["Value_Confirmed", "Value Confirmed"],
            False,
        )
        (u1,) = bom_symbols([["Reference", "Value", "Value_Confirmed"], ["U1", "X (A)", ""]])
        assert (u1.mpn_fields, u1.mpn_known) == (["MPN"], True)  # no MPN column

    def test_write_targets_of_a_variant(self):
        """A variant line that writes into other fields than the default line gives its
        own message (test_write_targets_of_a_variant of datasheet-lens)."""
        result = check_symbols(
            [
                CadSymbol(reference="U1", value="LM358", mpn="LM358DR", value_confirmed=""),
                CadSymbol(
                    reference="U1",
                    value="LM358",
                    mpn="LM358DR",
                    value_confirmed="",
                    variant="b",
                    mpn_fields=["Manufacturer Part Number"],
                ),
            ]
        )
        assert [list(c.fields)[1] for c in result.complete] == ["MPN", "Manufacturer Part Number"]

    def test_no_column_value_confirmed(self):
        (symbol,) = bom_symbols([["Reference", "Value", "MPN"], ["R1", "10k", "A"]])
        assert symbol.value_confirmed is None
        assert symbol_state(symbol).complete == "10k (A)"

    def test_value_confirmed_columns_with_different_texts(self):
        rows = [
            ["Reference", "Value", "Value_Confirmed", "Value Confirmed"],
            ["R1", "10k", "10k", "1k"],
        ]
        (symbol,) = bom_symbols(rows)
        assert symbol.conflict == "Value_Confirmed 10k, Value Confirmed 1k"


class TestCheckSymbols:
    def test_messages(self):
        """The messages of check_parts of datasheet-lens: a change, a symbol that is not
        confirmed, the completion of the fields, a symbol with no part, the variants
        and the power symbols."""
        result = check_symbols(
            [
                CadSymbol(
                    reference="Q1",
                    value="2N7002 (2N7002LT1G, BSS138LT1G)",
                    mpn="2N7002LT1G",
                    value_confirmed="2N7002 (2N7002LT1G)",
                ),
                CadSymbol(
                    reference="U1", value="ESP32-S3 (ESP32-S3-WROOM-1-N16R8)", value_confirmed=""
                ),
                CadSymbol(
                    reference="TP1", value="TestPoint_Small", mpn="-", value_confirmed="TestPoint"
                ),
                CadSymbol(reference="#PWR01", value="+3V3", value_confirmed=""),
                CadSymbol(reference="R1", value="10k", value_confirmed=""),
                CadSymbol(
                    reference="R5", value="10k", mpn="RC0603FR-0710KL", value_confirmed="10k"
                ),
                CadSymbol(
                    reference="C5", value="220nF", mpn="GRM1", value_confirmed="100nF (GRM1)"
                ),
                CadSymbol(
                    reference="Q2",
                    value="BSS138 (BSS138LT1G)",
                    mpn="BSS138LT1G",
                    value_confirmed="BSS138 (BSS138LT1G)",
                    variant="b",
                ),
                CadSymbol(
                    reference="Q2",
                    value="BSS138 (BSS138LT1G)",
                    mpn="BSS138LT1G",
                    value_confirmed="BSS138 (BSS138LT1G)",
                ),
                CadSymbol(
                    reference="Q3",
                    value="BSS138 (BSS138LT1G)",
                    mpn="BSS138LT1G",
                    value_confirmed="BSS138 (BSS138LT1G)",
                ),
                CadSymbol(
                    reference="Q3",
                    value="2N7002 (2N7002LT1G)",
                    mpn="BSS138LT1G",
                    value_confirmed="BSS138 (BSS138LT1G)",
                    variant="b",
                ),
            ]
        )
        q1, tp1, c5, q3 = result.changes
        assert (q1.reference, q1.variant, q1.select) == ("Q1", "", False)
        assert q1.text == (
            "Value '2N7002 (2N7002LT1G, BSS138LT1G)', Value_Confirmed '2N7002 (2N7002LT1G)': "
            "the confirmed MPN '2N7002LT1G' can be wrong now. New: BSS138LT1G."
        )
        assert q1.fields == {
            "MPN": "2N7002LT1G, BSS138LT1G",
            "Value_Confirmed": "2N7002 (2N7002LT1G, BSS138LT1G)",
        }
        assert (tp1.reference, tp1.fields["MPN"]) == ("TP1", "-")
        assert c5.select and c5.fields == {"MPN": NEW_MPN, "Value_Confirmed": NEW_VALUE}
        assert c5.text.endswith("Not in Value now: GRM1.")
        assert (q3.reference, q3.variant) == ("Q3", "b")  # Q2: the same as the default
        (u1,) = result.unconfirmed
        assert u1.fields == {
            "MPN": "ESP32-S3-WROOM-1-N16R8",
            "Value_Confirmed": "ESP32-S3 (ESP32-S3-WROOM-1-N16R8)",
        }
        (r5,) = result.complete
        assert r5.fields == {
            "Value": "10k (RC0603FR-0710KL)",
            "MPN": "RC0603FR-0710KL",
            "Value_Confirmed": "10k (RC0603FR-0710KL)",
        }
        assert result.no_mpn == ["R1"]  # not #PWR01
        assert (result.lines, result.no_value_confirmed, result.conflicts) == (6, 0, [])

    def test_no_value_confirmed(self):
        """With no field Value_Confirmed, a write has no Value_Confirmed. A reference
        of more variants counts one time."""
        result = check_symbols(
            [
                CadSymbol(reference="C1", value="100nF", mpn="GRM188R71H104KA93D"),
                CadSymbol(reference="C1", value="220nF", mpn="GRM188R71H104KA93D", variant="b"),
                CadSymbol(reference="C2", value="1uF (GRM188R61A105KA61D)"),
            ]
        )
        assert result.no_value_confirmed == 2
        c1, c1_b = result.complete
        assert c1.fields == {"Value": "100nF (GRM188R71H104KA93D)", "MPN": "GRM188R71H104KA93D"}
        assert (c1_b.variant, c1_b.fields["Value"]) == ("b", "220nF (GRM188R71H104KA93D)")
        (c2,) = result.unconfirmed
        assert c2.fields == {"MPN": "GRM188R61A105KA61D"}

    def test_conflict_of_a_variant(self):
        """A variant line gives only the conflicts that the default line does not give."""
        conflict = "the units of one package have different fields MPN: A = 'X', B = 'Y'"
        result = check_symbols(
            [
                CadSymbol(reference="U1", value="LM358 (X)", mpn="X,Y", conflict=conflict),
                CadSymbol(
                    reference="U1", value="LM358 (X)", mpn="X,Y", conflict=conflict, variant="b"
                ),
                CadSymbol(
                    reference="U2", value="LM358 (X)", mpn="X,Y", conflict=conflict, variant="b"
                ),
            ]
        )
        # U1 of the variant b has the conflict of the default variant: no second message.
        assert result.conflicts == [f"U1: {conflict}", f"U2 (variant b): {conflict}"]
        assert (result.changes, result.unconfirmed, result.complete) == ([], [], [])

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
        assert (result.changes, result.unconfirmed, result.complete) == ([], [], [])

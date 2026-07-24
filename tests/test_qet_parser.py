# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import annotations

import unittest
from pathlib import Path

from freecad.QetRouting.qet import (
    ConductorKind,
    DiagnosticCode,
    QetParseError,
    parse_qet,
    parse_qet_bytes,
)
from freecad.QetRouting.qet.parser import parse_section_mm2

FIXTURES = Path(__file__).parent / "fixtures"


class CurrentProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = (FIXTURES / "current.qet").read_bytes()

    def test_current_uuid_endpoints_and_metadata(self) -> None:
        result = parse_qet_bytes(self.data, source_path="fixture.qet")

        self.assertFalse(result.has_errors, result.diagnostics)
        self.assertEqual(result.project.title, "UUID endpoint fixture")
        self.assertEqual(len(result.project.elements), 2)
        self.assertEqual(len(result.project.conductors), 1)
        self.assertEqual(len(result.project.routeable_conductors), 1)

        k1, k2 = result.project.elements
        self.assertEqual(k1.label, "K1")
        self.assertEqual(k1.manufacturer, "ACME")
        self.assertEqual(k1.article_number, "RX-2")
        self.assertEqual(k1.order_number, "ORDER-42")
        self.assertEqual(k1.internal_number, "INT-K1")
        self.assertEqual(k1.occurrence_key, ("=MACHINE", "+CAB1", "K1"))
        self.assertEqual(k1.device_type, k2.device_type)
        self.assertEqual([terminal.pin_key for terminal in k1.terminals], ["A1", "A2"])
        self.assertEqual([terminal.local_id for terminal in k1.terminals], ["0", "1"])

        conductor = result.project.conductors[0]
        self.assertIs(conductor.kind, ConductorKind.MULTILINE)
        self.assertEqual(conductor.endpoint_a.terminal_name, "A2")
        self.assertEqual(conductor.endpoint_b.terminal_name, "A1")
        self.assertEqual(conductor.section_mm2, 1.5)
        self.assertEqual(conductor.raw_section, "1.5 mm²")

    def test_single_line_is_preserved_but_not_routeable(self) -> None:
        data = self.data.replace(b'type="multi"', b'type="single"', 1)
        result = parse_qet_bytes(data)

        self.assertEqual(len(result.project.conductors), 1)
        self.assertEqual(result.project.routeable_conductors, ())
        self.assertIs(result.project.conductors[0].kind, ConductorKind.SINGLE_LINE)
        self.assertIn(
            DiagnosticCode.SINGLE_LINE_NOT_EXPANDED,
            {item.code for item in result.diagnostics},
        )

    def test_mixed_endpoint_modes_are_resolved_independently(self) -> None:
        current = (
            b'element2="{10000000-0000-4000-8000-000000000002}"\n'
            b'          terminal2="{11111111-1111-4111-8111-111111111111}"'
        )
        legacy = b'terminal2="2"'
        result = parse_qet_bytes(self.data.replace(current, legacy))

        self.assertEqual(len(result.project.routeable_conductors), 1)
        self.assertEqual(result.project.conductors[0].endpoint_b.legacy_terminal_id, "2")
        self.assertIn(
            DiagnosticCode.MIXED_ENDPOINTS,
            {item.code for item in result.diagnostics},
        )

    def test_unknown_conductor_type_is_preserved_but_not_routeable(self) -> None:
        result = parse_qet_bytes(self.data.replace(b'type="multi"', b'type="future"', 1))

        self.assertEqual(result.project.routeable_conductors, ())
        self.assertIs(result.project.conductors[0].kind, ConductorKind.UNKNOWN)
        self.assertIn(
            DiagnosticCode.UNKNOWN_CONDUCTOR_TYPE,
            {item.code for item in result.diagnostics},
        )

    def test_report_symbol_endpoint_is_blocked_until_net_continuation_exists(self) -> None:
        data = self.data.replace(b'link_type="simple"', b'link_type="next_report"', 1)

        result = parse_qet_bytes(data)

        self.assertEqual(result.project.routeable_conductors, ())
        self.assertIn(
            DiagnosticCode.UNSUPPORTED_ELEMENT_LINK_TYPE,
            {item.code for item in result.diagnostics},
        )

    def test_unknown_element_link_type_is_blocked_conservatively(self) -> None:
        data = self.data.replace(b'link_type="simple"', b'link_type="future_kind"', 1)

        result = parse_qet_bytes(data)

        self.assertEqual(result.project.routeable_conductors, ())
        self.assertIn(
            DiagnosticCode.UNSUPPORTED_ELEMENT_LINK_TYPE,
            {item.code for item in result.diagnostics},
        )

    def test_uuid_mode_does_not_fall_back_to_numeric_terminal(self) -> None:
        data = self.data.replace(
            b'terminal1="{22222222-2222-4222-8222-222222222222}"',
            b'terminal1="1"',
            1,
        )
        result = parse_qet_bytes(data)

        self.assertEqual(result.project.routeable_conductors, ())
        self.assertIn(
            DiagnosticCode.UNRESOLVED_ENDPOINT,
            {item.code for item in result.diagnostics},
        )

    def test_known_definition_does_not_infer_an_unknown_terminal_uuid(self) -> None:
        data = self.data.replace(
            b"{22222222-2222-4222-8222-222222222222}",
            b"{aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa}",
            1,
        )

        result = parse_qet_bytes(data)

        self.assertEqual(result.project.routeable_conductors, ())
        self.assertNotIn(
            DiagnosticCode.INFERRED_TERMINAL_FROM_CONDUCTOR,
            {item.code for item in result.diagnostics},
        )

    def test_duplicate_element_uuid_blocks_ambiguous_conductor(self) -> None:
        data = self.data.replace(
            b"10000000-0000-4000-8000-000000000002",
            b"10000000-0000-4000-8000-000000000001",
        )

        result = parse_qet_bytes(data)

        self.assertEqual(result.project.routeable_conductors, ())
        self.assertIn(
            DiagnosticCode.DUPLICATE_ELEMENT_UUID,
            {item.code for item in result.diagnostics},
        )

    def test_duplicate_terminal_uuid_blocks_ambiguous_conductor(self) -> None:
        data = self.data.replace(
            b"22222222-2222-4222-8222-222222222222",
            b"11111111-1111-4111-8111-111111111111",
        )

        result = parse_qet_bytes(data)

        self.assertEqual(result.project.routeable_conductors, ())
        self.assertIn(
            DiagnosticCode.DUPLICATE_TERMINAL_UUID,
            {item.code for item in result.diagnostics},
        )

    def test_external_definition_connected_pins_are_inferred_from_uuid_endpoints(self) -> None:
        without_collection = self.data[: self.data.index(b"  <collection>")] + b"</project>"

        result = parse_qet_bytes(without_collection)

        self.assertEqual(len(result.project.routeable_conductors), 1)
        self.assertEqual([len(element.terminals) for element in result.project.elements], [1, 1])
        self.assertIn(
            DiagnosticCode.INFERRED_TERMINAL_FROM_CONDUCTOR,
            {item.code for item in result.diagnostics},
        )

    def test_mutable_conductor_metadata_does_not_change_sync_key(self) -> None:
        original = parse_qet_bytes(self.data).project.conductors[0]
        changed_data = (
            self.data.replace(b'title="F1"', b'title="Renamed folio"', 1)
            .replace(b'num="W1"', b'num="W99"', 1)
            .replace(b'conductor_section="1.5 mm\xc2\xb2"', b'conductor_section="2.5"', 1)
        )
        changed = parse_qet_bytes(changed_data).project.conductors[0]

        self.assertEqual(changed.key, original.key)
        self.assertEqual(changed.number, "W99")
        self.assertEqual(changed.section_mm2, 2.5)


class LegacyProjectTests(unittest.TestCase):
    def test_numeric_endpoints_resolve_per_diagram(self) -> None:
        result = parse_qet(FIXTURES / "legacy.qet")

        self.assertFalse(result.has_errors, result.diagnostics)
        conductor = result.project.routeable_conductors[0]
        self.assertEqual(conductor.endpoint_a.element_uuid, "30000000-0000-4000-8000-000000000001")
        self.assertEqual(conductor.endpoint_b.element_uuid, "30000000-0000-4000-8000-000000000002")
        self.assertEqual(conductor.endpoint_a.terminal_name, "1")
        self.assertIn(
            DiagnosticCode.LEGACY_ENDPOINT,
            {item.code for item in result.diagnostics},
        )

    def test_duplicate_legacy_terminal_id_is_ambiguous_not_routeable(self) -> None:
        data = (FIXTURES / "legacy.qet").read_bytes().replace(
            b'<terminal id="20"',
            b'<terminal id="10"',
            1,
        )

        result = parse_qet_bytes(data)

        self.assertEqual(result.project.routeable_conductors, ())
        self.assertIn(
            DiagnosticCode.DUPLICATE_TERMINAL_ID,
            {item.code for item in result.diagnostics},
        )

    def test_legacy_terminal_ids_use_qet_integer_semantics(self) -> None:
        data = (
            (FIXTURES / "legacy.qet")
            .read_bytes()
            .replace(b'<terminal id="20"', b'<terminal id="010"', 1)
            .replace(b'terminal2="20"', b'terminal2="10"', 1)
        )

        result = parse_qet_bytes(data)

        self.assertEqual(result.project.routeable_conductors, ())
        self.assertIn(
            DiagnosticCode.DUPLICATE_TERMINAL_ID,
            {item.code for item in result.diagnostics},
        )

    def test_non_numeric_legacy_terminal_id_is_not_resolved(self) -> None:
        data = (
            (FIXTURES / "legacy.qet")
            .read_bytes()
            .replace(b'id="10"', b'id="pin"', 1)
            .replace(b'terminal1="10"', b'terminal1="pin"', 1)
        )

        result = parse_qet_bytes(data)

        self.assertEqual(result.project.routeable_conductors, ())
        self.assertIn(
            DiagnosticCode.UNRESOLVED_ENDPOINT,
            {item.code for item in result.diagnostics},
        )

    def test_same_folio_titles_keep_legacy_terminal_scopes_separate(self) -> None:
        diagram = b"""
        <diagram order="{order}" title="Repeated">
          <elements>
            <element type="embed://d.elmt" uuid="{{{uuid_a}}}">
              <terminals><terminal id="1" x="0" y="0" orientation="1"/></terminals>
            </element>
            <element type="embed://d.elmt" uuid="{{{uuid_b}}}">
              <terminals><terminal id="2" x="0" y="0" orientation="1"/></terminals>
            </element>
          </elements>
          <conductors><conductor terminal1="1" terminal2="2" type="multi"/></conductors>
        </diagram>
        """
        first = diagram.replace(b"{order}", b"1").replace(
            b"{uuid_a}", b"41000000-0000-4000-8000-000000000001"
        ).replace(b"{uuid_b}", b"41000000-0000-4000-8000-000000000002")
        second = diagram.replace(b"{order}", b"2").replace(
            b"{uuid_a}", b"42000000-0000-4000-8000-000000000001"
        ).replace(b"{uuid_b}", b"42000000-0000-4000-8000-000000000002")
        data = (
            b'<project version="0.80"><newdiagrams>'
            + first
            + second
            + b"""</newdiagrams><collection>
              <element name="d.elmt"><definition><description>
                <terminal name="1" x="0" y="0" orientation="e"/>
              </description></definition></element>
            </collection></project>"""
        )

        result = parse_qet_bytes(data)

        self.assertEqual(len(result.project.routeable_conductors), 2)
        self.assertEqual(
            result.project.conductors[0].endpoint_a.element_uuid,
            "41000000-0000-4000-8000-000000000001",
        )
        self.assertEqual(
            result.project.conductors[1].endpoint_a.element_uuid,
            "42000000-0000-4000-8000-000000000001",
        )


class SafetyAndValueTests(unittest.TestCase):
    def test_rejects_dtd_and_non_project_root(self) -> None:
        with self.assertRaises(QetParseError):
            parse_qet_bytes(b'<!DOCTYPE project [<!ENTITY x "x">]><project>&x;</project>')
        with self.assertRaises(QetParseError):
            parse_qet_bytes(b"<diagram/>")
        utf16 = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<!DOCTYPE project [<!ENTITY expanded "unsafe">]>'
            "<project title=\"&expanded;\"/>"
        ).encode("utf-16")
        with self.assertRaises(QetParseError):
            parse_qet_bytes(utf16)

    def test_null_uuid_is_treated_as_missing_identity(self) -> None:
        data = (
            (FIXTURES / "current.qet")
            .read_bytes()
            .replace(
                b"10000000-0000-4000-8000-000000000001",
                b"00000000-0000-0000-0000-000000000000",
            )
        )

        result = parse_qet_bytes(data)

        self.assertEqual(result.project.routeable_conductors, ())
        self.assertIn(
            DiagnosticCode.MISSING_ELEMENT_UUID,
            {item.code for item in result.diagnostics},
        )

    def test_rejects_size_over_limit(self) -> None:
        with self.assertRaises(QetParseError):
            parse_qet_bytes(b"<project/>", max_project_bytes=3)

    def test_rejects_missing_or_unsupported_project_version(self) -> None:
        with self.assertRaisesRegex(QetParseError, "version"):
            parse_qet_bytes(b"<project/>")
        with self.assertRaisesRegex(QetParseError, "unsupported"):
            parse_qet_bytes(b'<project version="0.200"/>')
        with self.assertRaisesRegex(QetParseError, "unsupported"):
            parse_qet_bytes(b'<project version="0.60"/>')

    def test_rejects_pathological_collection_nesting_cleanly(self) -> None:
        data = (
            b'<project version="0.100"><collection>'
            + b'<category name="x">' * 300
            + b"<element/>"
            + b"</category>" * 300
            + b"</collection></project>"
        )

        with self.assertRaisesRegex(QetParseError, "nesting"):
            parse_qet_bytes(data)

    def test_rejects_pathological_terminal_count_cleanly(self) -> None:
        data = (
            b'<project version="0.100"><collection>'
            b'<element name="huge.elmt"><definition>'
            b"<description>"
            + b"<terminal/>" * 4097
            + b"</description></definition></element></collection></project>"
        )

        with self.assertRaisesRegex(QetParseError, "terminals"):
            parse_qet_bytes(data)

    def test_section_parser_is_conservative(self) -> None:
        self.assertEqual(parse_section_mm2("1.5"), 1.5)
        self.assertEqual(parse_section_mm2("1,5 mm²"), 1.5)
        self.assertEqual(parse_section_mm2("2.5 mm^2"), 2.5)
        self.assertIsNone(parse_section_mm2("4 x 1.5"))
        self.assertIsNone(parse_section_mm2("-1"))


if __name__ == "__main__":
    unittest.main()

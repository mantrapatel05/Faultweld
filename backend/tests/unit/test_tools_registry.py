"""Contract tests for the agent's closed read-only tool registry."""

from __future__ import annotations

import unittest

from app.agent.protocol import TOOL_NAMES
from app.agent.tools.registry import ToolArgumentsError, ToolRegistry


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()

    def test_registry_contains_exactly_four_tools(self) -> None:
        self.assertEqual(
            set(self.registry.names),
            {
                "get_deployment_status",
                "get_readiness_probe",
                "get_recent_changes",
                "get_logs",
            },
        )
        self.assertEqual(set(self.registry.names), set(TOOL_NAMES))
        self.assertEqual(len(self.registry.names), 4)

    def test_no_argument_tools_accept_only_empty_objects(self) -> None:
        for name in ("get_deployment_status", "get_readiness_probe"):
            with self.subTest(name=name):
                self.registry.parse_arguments(name, {})
                with self.assertRaises(ToolArgumentsError) as raised:
                    self.registry.parse_arguments(name, {"namespace": "default"})
                self.assertEqual(raised.exception.reason, "invalid_tool_arguments")

    def test_parameterized_tools_enforce_strict_boundaries(self) -> None:
        for name, key, maximum in (
            ("get_recent_changes", "limit", 10),
            ("get_logs", "lines", 20),
        ):
            with self.subTest(name=name):
                for value in (1, maximum):
                    self.registry.parse_arguments(name, {key: value})
                invalid_values: tuple[object, ...] = (0, maximum + 1, True, "1")
                for invalid_value in invalid_values:
                    with self.subTest(value=invalid_value):
                        with self.assertRaises(ToolArgumentsError) as raised:
                            self.registry.parse_arguments(name, {key: invalid_value})
                        self.assertEqual(raised.exception.reason, "invalid_tool_arguments")
                with self.assertRaises(ToolArgumentsError):
                    self.registry.parse_arguments(name, {key: 1, "path": "etc/passwd"})

    def test_unknown_names_and_non_object_arguments_fail_closed(self) -> None:
        with self.assertRaises(ToolArgumentsError) as raised:
            self.registry.parse_arguments("scale_deployment", {})
        self.assertEqual(raised.exception.reason, "unknown_tool")
        invalid_arguments: tuple[object, ...] = (None, [], "{}", 1)
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ToolArgumentsError) as raised:
                    self.registry.parse_arguments("get_logs", arguments)
                self.assertEqual(raised.exception.reason, "invalid_tool_arguments")


if __name__ == "__main__":
    unittest.main()

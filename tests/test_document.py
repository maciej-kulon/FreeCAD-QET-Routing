# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from freecad.QetRouting.document import _is_supported_target


class PhysicalTargetCapabilityTests(unittest.TestCase):
    def test_placementless_assembly_group_is_rejected_before_bounds(self) -> None:
        joint_group = types.SimpleNamespace(
            TypeId="Assembly::JointGroup",
            PropertiesList=["Group"],
        )

        with patch(
            "freecad.QetRouting.document._target_local_bounds"
        ) as bounds:
            self.assertFalse(_is_supported_target(joint_group))

        bounds.assert_not_called()

    def test_bounds_error_on_placeable_candidate_is_not_hidden(self) -> None:
        candidate = types.SimpleNamespace(PropertiesList=["Placement"])

        with patch(
            "freecad.QetRouting.document._target_local_bounds",
            side_effect=RuntimeError("invalid placement"),
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid placement"):
                _is_supported_target(candidate)


if __name__ == "__main__":
    unittest.main()

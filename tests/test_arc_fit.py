"""Guard the DXF arc fitter against clearance-changing false arcs."""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dxf_to_aspire import fit_closed_ring_arcs


class ArcFitTest(unittest.TestCase):
    def test_long_straight_followed_by_small_fillet_stays_straight(self) -> None:
        # A long PDF line followed by densely sampled points on a tiny corner.
        # The old fitter turned x=0..4.5 into a shallow 0.028 mm bulge.
        points = [
            (0, 0), (4.501445, 0), (4.503956, 0.000031),
            (4.506466, 0.000124), (4.508971, 0.000278),
            (4.511472, 0.000493), (4.513968, 0.000770),
            (5, 1), (0, 1),
        ]
        fitted = fit_closed_ring_arcs(points, 0.0001)
        start = next((x, y, bulge) for x, y, bulge in fitted if (x, y) == (0, 0))
        self.assertEqual(start[2], 0.0)

    def test_true_circle_remains_arcs(self) -> None:
        points = [
            (math.cos(2 * math.pi * i / 64), math.sin(2 * math.pi * i / 64))
            for i in range(64)
        ]
        fitted = fit_closed_ring_arcs(points, 0.0001)
        self.assertEqual(len(fitted), 2)
        self.assertTrue(all(abs(bulge) == 1.0 for _, _, bulge in fitted))


if __name__ == "__main__":
    unittest.main()

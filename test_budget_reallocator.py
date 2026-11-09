"""Run: python test_budget_reallocator.py"""
from __future__ import annotations

import unittest

import numpy as np

import budget_reallocator as br
import llm

_CACHE: dict = {}


def engine() -> dict:
    """Fit the synthetic data once (small bootstrap) and share across tests."""
    if not _CACHE:
        br.RNG = np.random.default_rng(2026)
        spend, revenue = br.make_data()
        current = {c: float(spend[c].tail(12).mean()) for c in spend}
        fits = {c: br.fit_channel(spend[c].values, revenue[c].values, 25) for c in spend}
        checks = {c: br.trust(f) for c, f in fits.items()}
        trusted = {c: ok for c, (ok, _) in checks.items()}
        rec = br.optimise(current, fits, trusted)
        ev = br.evidence(current, rec, fits, trusted, {c: n for c, (_, n) in checks.items()})
        _CACHE.update(spend=spend, current=current, fits=fits, trusted=trusted, rec=rec, ev=ev)
    return _CACHE


class HillMaths(unittest.TestCase):
    def test_hill_and_marginal_by_hand(self):
        # vmax 10, k 5, s 1: f(5) = 10 * 5 / (5 + 5) = 5; f'(5) = 10 * 5 / (5 + 5)^2 = 0.5
        self.assertAlmostEqual(float(br.hill(5, 10, 5, 1)), 5.0)
        self.assertAlmostEqual(float(br.hill_marginal(5, 10, 5, 1)), 0.5)
        # with decay 0.5 a steady weekly spend of 2.5 adstocks to 5; marginal per weekly rupee doubles
        fit = dict(decay=0.5, params=np.array([10.0, 5.0, 1.0]))
        self.assertAlmostEqual(br.steady(2.5, fit), 5.0)
        self.assertAlmostEqual(br.marginal(2.5, fit), 1.0)

    def test_adstock_starts_at_steady_state(self):
        np.testing.assert_allclose(br.adstock(np.ones(4), 0.5), [2.0, 2.0, 2.0, 2.0])


class Optimiser(unittest.TestCase):
    def test_two_channel_exact_curves(self):
        # A is far past its knee, B has headroom; both spend 10; budget 20, bounds +-30%.
        fits = {"A": dict(decay=0.0, params=np.array([10.0, 2.0, 2.0])),
                "B": dict(decay=0.0, params=np.array([100.0, 40.0, 1.5]))}
        current = {"A": 10.0, "B": 10.0}
        rec = br.optimise(current, fits, {"A": True, "B": True})
        self.assertAlmostEqual(sum(rec.values()), 20.0, places=6)
        self.assertGreater(rec["B"], rec["A"])
        for c in rec:
            self.assertGreaterEqual(rec[c], 7.0 - 1e-6)
            self.assertLessEqual(rec[c], 13.0 + 1e-6)
        # brute force the 1-D problem: x_A in [7, 13], x_B = 20 - x_A
        xa = np.linspace(7, 13, 60001)
        best = max(br.steady(a, fits["A"]) + br.steady(20 - a, fits["B"]) for a in xa)
        got = br.steady(rec["A"], fits["A"]) + br.steady(rec["B"], fits["B"])
        before = br.steady(10, fits["A"]) + br.steady(10, fits["B"])
        self.assertGreater(got, before)
        self.assertAlmostEqual(got, best, places=4)

    def test_synthetic_budget_preserved_and_bounds_respected(self):
        e = engine()
        self.assertAlmostEqual(sum(e["rec"].values()), sum(e["current"].values()), places=6)
        for c, x in e["rec"].items():
            cur = e["current"][c]
            if not e["trusted"][c]:
                self.assertEqual(x, cur)                       # frozen
            else:
                floor = br.BRAND_FLOOR if c == br.BRAND else 1 - br.BOUND
                self.assertGreaterEqual(x, cur * floor - 1e-6)
                self.assertLessEqual(x, cur * (1 + br.BOUND) + 1e-6)
        self.assertGreater(e["ev"]["portfolio"]["lift_pct"], 0)
        self.assertLessEqual(e["ev"]["portfolio"]["conservative_lift_pct"], e["ev"]["portfolio"]["lift_pct"])


class SyntheticEngine(unittest.TestCase):
    def test_curve_shapes_recovered(self):
        ch = {c["channel"]: c for c in engine()["ev"]["channels"]}
        self.assertGreater(ch["Paid search brand"]["saturation_share"], 2.0)      # far past the knee
        self.assertLess(ch["Paid social"]["saturation_share"], 1.0)               # headroom
        self.assertGreater(ch["Paid social"]["marginal_roi"], ch["Paid search brand"]["marginal_roi"])
        self.assertGreater(ch["Paid social"]["fit_r2"], 0.9)
        self.assertGreater(ch["Paid social"]["change_pct"], 0)
        self.assertLess(ch["Paid search brand"]["change_pct"], 0)

    def test_trust_flags(self):
        ch = {c["channel"]: c for c in engine()["ev"]["channels"]}
        self.assertFalse(ch["Influencer"]["trusted"])
        self.assertIn("knee", ch["Influencer"]["trust_note"])
        self.assertFalse(ch["Display"]["trusted"])
        self.assertIn("noisy", ch["Display"]["trust_note"])
        self.assertTrue(all(ch[c]["trusted"] for c in ("Paid search brand", "Paid search generic", "Paid social", "Affiliates")))


class Narrative(unittest.TestCase):
    def test_template_renders_and_is_pinned(self):
        ev = engine()["ev"]
        note = br.template_note(ev)
        self.assertEqual(len(note.splitlines()), 4)
        self.assertTrue(note.startswith("Move ₹"))
        self.assertIn("Paid social (+", note)
        self.assertIn("Not trusted yet", note)
        self.assertEqual(llm.unpinned(note, ev), set())

    def test_unpinned_catches_invented_number(self):
        ev = engine()["ev"]
        note = br.template_note(ev) + " Expect a 93.5% lift."
        self.assertIn("93.5", llm.unpinned(note, ev))


if __name__ == "__main__":
    unittest.main(verbosity=2)

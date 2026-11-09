#!/usr/bin/env python3
"""Budget Reallocator - the mix model gives you curves; this turns them into a spend decision.

1. Synthesize 104 weeks of weekly spend and attributed incremental revenue for six
   marketing channels, each with a known Hill saturation curve and geometric adstock:
   one far past its knee, one with headroom, one near-linear, one barely responding,
   one whose spend never varied.
2. Fit: per channel, pick the adstock decay on a small grid and fit a Hill curve by
   non-linear least squares; report R² and a bootstrap 80% band on the marginal ROI at
   current spend. Flag a curve as "do not trust" when R² is low or spend never moved
   enough to locate the knee - that channel's budget is frozen.
3. Optimise: same total budget, maximise predicted revenue, each trusted channel within
   ±30% of current spend, brand never below 85%. Assemble an *evidence pack*: per channel
   current vs recommended spend, marginal ROI with band, saturation, R², trust flag;
   portfolio lift and a conservative lift from the low end of the bootstrap bands.
4. Narrate: with GEMINI_API_KEY or ANTHROPIC_API_KEY set, an LLM writes the four-line
   recommendation. Every number it writes is checked against the evidence pack; if it
   invents one, the deterministic template is used. Without a key you get the template.
   The optimiser decides where the money goes; the LLM only explains it.

Run:  python budget_reallocator.py [--bootstrap 150]
Out:  reallocation.md, curves.png
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import curve_fit, minimize

sys.path.insert(0, str(Path(__file__).parent))
import llm  # noqa: E402

RNG = np.random.default_rng(2026)
WEEKS = 104
BOUND = 0.30          # a trusted channel may move at most ±30% of its current spend
BRAND_FLOOR = 0.85    # brand search is never cut below 85% of current
MIN_R2 = 0.50         # below this the curve is noise, not a curve
MIN_SPEND_CV = 0.10   # below this the spend never varied enough to locate the knee
DECAY_GRID = np.round(np.arange(0.0, 0.71, 0.1), 1)
BRAND = "Paid search brand"

# Ground truth used only to synthesize data. Spend and revenue in ₹ lakh per week.
# vmax = ceiling, k = half-saturation point (on adstocked spend), s = shape, cv = week-to-week spend variation.
TRUE = {
    "Paid search brand":   dict(spend=8.0,  vmax=22.0,  k=3.5,   s=2.0, decay=0.15, cv=0.35),  # far past the knee
    "Paid search generic": dict(spend=18.0, vmax=60.0,  k=20.0,  s=1.5, decay=0.10, cv=0.25),  # at the knee
    "Paid social":         dict(spend=12.0, vmax=95.0,  k=45.0,  s=1.6, decay=0.35, cv=0.30),  # lots of headroom
    "Display":             dict(spend=6.0,  vmax=3.0,   k=8.0,   s=1.2, decay=0.40, cv=0.25),  # barely responds
    "Affiliates":          dict(spend=5.0,  vmax=120.0, k=150.0, s=1.0, decay=0.05, cv=0.25),  # near-linear
    "Influencer":          dict(spend=4.0,  vmax=30.0,  k=15.0,  s=1.8, decay=0.50, cv=0.03),  # spend never moved
}


# --------------------------------------------------------------------------- curves
def hill(a, vmax: float, k: float, s: float):
    a = np.asarray(a, dtype=float)
    return vmax * a**s / (k**s + a**s)


def hill_marginal(a, vmax: float, k: float, s: float):
    a = np.asarray(a, dtype=float)
    return vmax * s * k**s * a ** (s - 1) / (k**s + a**s) ** 2


def adstock(x: np.ndarray, decay: float) -> np.ndarray:
    out, carry = np.empty(len(x)), x[0] / (1 - decay)   # start at steady state, no burn-in
    for i, v in enumerate(x):
        carry = v + decay * carry
        out[i] = carry
    return out


def steady(x: float, fit: dict) -> float:
    """Weekly revenue at a constant weekly spend x, once adstock has settled."""
    return float(hill(x / (1 - fit["decay"]), *fit["params"]))


def marginal(x: float, fit: dict, params=None) -> float:
    """Extra weekly revenue per extra rupee of steady weekly spend."""
    p = fit["params"] if params is None else params
    return float(hill_marginal(x / (1 - fit["decay"]), *p) / (1 - fit["decay"]))


# --------------------------------------------------------------------------- data
def make_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    weeks = pd.date_range("2024-11-04", periods=WEEKS, freq="W-MON")
    spend, revenue = pd.DataFrame(index=weeks), pd.DataFrame(index=weeks)
    for name, t in TRUE.items():
        x = t["spend"] * np.exp(RNG.normal(0, t["cv"], WEEKS))
        if name == "Paid social":
            x[60:66] *= 2.4                      # a six-week test burst: the reason this curve is identifiable
        y = (hill(adstock(x, t["decay"]), t["vmax"], t["k"], t["s"])
             * np.exp(RNG.normal(0, 0.04, WEEKS)) + RNG.normal(0, 0.35, WEEKS))   # 4% relative + ₹0.35 L floor
        spend[name], revenue[name] = x.round(2), np.clip(y, 0, None).round(2)
    return spend, revenue


# --------------------------------------------------------------------------- fit
def fit_hill(a: np.ndarray, y: np.ndarray, p0=None) -> np.ndarray:
    lo = [0.0, 0.05 * a.min(), 0.3]
    hi = [200 * y.max(), 50 * a.max(), 5.0]
    p0 = [1.5 * y.max(), float(np.median(a)), 1.0] if p0 is None else list(p0)
    params, _ = curve_fit(hill, a, y, p0=np.clip(p0, lo, hi), bounds=(lo, hi), maxfev=20000)
    return params


def fit_channel(x: np.ndarray, y: np.ndarray, n_boot: int) -> dict:
    """Pick the adstock decay on a grid, fit the Hill curve, bootstrap the parameters."""
    best = None
    for decay in DECAY_GRID:
        a = adstock(x, decay)
        try:
            p = fit_hill(a, y)
        except RuntimeError:
            continue
        sse = float(((y - hill(a, *p)) ** 2).sum())
        if best is None or sse < best[0]:
            best = (sse, float(decay), p, a)
    sse, decay, p, a = best
    boots = []
    for _ in range(n_boot):
        idx = RNG.integers(0, len(y), len(y))
        try:
            boots.append(fit_hill(a[idx], y[idx], p0=p))
        except RuntimeError:
            pass
    return dict(decay=decay, params=p, boots=np.array(boots),
                r2=1 - sse / float(((y - y.mean()) ** 2).sum()),
                spend_cv=float(x.std() / x.mean()))


def trust(fit: dict) -> tuple[bool, str]:
    if fit["spend_cv"] < MIN_SPEND_CV:
        return False, f"spend CV {fit['spend_cv']:.2f} is below {MIN_SPEND_CV}: never varied enough to locate the knee"
    if fit["r2"] < MIN_R2:
        return False, f"fit R² {fit['r2']:.2f} is below {MIN_R2}: too noisy to see a curve"
    return True, "curve is identified"


# --------------------------------------------------------------------------- optimise
def optimise(current: dict[str, float], fits: dict[str, dict], trusted: dict[str, bool]) -> dict[str, float]:
    """Same total budget; maximise steady-state revenue; untrusted channels stay where they are."""
    free = [c for c in current if trusted[c]]
    budget = sum(current[c] for c in free)
    lo = [current[c] * (BRAND_FLOOR if c == BRAND else 1 - BOUND) for c in free]
    hi = [current[c] * (1 + BOUND) for c in free]
    res = minimize(lambda x: -sum(steady(v, fits[c]) for v, c in zip(x, free)),
                   np.array([current[c] for c in free]), method="SLSQP",
                   jac=lambda x: -np.array([marginal(v, fits[c]) for v, c in zip(x, free)]),
                   bounds=list(zip(lo, hi)),
                   constraints=[{"type": "eq", "fun": lambda x: x.sum() - budget}],
                   options={"ftol": 1e-12, "maxiter": 500})
    out = dict(current)
    out.update({c: float(v) for c, v in zip(free, res.x)})
    return out


# --------------------------------------------------------------------------- evidence
def bootstrap_lift_pct(current: dict, rec: dict, fits: dict) -> np.ndarray:
    n = min(len(f["boots"]) for f in fits.values())
    before = np.zeros(n)
    after = np.zeros(n)
    for c, f in fits.items():
        for b in range(n):
            before[b] += hill(current[c] / (1 - f["decay"]), *f["boots"][b])
            after[b] += hill(rec[c] / (1 - f["decay"]), *f["boots"][b])
    return (after - before) / before * 100


def evidence(current: dict, rec: dict, fits: dict, trusted: dict, notes: dict) -> dict:
    channels = []
    for c, f in fits.items():
        band = np.percentile([marginal(current[c], f, p) for p in f["boots"]], [10, 90])
        channels.append({
            "channel": c,
            "current_spend": round(current[c], 2),
            "recommended_spend": round(rec[c], 2),
            "change_pct": round((rec[c] / current[c] - 1) * 100, 1),
            "marginal_roi": round(marginal(current[c], f), 2),
            "marginal_roi_band_80": [round(float(band[0]), 2), round(float(band[1]), 2)],
            "saturation_share": round(current[c] / (1 - f["decay"]) / f["params"][1], 2),
            "adstock_decay": f["decay"],
            "fit_r2": round(f["r2"], 2),
            "trusted": trusted[c],
            "trust_note": notes[c],
        })
    before = sum(steady(current[c], fits[c]) for c in fits)
    after = sum(steady(rec[c], fits[c]) for c in fits)
    return {
        "units": "spend and revenue in ₹ lakh per week; bounds ±30% per channel, brand floor 85%",
        "portfolio": {
            "total_budget": round(sum(current.values()), 2),
            "moved_budget": round(sum(max(rec[c] - current[c], 0) for c in fits), 2),
            "response_before": round(before, 2),
            "response_after": round(after, 2),
            "lift_pct": round((after / before - 1) * 100, 1),
            "conservative_lift_pct": round(float(np.percentile(bootstrap_lift_pct(current, rec, fits), 10)), 1),
            "frozen_channels": [c for c in fits if not trusted[c]],
        },
        "channels": channels,
    }


# --------------------------------------------------------------------------- narrative
def template_note(ev: dict) -> str:
    ch, p = ev["channels"], ev["portfolio"]
    cuts = sorted([c for c in ch if c["change_pct"] < 0], key=lambda c: c["change_pct"])
    adds = sorted([c for c in ch if c["change_pct"] > 0], key=lambda c: -c["change_pct"])
    tag = lambda c: f"{c['channel']} ({c['change_pct']:+.1f}%)"  # noqa: E731
    roi = lambda c: f"marginal ROI {c['marginal_roi']:.2f} (band {c['marginal_roi_band_80'][0]:.2f}–{c['marginal_roi_band_80'][1]:.2f})"  # noqa: E731
    ok = [c for c in ch if c["trusted"]]
    sat, head = max(ok, key=lambda c: c["saturation_share"]), max(ok, key=lambda c: c["marginal_roi"])
    bad = [c for c in ch if not c["trusted"]]
    l1 = (f"Move ₹{p['moved_budget']:.2f} L/week out of " + ", ".join(map(tag, cuts))
          + " and into " + ", ".join(map(tag, adds)) + f"; the total stays at ₹{p['total_budget']:.2f} L/week.")
    l2 = (f"Predicted weekly incremental revenue ₹{p['response_before']:.2f} L → ₹{p['response_after']:.2f} L "
          f"({p['lift_pct']:+.1f}%); at the low end of the bootstrap bands, {p['conservative_lift_pct']:+.1f}%.")
    l3 = (f"{sat['channel']} sits {sat['saturation_share']:.2f}× past its half-saturation point ({roi(sat)}); "
          f"{head['channel']} has the most headroom ({roi(head)}).")
    l4 = ("Not trusted yet, held at current spend: " + "; ".join(f"{c['channel']} - {c['trust_note']}" for c in bad) + "."
          if bad else "Every channel's curve passed the trust checks.")
    return "\n".join([l1, l2, l3, l4])


SYSTEM = """You write a four-line budget recommendation for a CMO, from a saturation-curve reallocation.
Line 1: where the money moves from and to, with each channel's % change; state the total is unchanged.
Line 2: predicted revenue before -> after with the lift %, then the conservative lift % and what it means.
Line 3: the most saturated channel and the one with most headroom, with their marginal ROI and band.
Line 4: which channels are NOT trusted yet, why (quote the trust note), and that their budget is frozen.
Use ONLY numbers present in the evidence. Never compute new numbers or re-round. Plain English, no jargon about models."""


def narrate(ev: dict) -> tuple[str, str]:
    text = llm.complete(SYSTEM, json.dumps(ev, indent=2, ensure_ascii=False), max_tokens=400)
    if text is None:
        return template_note(ev), "template (no API key)"
    bad = llm.unpinned(text, ev)
    if bad:
        return template_note(ev), f"template (LLM invented numbers: {sorted(bad)})"
    return text, f"llm ({llm.provider()})"


# --------------------------------------------------------------------------- output
def table(ev: dict) -> list[str]:
    rows = ["| Channel | Now ₹L/wk | Recommended | Change | Marginal ROI (80% band) | Saturation | R² | Trust |",
            "|---|---:|---:|---:|---:|---:|---:|---|"]
    for c in ev["channels"]:
        lo, hi = c["marginal_roi_band_80"]
        rows.append(f"| {c['channel']} | {c['current_spend']:.2f} | {c['recommended_spend']:.2f} | {c['change_pct']:+.1f}% "
                    f"| {c['marginal_roi']:.2f} ({lo:.2f}–{hi:.2f}) | {c['saturation_share']:.2f}× | {c['fit_r2']:.2f} "
                    f"| {'ok' if c['trusted'] else 'DO NOT TRUST'} |")
    return rows


def plot(spend: pd.DataFrame, revenue: pd.DataFrame, fits: dict, ev: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 3, figsize=(13, 7.5))
    for ax, c in zip(axes.flat, fits):
        f, e = fits[c], next(x for x in ev["channels"] if x["channel"] == c)
        colour = "#4A6FA5" if e["trusted"] else "#d9534f"
        a = adstock(spend[c].values, f["decay"]) * (1 - f["decay"])        # adstock-adjusted weekly spend
        grid = np.linspace(0.01, max(a.max(), e["recommended_spend"]) * 1.15, 200)
        if len(f["boots"]):
            band = np.percentile([hill(grid / (1 - f["decay"]), *p) for p in f["boots"]], [10, 90], axis=0)
            ax.fill_between(grid, band[0], band[1], color=colour, alpha=0.12, lw=0)
        ax.scatter(a, revenue[c].values, s=14, color="#8a8f98", alpha=0.6, lw=0)
        ax.plot(grid, hill(grid / (1 - f["decay"]), *f["params"]), color=colour, lw=2,
                ls="-" if e["trusted"] else "--")
        ax.axvline(e["current_spend"], color="#444", ls=":", lw=1.2)
        ax.axvline(e["recommended_spend"], color=colour, ls="-", lw=1.4, alpha=0.9)
        ax.set_title(f"{c}{'' if e['trusted'] else '  ·  do not trust'}\n"
                     f"R² {e['fit_r2']:.2f} · marginal ROI {e['marginal_roi']:.2f} · "
                     f"{e['saturation_share']:.1f}× half-saturation", fontsize=9.5,
                     color="#222" if e["trusted"] else "#d9534f")
        ax.set_xlabel("weekly spend, adstock-adjusted (₹ lakh)", fontsize=8)
        ax.set_ylabel("incremental revenue (₹ lakh/week)", fontsize=8)
        ax.tick_params(labelsize=8)
        ax.grid(alpha=0.2)
    fig.suptitle("Fitted saturation curves · dotted = current spend · solid = recommended · shaded = bootstrap 80% band",
                 fontsize=11)
    plt.tight_layout()
    plt.savefig("curves.png", dpi=130)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap", type=int, default=150, help="bootstrap refits per channel")
    args = ap.parse_args()

    spend, revenue = make_data()
    current = {c: float(spend[c].tail(12).mean()) for c in spend}      # last-quarter run rate
    fits = {c: fit_channel(spend[c].values, revenue[c].values, args.bootstrap) for c in spend}
    checks = {c: trust(f) for c, f in fits.items()}
    trusted = {c: ok for c, (ok, _) in checks.items()}
    rec = optimise(current, fits, trusted)
    ev = evidence(current, rec, fits, trusted, {c: note for c, (_, note) in checks.items()})
    p = ev["portfolio"]

    print(f"{WEEKS} weeks · {len(fits)} channels · budget ₹{p['total_budget']:.2f} L/week · "
          f"{len(p['frozen_channels'])} frozen · LLM provider: {llm.provider()}\n")
    print("\n".join(table(ev)), "\n")
    note, src = narrate(ev)
    print(f"── reallocation  [{src}]")
    print(note, "\n")

    out = ["# Budget reallocation", "",
           f"Hill saturation curves with geometric adstock, fitted per channel on {WEEKS} weeks; SLSQP reallocation "
           f"within ±{BOUND:.0%} per channel, brand floor {BRAND_FLOOR:.0%}, untrusted curves frozen. "
           f"Note written by: {llm.provider()}; every number verified against the evidence pack.", "",
           "## Recommendation", "", note, "", f"*source: {src}*", "", *table(ev), "",
           "<details><summary>evidence</summary>", "", "```json",
           json.dumps(ev, indent=2, ensure_ascii=False), "```", "</details>", "",
           "![fitted curves](curves.png)", ""]
    Path("reallocation.md").write_text("\n".join(out), encoding="utf-8")
    plot(spend, revenue, fits, ev)
    print("wrote reallocation.md and curves.png")


if __name__ == "__main__":
    main()

# Budget Reallocator

Every mix-model readout I have sat through ends the same way. Forty minutes of response curves and adstock decays, then someone asks the only question the room came for — *so what should we spend next quarter?* — and the answer comes from whoever has the loudest slide. The model gave us six curves. The decision came from a gut feel wearing the model's clothes.

This is the last mile. It takes saturation curves (fitted here from weekly spend and attributed revenue; the same optimiser runs on curves a Bayesian MMM hands you) and turns them into a bounded spend decision: same total, every channel within ±30%, brand never below 85%. And it refuses to move money on a curve it cannot see. A channel whose spend never varied, or whose response is mostly noise, gets its budget frozen and a line in the note saying why.

```
── reallocation  [template (no API key)]
Move ₹3.77 L/week out of Affiliates (-30.0%), Paid search brand (-15.0%), Paid search generic (-5.4%) and into Paid social (+30.0%); the total stays at ₹55.70 L/week.
Predicted weekly incremental revenue ₹84.73 L → ₹88.98 L (+5.0%); at the low end of the bootstrap bands, +4.7%.
Paid search brand sits 2.54× past its half-saturation point (marginal ROI 0.68 (band 0.62–0.73)); Paid social has the most headroom (marginal ROI 2.06 (band 2.00–2.12)).
Not trusted yet, held at current spend: Display - fit R² 0.22 is below 0.5: too noisy to see a curve; Influencer - spend CV 0.03 is below 0.1: never varied enough to locate the knee.
```

That is the deterministic template. With an API key the same evidence pack goes to an LLM, which writes it the way a CMO wants to read it — and is then checked.

## How it works

1. **Data.** 104 weeks of weekly spend and attributed incremental revenue for six channels — brand search, generic search, paid social, display, affiliates, influencer — synthetic, with known Hill curves and geometric adstock. One channel is far past its knee, one has headroom, one is near-linear, one barely responds, and influencer was spent at the same ₹4 L every week for two years. Paid social got a six-week test burst in year two; that burst is the only reason its curve is identifiable above current spend.
2. **Fit + optimise.** Per channel: pick the adstock decay on a small grid, fit a Hill curve by non-linear least squares, bootstrap it 150 times for an 80% band on the marginal ROI at current spend. A curve with R² under 0.5, or spend that varied less than 10% week to week, is flagged *do not trust* and that channel is frozen. SLSQP then maximises predicted revenue at the same total within the bounds. The evidence pack holds, per channel: current and recommended spend, change %, marginal ROI with band, how far past the half-saturation point it sits, R², trust flag; for the portfolio: revenue before and after, lift, and a conservative lift from the low end of the bootstrap bands.
3. **Write.** The pack goes to an LLM with a four-line brief: from where to where, predicted and conservative lift, most saturated vs most headroom, which curves not to trust yet — **using only the numbers provided**.
4. **Verify.** Every number in the LLM's output is parsed and checked against the evidence pack. If it invented or re-rounded anything, the note is discarded and the template is used instead. The output says which path produced it.

Step 4 matters more here than in most places, because the numbers are a spend plan. A note that says "about 5%" when the optimiser said 5.0% is harmless. A note that says "move ₹4 L into social" when the bound allowed ₹3.77 L is a budget that doesn't add up, and finance will find it before you do.

## Run it

```bash
pip install -r requirements.txt
python budget_reallocator.py --bootstrap 150
```

Works with no API key — you get the template note. For an LLM-written note set one of:

```bash
export GEMINI_API_KEY=...        # preferred; uses gemini-2.5-flash via REST
export ANTHROPIC_API_KEY=...     # uses the official anthropic SDK, claude-opus-5-5
```

Outputs: `reallocation.md` (note + channel table + collapsible evidence pack) and `curves.png` (one panel per channel: fitted curve, observed weeks, current and recommended spend). Tests: `python test_budget_reallocator.py`.

## What I'd change for production

- Feed it the mix model's posterior curves instead of refitting from weekly aggregates. The fit is the demo's way of getting curves; the bounded optimiser and the trust gate are the product. The conservative lift should come from posterior draws, not a bootstrap.
- Make the bounds a conversation, not a constant. ±30% and an 85% brand floor are the numbers I have used; the real limits are contracts, minimum commitments, and what the channel team can execute in a quarter.
- Pair every frozen channel with the test that would unfreeze it. The influencer line is the honest one: the curve cannot be trusted because spend never moved, so the recommendation there is a spend test, not a reallocation.

Companion reading: [Bayesian MMM in 2026](https://nikunjkaushik.com/blog/bayesian-mmm-2026.html). Part of the [Building with AI](https://nikunjkaushik.com/projects.html) set on my site.

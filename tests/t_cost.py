"""Cost accounting: reasoning tokens, cached input, per-model pricing.

Each case here corresponds to a way the old estimate was wrong. The numbers in
the first case are the real ones production recorded for 2026-08-26.
"""
import qc_runner as q

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def close(name, got, want, tol=1e-9):
    ok = abs(got - want) < tol
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got:.6f}, want {want:.6f}")
    if not ok:
        fails.append(name)


class Usage:
    """Stand-in for the SDK's pydantic usage model."""

    def __init__(self, **kw):
        self._d = kw

    def model_dump(self):
        return dict(self._d)


print("=== reasoning tokens are now billed (they were dropped) ===")
u = q._tokens_from_usage(Usage(
    prompt_token_count=1000, candidates_token_count=200,
    thoughts_token_count=800, total_token_count=2000,
))
check("prompt", u.prompt, 1000)
check("output includes thoughts", u.output, 1000)   # 200 visible + 800 thinking
check("thoughts recorded", u.thoughts, 800)
print("   (old code reported output=200, understating this call by 80%)")

print()
print("=== cached input is discounted, not ignored ===")
u = q._tokens_from_usage(Usage(
    prompt_token_count=10000, cached_content_token_count=8000,
    candidates_token_count=500,
))
check("cached captured", u.cached, 8000)
check("cached is a subset of prompt", u.cached <= u.prompt, True)

stats = q.RunStats()
stats.record("gemini-2.5-flash", Usage(
    prompt_token_count=10000, cached_content_token_count=8000,
    candidates_token_count=500,
))
p_in, p_out = q._price_for("gemini-2.5-flash")
want = (2000 * p_in + 8000 * p_in * q.CACHED_INPUT_DISCOUNT + 500 * p_out) / 1e6
close("cached priced at a discount", stats.cost_usd(), round(want, 6), 1e-6)

print()
print("=== a total-only response still yields output, without double-counting ===")
u = q._tokens_from_usage(Usage(prompt_token_count=1000, total_token_count=1600))
check("output derived from total", u.output, 600)

print()
print("=== per-model pricing on a cascade ===")
stats = q.RunStats()
stats.record("gemini-2.5-pro", Usage(prompt_token_count=1000,
                                     candidates_token_count=1000))
stats.record("gemini-2.5-flash", Usage(prompt_token_count=1000,
                                       candidates_token_count=1000))
pro_in, pro_out = q._price_for("gemini-2.5-pro")
fl_in, fl_out = q._price_for("gemini-2.5-flash")
want = (1000 * pro_in + 1000 * pro_out + 1000 * fl_in + 1000 * fl_out) / 1e6
close("each model priced at its own rate", stats.cost_usd(), round(want, 6), 1e-6)
print("   (old code charged all 4000 tokens at one model's rate)")

print()
print("=== unpriced models are flagged, not silently guessed ===")
check("known model not flagged", q._has_price("gemini-2.5-flash"), True)
check("gemini-3.7-flash unknown", q._has_price("gemini-3.7-flash"), False)
s1 = q.RunStats()
s1.record("gemini-2.5-flash", Usage(prompt_token_count=10,
                                    candidates_token_count=10))
check("not estimated for known model", s1.cost_is_estimated(), False)
s2 = q.RunStats()
s2.record("gemini-3.7-flash", Usage(prompt_token_count=10,
                                   candidates_token_count=10))
check("estimated for unknown model", s2.cost_is_estimated(), True)

print()
print("=== production's real 08-26 run, recomputed ===")
# What the run actually recorded. With no thinking or cache reported, the
# figure must be unchanged — the fix must not move existing numbers.
stats = q.RunStats()
stats.record("gemini-2.5-flash", Usage(
    prompt_token_count=31625, candidates_token_count=4571,
))
close("matches the stored cost", stats.cost_usd(), 0.020915, 1e-6)
check("not flagged as extra-estimated", stats.cost_is_estimated(), False)

print()
print("=== degenerate inputs ===")
check("None usage", q._tokens_from_usage(None), q.TokenUsage())
check("empty usage", q._tokens_from_usage(Usage()), q.TokenUsage())
check("empty stats cost", q.RunStats().cost_usd(), 0.0)
check("empty stats not flagged", q.RunStats().cost_is_estimated(), False)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL COST ASSERTIONS PASSED")

import pytest

from examples.reconcile_fill_divergence import FirstFillDivergenceReconciler


@pytest.mark.parametrize("field, value", [
    ("time", 2), ("intent_id", "b"), ("price", "100.01"), ("fee", "5.01"),
])
def test_reconciler_preserves_material_differences(field, value):
    fill = dict(symbol="600519.SH", side="BUY", quantity=100, price="100.00",
                fee="5.00", time=1, intent_id="a")
    report = FirstFillDivergenceReconciler("a", [fill], "b", [dict(fill, **{field: value})]).find_first_divergence()
    assert report["divergence_found"]

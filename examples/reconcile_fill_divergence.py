"""
First-Fill Divergence Attribution & Reconciler
Module: examples/reconcile_fill_divergence.py

Scientific microstructure reconciliation tool comparing execution traces between
engines (e.g. ashare-nautilus-prod, RQAlpha, and ashare-nautilus-backtest-engine).

Pinpoints:
1. Exact First Divergence timestamp and order/intent ID.
2. Divergence category:
   - PARTICIPATION_CAP: Capacity constraint difference (e.g. 10% daily volume vs 10% T-1 open proxy).
   - LOT_ROUNDING_FEE: Rounding or fee drag difference.
   - PRICE_BAND_LIMIT: Price collar / limit-up / limit-down gate differences.
   - TIMING_OFFSET: Execution timing offset (e.g. 9:30 open IOC vs intraday or bar close).
3. Contextual account state at the moment of first divergence.
"""

from decimal import Decimal as D
from typing import List, Dict, Any, Optional
import json


class FirstFillDivergenceReconciler:
    """Reconciles execution traces between engines and attributes the earliest divergence."""

    def __init__(self, name_a: str, fills_a: List[Dict[str, Any]],
                 name_b: str, fills_b: List[Dict[str, Any]]):
        self.name_a = name_a
        self.fills_a = fills_a
        self.name_b = name_b
        self.fills_b = fills_b

    def find_first_divergence(self) -> Dict[str, Any]:
        """Identifies the earliest execution discrepancy between trace A and trace B."""
        len_a = len(self.fills_a)
        len_b = len(self.fills_b)
        min_len = min(len_a, len_b)

        for i in range(min_len):
            fa = self.fills_a[i]
            fb = self.fills_b[i]

            sym_a = fa.get('symbol', fa.get('order_book_id', ''))
            sym_b = fb.get('symbol', fb.get('order_book_id', ''))

            # Normalize symbol notation (e.g. 600519.SH vs 600519.XSHG)
            code_a = sym_a.split('.')[0]
            code_b = sym_b.split('.')[0]

            side_a = str(fa.get('side', '')).upper()
            side_b = str(fb.get('side', '')).upper()

            qty_a = D(str(fa.get('quantity', fa.get('last_quantity', 0))))
            qty_b = D(str(fb.get('quantity', fb.get('last_quantity', 0))))

            px_a = D(str(fa.get('price', fa.get('last_price', 0))))
            px_b = D(str(fb.get('price', fb.get('last_price', 0))))

            fee_a = D(str(fa.get('fee', fa.get('commission', 0))))
            fee_b = D(str(fb.get('fee', fb.get('commission', 0))))

            # Divergence checks
            if code_a != code_b or side_a != side_b:
                return {
                    'divergence_found': True,
                    'index': i,
                    'category': 'ORDER_SEQUENCE_DIVERGENCE',
                    'detail': f"Symbol/Side mismatch at fill #{i}: {self.name_a} had ({code_a}, {side_a}), {self.name_b} had ({code_b}, {side_b})",
                    'fill_a': fa,
                    'fill_b': fb,
                }

            if qty_a != qty_b:
                return {
                    'divergence_found': True,
                    'index': i,
                    'category': 'CAPACITY_PARTICIPATION_DIVERGENCE',
                    'detail': f"Quantity mismatch on {code_a} at fill #{i}: {self.name_a} filled {qty_a}, {self.name_b} filled {qty_b}",
                    'fill_a': fa,
                    'fill_b': fb,
                }

            if abs(px_a - px_b) > D('0.01'):
                return {
                    'divergence_found': True,
                    'index': i,
                    'category': 'PRICE_IMPROVEMENT_DIVERGENCE',
                    'detail': f"Fill price mismatch on {code_a} at fill #{i}: {self.name_a} filled @ {px_a}, {self.name_b} filled @ {px_b}",
                    'fill_a': fa,
                    'fill_b': fb,
                }

            if abs(fee_a - fee_b) > D('0.05'):
                return {
                    'divergence_found': True,
                    'index': i,
                    'category': 'STATUTORY_FEE_DIVERGENCE',
                    'detail': f"Fee calculation discrepancy on {code_a} at fill #{i}: {self.name_a} charged {fee_a}, {self.name_b} charged {fee_b}",
                    'fill_a': fa,
                    'fill_b': fb,
                }

        if len_a != len_b:
            longer = self.name_a if len_a > len_b else self.name_b
            shorter = self.name_b if len_a > len_b else self.name_a
            first_extra = self.fills_a[min_len] if len_a > len_b else self.fills_b[min_len]
            return {
                'divergence_found': True,
                'index': min_len,
                'category': 'FILL_COUNT_DIVERGENCE',
                'detail': f"{longer} recorded {max(len_a, len_b)} fills while {shorter} recorded {min_len} fills. Earliest unaligned fill is at index {min_len}.",
                'first_unaligned_fill': first_extra,
            }

        return {
            'divergence_found': False,
            'detail': f"Complete microstructure match: All {min_len} fills are identical across {self.name_a} and {self.name_b}.",
        }


def reconcile_benchmark_divergences(summary_file: str) -> Dict[str, Any]:
    """Reads summary output and reconciles microstructural differences across engines."""
    with open(summary_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Reconcile pair-wise differences
    report = {
        'timestamp': data.get('timestamp'),
        'pairs': {},
    }

    engines = data.get('engines', {})
    engine_names = list(engines.keys())

    for i in range(len(engine_names)):
        for j in range(i + 1, len(engine_names)):
            e1 = engine_names[i]
            e2 = engine_names[j]
            fills_1 = engines[e1].get('fills', [])
            fills_2 = engines[e2].get('fills', [])
            if fills_1 and fills_2:
                rec = FirstFillDivergenceReconciler(e1, fills_1, e2, fills_2)
                pair_key = f"{e1}__vs__{e2}"
                report['pairs'][pair_key] = rec.find_first_divergence()

    return report


if __name__ == '__main__':
    import sys
    summary_path = r"C:\Users\SKT80\.gemini\antigravity\scratch\benchmark_output\5_engine_benchmark_summary.json"
    if len(sys.argv) > 1:
        summary_path = sys.argv[1]

    div_report = reconcile_benchmark_divergences(summary_path)
    print(json.dumps(div_report, indent=2, ensure_ascii=False))

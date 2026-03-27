#!/usr/bin/env python3
"""triangle_inequality_sfu.py

Minimal CLI for triangle-inequality prevalence on an RTT matrix.

Modes (exactly one required):
    --all-sfus
        Evaluate every node as an SFU relay location, then print a compact summary
        across SFU placements plus the benchmark-chosen “central” SFU.

    --exists-relay
        For each node pair (a,b), find the best intermediary r != a,b (lowest-latency
        one-hop detour) and report how often a detour exists that improves latency.
        Defaults are set to a paper-like “significant TIV” filter.

Input:
    --mean-matrix PATH
        Defaults to the PlanetLab matrix shipped with this repo.

Model:
    one_way(a->b) = RTT(a,b) / 2

This script uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import signal
import statistics
import sys
from typing import List, Optional, Tuple


DEFAULT_MEAN_MATRIX = "datasets/netlatency_processed/PlanetLab/avg_PlanetLab_rtt_ms.txt"

# Hardcoded defaults to keep the CLI minimal.
ALL_SFUS_THRESHOLD_MS = 5.0

# Paper-like “significant TIV” filter for exists-relay.
EXISTS_RELAY_THRESHOLD_MS = 0.0
EXISTS_RELAY_MIN_IMPROVE_MS = 10.0
EXISTS_RELAY_MIN_IMPROVE_PCT = 10.0


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


def _read_mean_matrix_file(path: str) -> Tuple[int, List[float]]:
    n_header: Optional[int] = None
    rows: List[List[float]] = []

    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                kv = line.lstrip("#").strip()
                if kv.startswith("n="):
                    try:
                        n_header = int(kv.split("=", 1)[1])
                    except Exception:
                        pass
                continue

            parts = line.split()
            try:
                row = [float(x) for x in parts]
            except Exception as e:
                raise ValueError(f"Failed parsing matrix row in {path}: {e}")
            rows.append(row)

    if not rows:
        raise ValueError(f"No matrix rows found in {path}")

    n = len(rows)
    if n_header is not None and n_header != n:
        raise ValueError(f"Header n={n_header} but found {n} rows in {path}")

    for r in rows:
        if len(r) != n:
            raise ValueError(f"Non-square matrix in {path}: expected {n} cols, got {len(r)}")

    flat: List[float] = []
    for r in rows:
        flat.extend(r)

    return n, flat


def _row_values(avg: List[float], n: int, i: int) -> List[float]:
    base = i * n
    out: List[float] = []
    for j in range(n):
        if i == j:
            continue
        out.append(float(avg[base + j]))
    return out


def _pick_sfu_central_node(avg: List[float], n: int) -> int:
    """Centrality heuristic: node with smallest median RTT to others."""
    best_node = 0
    best_med = float("inf")
    for i in range(n):
        vals = _row_values(avg, n, i)
        if not vals:
            continue
        m = float(statistics.median(vals))
        if m < best_med:
            best_med = m
            best_node = i
    return best_node


def _rtt_ms(avg_flat: List[float], n: int, src: int, dst: int) -> float:
    return float(avg_flat[src * n + dst])


def _is_valid_ms(v: float) -> bool:
    return math.isfinite(v) and v >= 0.0


@dataclasses.dataclass(frozen=True)
class Stats:
    n: int
    sfu_node: int
    ordered_pairs: bool
    threshold_ms: float
    pairs_total: int
    pairs_classified: int
    pairs_skipped: int
    via_sfu_faster: int
    roughly_equal: int
    p2p_faster: int
    mean_delta_ms_overall: float
    mean_abs_delta_ms_overall: float
    mean_delta_via_sfu_faster: float
    mean_delta_roughly_equal: float
    mean_delta_p2p_faster: float


@dataclasses.dataclass(frozen=True)
class BestRelayStats:
    n: int
    ordered_pairs: bool
    threshold_ms: float
    pairs_total: int
    pairs_any_improving_relay: int
    pairs_classified: int
    pairs_skipped: int
    pairs_filtered_out: int
    via_relay_faster: int
    roughly_equal: int
    direct_faster: int
    mean_delta_ms_overall: float
    mean_abs_delta_ms_overall: float
    mean_delta_via_relay_faster: float
    mean_delta_roughly_equal: float
    mean_delta_direct_faster: float


def compute_stats_best_relay(
    *,
    n: int,
    avg_flat: List[float],
    threshold_ms: float,
    min_improve_ms: float,
    min_improve_pct: float,
    ordered: bool,
    progress: bool,
    assume_all_values_valid: bool,
) -> BestRelayStats:
    """For each (a,b), find best intermediary r != a,b, then classify best-via vs direct."""
    if n <= 2:
        raise ValueError(f"Need at least 3 nodes to analyze (n={n})")
    if threshold_ms < 0:
        raise ValueError("--threshold-ms must be >= 0")
    if min_improve_ms < 0:
        raise ValueError("--min-improve-ms must be >= 0")
    if min_improve_pct < 0:
        raise ValueError("--min-improve-pct must be >= 0")

    via_faster = 0
    roughly_equal = 0
    direct_faster = 0
    skipped = 0
    filtered_out = 0

    sum_delta_ms = 0.0
    sum_abs_delta_ms = 0.0
    sum_delta_via_faster = 0.0
    sum_delta_roughly_equal = 0.0
    sum_delta_direct_faster = 0.0

    if ordered:
        total_pairs = n * (n - 1)
    else:
        total_pairs = n * (n - 1) // 2

    processed_pairs_total = 0
    processed_pairs_classified = 0

    any_improving_relay = 0

    # Precompute one-way values (RTT/2) once to keep the hot loop tight.
    half_flat = [float(v) / 2.0 for v in avg_flat]

    if ordered:
        outer = range(n)
        outer_len = n
    else:
        outer = range(n - 1)
        outer_len = n - 1

    for idx_i, i in enumerate(outer, start=1):
        if ordered:
            js = (j for j in range(n) if j != i)
        else:
            js = range(i + 1, n)

        i_base = i * n
        half_i_base = i_base

        for j in js:
            rtt_direct = float(avg_flat[i_base + j])
            if not assume_all_values_valid:
                if not _is_valid_ms(rtt_direct):
                    skipped += 1
                    continue

            direct_oneway = float(half_flat[half_i_base + j])

            best_via = float("inf")
            # Search for the best relay r != i,j
            for r in range(n):
                if r == i or r == j:
                    continue
                if not assume_all_values_valid:
                    rtt_i_r = float(avg_flat[half_i_base + r])
                    rtt_r_j = float(avg_flat[r * n + j])
                    if not (_is_valid_ms(rtt_i_r) and _is_valid_ms(rtt_r_j)):
                        continue
                via = float(half_flat[half_i_base + r] + half_flat[r * n + j])
                if via < best_via:
                    best_via = via

            if not math.isfinite(best_via):
                skipped += 1
                continue

            processed_pairs_total += 1
            delta_any = best_via - direct_oneway
            if delta_any < 0.0:
                any_improving_relay += 1

            if min_improve_ms > 0.0 or min_improve_pct > 0.0:
                improve = direct_oneway - best_via
                if improve < float(min_improve_ms):
                    filtered_out += 1
                    continue
                if direct_oneway > 0.0:
                    if improve < (direct_oneway * (float(min_improve_pct) / 100.0)):
                        filtered_out += 1
                        continue

            delta = best_via - direct_oneway
            sum_delta_ms += float(delta)
            sum_abs_delta_ms += float(abs(delta))

            if delta < -threshold_ms:
                via_faster += 1
                sum_delta_via_faster += float(delta)
            elif delta > threshold_ms:
                direct_faster += 1
                sum_delta_direct_faster += float(delta)
            else:
                roughly_equal += 1
                sum_delta_roughly_equal += float(delta)

            processed_pairs_classified += 1

        if progress and (idx_i == 1 or idx_i % 50 == 0 or idx_i == outer_len):
            _eprint(
                f"exists-relay progress: src {idx_i}/{outer_len} "
                f"(pairs {processed_pairs_total}/{total_pairs})"
            )

    classified = via_faster + roughly_equal + direct_faster
    if classified != processed_pairs_classified:
        raise RuntimeError("Internal counting error")

    denom = float(classified) if classified else 1.0

    mean_delta = (sum_delta_ms / denom) if classified else float("nan")
    mean_abs_delta = (sum_abs_delta_ms / denom) if classified else float("nan")

    mean_delta_via = (sum_delta_via_faster / float(via_faster)) if via_faster else float("nan")
    mean_delta_eq = (sum_delta_roughly_equal / float(roughly_equal)) if roughly_equal else float("nan")
    mean_delta_dir = (sum_delta_direct_faster / float(direct_faster)) if direct_faster else float("nan")

    return BestRelayStats(
        n=int(n),
        ordered_pairs=bool(ordered),
        threshold_ms=float(threshold_ms),
        pairs_total=int(total_pairs),
        pairs_any_improving_relay=int(any_improving_relay),
        pairs_classified=int(classified),
        pairs_skipped=int(skipped),
        pairs_filtered_out=int(filtered_out),
        via_relay_faster=int(via_faster),
        roughly_equal=int(roughly_equal),
        direct_faster=int(direct_faster),
        mean_delta_ms_overall=float(mean_delta),
        mean_abs_delta_ms_overall=float(mean_abs_delta),
        mean_delta_via_relay_faster=float(mean_delta_via),
        mean_delta_roughly_equal=float(mean_delta_eq),
        mean_delta_direct_faster=float(mean_delta_dir),
    )


def _all_values_valid(avg_flat: List[float]) -> bool:
    # avg_flat is only ~240k values for n=490; scanning once is cheap and lets us
    # skip expensive per-pair validity checks.
    for v in avg_flat:
        if not _is_valid_ms(float(v)):
            return False
    return True


def compute_stats_for_sfu(
    *,
    n: int,
    avg_flat: List[float],
    sfu_node: int,
    threshold_ms: float,
    ordered: bool,
    progress: bool,
    assume_all_values_valid: bool,
) -> Stats:
    if n <= 2:
        raise ValueError(f"Need at least 3 nodes to analyze (n={n})")
    if not (0 <= sfu_node < n):
        raise ValueError(f"Invalid --sfu-node {sfu_node} for n={n}")
    if threshold_ms < 0:
        raise ValueError("--threshold-ms must be >= 0")

    via_faster = 0
    roughly_equal = 0
    p2p_faster = 0
    skipped = 0

    sum_delta_ms = 0.0
    sum_abs_delta_ms = 0.0

    sum_delta_via_faster = 0.0
    sum_delta_roughly_equal = 0.0
    sum_delta_p2p_faster = 0.0

    if ordered:
        total_pairs = (n - 1) * (n - 2)
    else:
        total_pairs = (n - 1) * (n - 2) // 2

    processed_pairs = 0

    nodes = [i for i in range(n) if i != sfu_node]
    nodes_total = len(nodes)

    # Cache the SFU row once (RTT(sfu, j))
    sfu_row = avg_flat[sfu_node * n : (sfu_node + 1) * n]

    for idx_i, i in enumerate(nodes, start=1):
        rtt_i_sfu = float(avg_flat[i * n + sfu_node])
        if ordered:
            js = (j for j in range(n) if j != sfu_node and j != i)
        else:
            js = (j for j in range(i + 1, n) if j != sfu_node)

        for j in js:
            rtt_direct = _rtt_ms(avg_flat, n, i, j)
            rtt_sfu_j = float(sfu_row[j])

            if not assume_all_values_valid:
                if not (_is_valid_ms(rtt_direct) and _is_valid_ms(rtt_i_sfu) and _is_valid_ms(rtt_sfu_j)):
                    skipped += 1
                    continue

            direct_oneway = rtt_direct / 2.0
            via_oneway = (rtt_i_sfu + rtt_sfu_j) / 2.0
            delta = via_oneway - direct_oneway

            sum_delta_ms += float(delta)
            sum_abs_delta_ms += float(abs(delta))

            if delta < -threshold_ms:
                via_faster += 1
                sum_delta_via_faster += float(delta)
            elif delta > threshold_ms:
                p2p_faster += 1
                sum_delta_p2p_faster += float(delta)
            else:
                roughly_equal += 1
                sum_delta_roughly_equal += float(delta)

            processed_pairs += 1

        if progress:
            _eprint(
                f"Processed node {i} ({idx_i}/{nodes_total}); "
                f"pairs {processed_pairs}/{total_pairs}"
            )

    classified = via_faster + roughly_equal + p2p_faster
    if classified != processed_pairs:
        raise RuntimeError("Internal counting error")

    denom = float(classified) if classified else 1.0

    mean_delta = (sum_delta_ms / denom) if classified else float("nan")
    mean_abs_delta = (sum_abs_delta_ms / denom) if classified else float("nan")

    mean_delta_via_faster = (sum_delta_via_faster / float(via_faster)) if via_faster else float("nan")
    mean_delta_roughly_equal = (
        (sum_delta_roughly_equal / float(roughly_equal)) if roughly_equal else float("nan")
    )
    mean_delta_p2p_faster = (sum_delta_p2p_faster / float(p2p_faster)) if p2p_faster else float("nan")

    return Stats(
        n=n,
        sfu_node=int(sfu_node),
        ordered_pairs=bool(ordered),
        threshold_ms=float(threshold_ms),
        pairs_total=int(total_pairs),
        pairs_classified=int(classified),
        pairs_skipped=int(skipped),
        via_sfu_faster=int(via_faster),
        roughly_equal=int(roughly_equal),
        p2p_faster=int(p2p_faster),
        mean_delta_ms_overall=float(mean_delta),
        mean_abs_delta_ms_overall=float(mean_abs_delta),
        mean_delta_via_sfu_faster=float(mean_delta_via_faster),
        mean_delta_roughly_equal=float(mean_delta_roughly_equal),
        mean_delta_p2p_faster=float(mean_delta_p2p_faster),
    )


def _print_human(stats: Stats) -> None:
    denom = float(stats.pairs_classified) if stats.pairs_classified else 1.0
    print("SFU Triangle Inequality Stats")
    print(f"n={stats.n} sfu_node={stats.sfu_node} ordered_pairs={int(bool(stats.ordered_pairs))}")
    print(f"threshold_ms={stats.threshold_ms:.3f} (one-way model: rtt/2 per leg)")
    print(
        f"pairs_total={stats.pairs_total} pairs_classified={stats.pairs_classified} "
        f"pairs_skipped={stats.pairs_skipped}"
    )
    print(
        "via_sfu_faster="
        f"{stats.via_sfu_faster} ({(stats.via_sfu_faster/denom)*100.0:.2f}%)"
    )
    print(
        "roughly_equal="
        f"{stats.roughly_equal} ({(stats.roughly_equal/denom)*100.0:.2f}%)"
    )
    print(
        "p2p_faster="
        f"{stats.p2p_faster} ({(stats.p2p_faster/denom)*100.0:.2f}%)"
    )
    print("mean_delta_ms_by_bucket (via_sfu - direct):")
    print(f"  via_sfu_faster={stats.mean_delta_via_sfu_faster:.3f}")
    print(f"  roughly_equal={stats.mean_delta_roughly_equal:.3f}")
    print(f"  p2p_faster={stats.mean_delta_p2p_faster:.3f}")
    print(f"mean_delta_ms_overall={stats.mean_delta_ms_overall:.3f}")
    print(f"mean_abs_delta_ms_overall={stats.mean_abs_delta_ms_overall:.3f}")


def _print_best_relay_human(stats: BestRelayStats) -> None:
    denom_total = float(stats.pairs_total) if stats.pairs_total else 1.0
    denom_classified = float(stats.pairs_classified) if stats.pairs_classified else 1.0
    print("Triangle Inequality Stats (exists-relay / best intermediary)")
    print(f"n={stats.n} ordered_pairs={int(bool(stats.ordered_pairs))}")
    print(f"threshold_ms={stats.threshold_ms:.3f} (one-way model: rtt/2 per leg)")
    print(
        f"pairs_total={stats.pairs_total} pairs_classified={stats.pairs_classified} "
        f"pairs_skipped={stats.pairs_skipped} pairs_filtered_out={stats.pairs_filtered_out}"
    )
    print(
        "paper_style_pairs_with_any_improving_relay="
        f"{stats.pairs_any_improving_relay} ({(stats.pairs_any_improving_relay/denom_total)*100.0:.2f}%)"
    )
    print(
        "significant_tiv_pairs="
        f"{stats.pairs_classified} ({(stats.pairs_classified/denom_total)*100.0:.2f}%)"
    )
    print(
        "significant_bucket_split="
        f"via_relay_faster={stats.via_relay_faster} ({(stats.via_relay_faster/denom_classified)*100.0:.2f}%)"
        f" | equal={stats.roughly_equal} ({(stats.roughly_equal/denom_classified)*100.0:.2f}%)"
        f" | direct_faster={stats.direct_faster} ({(stats.direct_faster/denom_classified)*100.0:.2f}%)"
    )
    print("mean_delta_ms_by_bucket (best_via_relay - direct):")
    print(f"  via_relay_faster={stats.mean_delta_via_relay_faster:.3f}")
    print(f"  roughly_equal={stats.mean_delta_roughly_equal:.3f}")
    print(f"  direct_faster={stats.mean_delta_direct_faster:.3f}")
    print(f"mean_delta_ms_overall={stats.mean_delta_ms_overall:.3f}")
    print(f"mean_abs_delta_ms_overall={stats.mean_abs_delta_ms_overall:.3f}")


def _pct(part: int, whole: int) -> float:
    if whole <= 0:
        return float("nan")
    return (float(part) / float(whole)) * 100.0


def _format_sfu_one_liner(stats: Stats) -> str:
    denom = int(stats.pairs_classified)
    vf = _pct(stats.via_sfu_faster, denom)
    eq = _pct(stats.roughly_equal, denom)
    p2p = _pct(stats.p2p_faster, denom)
    return (
        f"SFU {stats.sfu_node}: "
        f"via_faster {stats.via_sfu_faster} ({vf:.2f}%) | "
        f"equal {stats.roughly_equal} ({eq:.2f}%) | "
        f"p2p_faster {stats.p2p_faster} ({p2p:.2f}%) | "
        f"mean_delta {stats.mean_delta_ms_overall:.3f}ms | "
        f"mean_abs_delta {stats.mean_abs_delta_ms_overall:.3f}ms"
    )


def _percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return float("nan")
    if pct <= 0:
        return float(sorted_vals[0])
    if pct >= 100:
        return float(sorted_vals[-1])
    k = int(round((len(sorted_vals) - 1) * (pct / 100.0)))
    k = max(0, min(len(sorted_vals) - 1, k))
    return float(sorted_vals[k])


def _percentile_rank(values: List[float], v: float, *, higher_is_better: bool) -> float:
    """Return percentile rank in [0,100].

    If higher_is_better=True: 100 means v is the maximum (best).
    If higher_is_better=False: 100 means v is the minimum (best).
    """
    if not values:
        return float("nan")
    vals = sorted(float(x) for x in values)
    v = float(v)

    # Fraction of values <= v
    leq = 0
    for x in vals:
        if x <= v:
            leq += 1
        else:
            break
    frac = leq / float(len(vals))

    if higher_is_better:
        return frac * 100.0
    return (1.0 - frac) * 100.0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Triangle inequality prevalence on an RTT matrix (minimal CLI)."
        )
    )

    p.add_argument(
        "--mean-matrix",
        default=DEFAULT_MEAN_MATRIX,
        help=f"Path to avg_<Dataset>_rtt_ms.txt (RTT in ms). Default: {DEFAULT_MEAN_MATRIX}",
    )

    p.add_argument(
        "--all-sfus",
        action="store_true",
        help="Evaluate every node as the SFU and print a compact summary (recommended).",
    )

    p.add_argument(
        "--exists-relay",
        action="store_true",
        help="For each node pair, find the best relay and report significant TIV prevalence.",
    )

    return p


def main(argv: Optional[List[str]] = None) -> int:
    # When piping to tools like `head`, the reader may close early.
    # On Linux, defaulting SIGPIPE avoids noisy BrokenPipeError tracebacks.
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except Exception:
        pass

    args = build_parser().parse_args(argv)

    if not bool(args.all_sfus) and not bool(args.exists_relay):
        raise SystemExit("Please specify exactly one mode: --all-sfus or --exists-relay")
    if bool(args.all_sfus) and bool(args.exists_relay):
        raise SystemExit("Please specify only one mode: --all-sfus or --exists-relay")

    n, avg_flat = _read_mean_matrix_file(str(args.mean_matrix))

    assume_valid = _all_values_valid(avg_flat)

    if bool(args.exists_relay):
        try:
            stats_best = compute_stats_best_relay(
                n=n,
                avg_flat=avg_flat,
                threshold_ms=float(EXISTS_RELAY_THRESHOLD_MS),
                min_improve_ms=float(EXISTS_RELAY_MIN_IMPROVE_MS),
                min_improve_pct=float(EXISTS_RELAY_MIN_IMPROVE_PCT),
                ordered=False,
                progress=True,
                assume_all_values_valid=bool(assume_valid),
            )
            _print_best_relay_human(stats_best)
            print(
                "defaults: "
                f"threshold_ms={EXISTS_RELAY_THRESHOLD_MS:.3f}, "
                f"min_improve_ms={EXISTS_RELAY_MIN_IMPROVE_MS:.1f}, "
                f"min_improve_pct={EXISTS_RELAY_MIN_IMPROVE_PCT:.1f}"
            )
            return 0
        except BrokenPipeError:
            return 0

    if bool(args.all_sfus):
        # Compact summary across SFU placements + central SFU.
        try:
            stats_all: List[Stats] = []
            for sfu in range(n):
                if sfu == 0 or (sfu + 1) % 50 == 0 or (sfu + 1) == n:
                    _eprint(f"all-sfus progress: {sfu+1}/{n}")
                stats = compute_stats_for_sfu(
                    n=n,
                    avg_flat=avg_flat,
                    sfu_node=int(sfu),
                    threshold_ms=float(ALL_SFUS_THRESHOLD_MS),
                    ordered=False,
                    progress=False,
                    assume_all_values_valid=bool(assume_valid),
                )
                stats_all.append(stats)

            if stats_all:
                central_sfu = int(_pick_sfu_central_node(avg_flat, n))
                central_stats = stats_all[central_sfu]

                by_via_pct = sorted(
                    ((_pct(s.via_sfu_faster, s.pairs_classified), s.sfu_node) for s in stats_all),
                    key=lambda x: x[0],
                )
                by_mean_delta = sorted(
                    ((s.mean_delta_ms_overall, s.sfu_node) for s in stats_all),
                    key=lambda x: x[0],
                )
                by_mean_abs = sorted(
                    ((s.mean_abs_delta_ms_overall, s.sfu_node) for s in stats_all),
                    key=lambda x: x[0],
                )

                via_pcts = sorted([_pct(s.via_sfu_faster, s.pairs_classified) for s in stats_all])
                eq_pcts = sorted([_pct(s.roughly_equal, s.pairs_classified) for s in stats_all])
                p2p_pcts = sorted([_pct(s.p2p_faster, s.pairs_classified) for s in stats_all])
                mean_deltas = sorted([float(s.mean_delta_ms_overall) for s in stats_all])
                mean_abs_deltas = sorted([float(s.mean_abs_delta_ms_overall) for s in stats_all])

                via_mean = float(statistics.fmean(via_pcts))
                eq_mean = float(statistics.fmean(eq_pcts))
                p2p_mean = float(statistics.fmean(p2p_pcts))
                mean_delta_mean = float(statistics.fmean(mean_deltas))
                mean_abs_delta_mean = float(statistics.fmean(mean_abs_deltas))

                central_via_pct = _pct(central_stats.via_sfu_faster, central_stats.pairs_classified)
                central_eq_pct = _pct(central_stats.roughly_equal, central_stats.pairs_classified)
                central_p2p_pct = _pct(central_stats.p2p_faster, central_stats.pairs_classified)

                central_via_rank = _percentile_rank(via_pcts, central_via_pct, higher_is_better=True)
                central_mean_delta_rank = _percentile_rank(
                    mean_deltas, central_stats.mean_delta_ms_overall, higher_is_better=False
                )
                central_mean_abs_rank = _percentile_rank(
                    mean_abs_deltas, central_stats.mean_abs_delta_ms_overall, higher_is_better=False
                )

                sfus_with_any_via = sum(1 for s in stats_all if s.via_sfu_faster > 0)
                pct_sfus_with_any_via = (float(sfus_with_any_via) / float(n)) * 100.0

                print("All-SFU summary (varying SFU placement)")
                print(f"Dataset: n={n}, threshold={ALL_SFUS_THRESHOLD_MS:.3f} ms")
                print("Mapping to paper-style buckets: via_faster≈Shorter, equal≈Equal, p2p_faster≈Longer")
                print(f"SFU locations with any via_faster pairs: {pct_sfus_with_any_via:.2f}% ({sfus_with_any_via}/{n})")
                print("Note: SFU nodes are 0-based indices into the RTT matrix.")

                print(f"Chosen/central SFU (min median RTT): node={central_sfu}  <<<")
                print(
                    f"  via_faster: {central_via_pct:.2f}% ({central_stats.via_sfu_faster})"
                    f" | equal: {central_eq_pct:.2f}% ({central_stats.roughly_equal})"
                    f" | p2p_faster: {central_p2p_pct:.2f}% ({central_stats.p2p_faster})"
                )
                print(
                    f"  mean_delta (via-direct): {central_stats.mean_delta_ms_overall:.3f} ms"
                    f" | mean_abs_delta: {central_stats.mean_abs_delta_ms_overall:.3f} ms"
                )
                print(
                    "  representativeness among SFU choices (percentile; higher is better): "
                    f"via_faster%={central_via_rank:.1f}, "
                    f"mean_delta={central_mean_delta_rank:.1f}, "
                    f"mean_abs_delta={central_mean_abs_rank:.1f}"
                )

                print("Across all possible SFU nodes (distribution)")
                print(
                    "  via_faster (%): "
                    f"min={_percentile(via_pcts,0):.2f} | p50={_percentile(via_pcts,50):.2f} | "
                    f"mean={via_mean:.2f} | max={_percentile(via_pcts,100):.2f}"
                )
                print(
                    "  equal (%): "
                    f"min={_percentile(eq_pcts,0):.2f} | p50={_percentile(eq_pcts,50):.2f} | "
                    f"mean={eq_mean:.2f} | max={_percentile(eq_pcts,100):.2f}"
                )
                print(
                    "  p2p_faster (%): "
                    f"min={_percentile(p2p_pcts,0):.2f} | p50={_percentile(p2p_pcts,50):.2f} | "
                    f"mean={p2p_mean:.2f} | max={_percentile(p2p_pcts,100):.2f}"
                )
                print("Central vs typical SFU (percentages)")
                print(
                    f"  central(node {central_sfu}): via={central_via_pct:.2f}% | equal={central_eq_pct:.2f}% | p2p={central_p2p_pct:.2f}%"
                )
                print(
                    f"  median(SFU p50):    via={_percentile(via_pcts,50):.2f}% | equal={_percentile(eq_pcts,50):.2f}% | p2p={_percentile(p2p_pcts,50):.2f}%"
                )
                print(
                    f"  mean(across SFUs):  via={via_mean:.2f}% | equal={eq_mean:.2f}% | p2p={p2p_mean:.2f}%"
                )
                print(
                    "  mean_delta (ms, via-direct): "
                    f"min={_percentile(mean_deltas,0):.3f} | p50={_percentile(mean_deltas,50):.3f} | "
                    f"mean={mean_delta_mean:.3f} | p90={_percentile(mean_deltas,90):.3f} | "
                    f"max={_percentile(mean_deltas,100):.3f}"
                )
                print(
                    "  mean_abs_delta (ms): "
                    f"min={_percentile(mean_abs_deltas,0):.3f} | p50={_percentile(mean_abs_deltas,50):.3f} | "
                    f"mean={mean_abs_delta_mean:.3f} | p90={_percentile(mean_abs_deltas,90):.3f} | "
                    f"max={_percentile(mean_abs_deltas,100):.3f}"
                )

            return 0
        except BrokenPipeError:
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

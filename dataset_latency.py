#!/usr/bin/env python3
"""dataset_latency.py

Helper for dataset-driven latency simulation using NetLatency-Data.

Two-phase workflow:
  1) prepare: load all time slices, compute averaged RTT matrix, choose nodes
  2) generate-tables: for a scenario, generate per-container per-destination delay CSVs

All internal RTT values are converted to milliseconds.

This script intentionally uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import glob
import math
import os
import random
import statistics
import sys
from typing import Dict, Iterable, List, Optional, Tuple


def _write_mean_matrix_file(
    out_path: str,
    *,
    dataset: str,
    dataset_root: str,
    slices: int,
    units_input: str,
    n: int,
    avg_rtt_ms: List[float],
    observed_ms: List[float],
    zero_missing: bool,
) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    observed_sorted = sorted(observed_ms)

    def _pct(p: float) -> float:
        if not observed_sorted:
            return float("nan")
        k = int(round((p / 100.0) * (len(observed_sorted) - 1)))
        k = max(0, min(k, len(observed_sorted) - 1))
        return float(observed_sorted[k])

    # Single human-readable cached artifact:
    # - header lines start with '#'
    # - then N rows of N space-separated RTT values in milliseconds
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# dataset={dataset}\n")
        f.write(f"# dataset_root={os.path.abspath(dataset_root)}\n")
        f.write(f"# created_at_utc={_dt.datetime.utcnow().isoformat()}Z\n")
        f.write(f"# slices={slices}\n")
        f.write(f"# units_input={units_input}\n")
        f.write(f"# units_internal=ms\n")
        f.write(f"# n={n}\n")
        f.write(f"# zero_missing={int(bool(zero_missing))}\n")
        if observed_sorted:
            f.write(f"# observed_count={len(observed_sorted)}\n")
            f.write(f"# observed_ms_min={observed_sorted[0]:.6f}\n")
            f.write(f"# observed_ms_p50={_pct(50):.6f}\n")
            f.write(f"# observed_ms_p90={_pct(90):.6f}\n")
            f.write(f"# observed_ms_p99={_pct(99):.6f}\n")
            f.write(f"# observed_ms_max={observed_sorted[-1]:.6f}\n")
        f.write("# matrix: N lines; N cols; space-separated; RTT in ms\n")

        for i in range(n):
            base = i * n
            row = avg_rtt_ms[base : base + n]
            f.write(" ".join(f"{v:.6f}" for v in row) + "\n")


def _read_mean_matrix_file(path: str) -> Tuple[int, List[float]]:
    n_header: Optional[int] = None
    rows: List[List[float]] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                # example: '# n=490'
                try:
                    kv = line.lstrip("#").strip()
                    if kv.startswith("n="):
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


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def _find_slice_files(dataset_root: str, dataset: str) -> List[str]:
    if dataset not in ("PlanetLab", "Seattle"):
        raise ValueError(f"Unsupported dataset: {dataset}")
    folder = os.path.join(dataset_root, dataset)
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Dataset folder not found: {folder}")
    pattern = os.path.join(folder, f"{dataset}Data_*" if dataset == "Seattle" else f"{dataset}Data_*")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No slice files found with pattern: {pattern}")
    return files


def _infer_matrix_size(path: str) -> int:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            return len(parts)
    raise ValueError(f"Empty/invalid matrix file: {path}")


def _iter_matrix_rows(path: str, expected_n: int) -> Iterable[List[float]]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) != expected_n:
                raise ValueError(f"Row length mismatch in {path}: expected {expected_n}, got {len(parts)}")
            try:
                yield [float(x) for x in parts]
            except Exception as e:
                raise ValueError(f"Failed parsing floats in {path}: {e}")


def _units_to_ms_multiplier(units: str) -> float:
    u = units.strip().lower()
    if u in ("ms", "millisecond", "milliseconds"):
        return 1.0
    if u in ("s", "sec", "second", "seconds"):
        return 1000.0
    raise ValueError(f"Unknown units: {units}")


def _prepare_avg_matrix(
    slice_files: List[str],
    units: str,
    treat_zero_as_missing: bool,
) -> Tuple[int, List[float], List[int], List[float]]:
    """Return (n, sum_ms, count, observed_values_ms)."""

    n = _infer_matrix_size(slice_files[0])
    size = n * n
    sums = [0.0] * size
    counts = [0] * size
    observed: List[float] = []
    mul = _units_to_ms_multiplier(units)

    for fp in slice_files:
        i = 0
        for row in _iter_matrix_rows(fp, n):
            if i >= n:
                raise ValueError(f"Too many rows in {fp}")
            for j, v in enumerate(row):
                idx = i * n + j
                if i == j:
                    continue
                if treat_zero_as_missing and v == 0.0:
                    continue
                v_ms = v * mul
                # ignore negative or NaNs
                if not math.isfinite(v_ms) or v_ms < 0:
                    continue
                sums[idx] += v_ms
                counts[idx] += 1
                observed.append(v_ms)
            i += 1
        if i != n:
            raise ValueError(f"Row count mismatch in {fp}: expected {n}, got {i}")

    return n, sums, counts, observed


def _finalize_avg_matrix(
    n: int,
    sums: List[float],
    counts: List[int],
    observed_ms: List[float],
) -> List[float]:
    if not observed_ms:
        raise ValueError("No observed values found while averaging matrices")

    # robust fallback value for missing entries
    global_med = float(statistics.median(observed_ms))

    avg = [0.0] * (n * n)
    for i in range(n):
        for j in range(n):
            idx = i * n + j
            if i == j:
                avg[idx] = 0.0
                continue
            c = counts[idx]
            if c > 0:
                avg[idx] = sums[idx] / float(c)
            else:
                # Prefer symmetric counterpart if available; otherwise fall back to global median
                sym = counts[j * n + i]
                if sym > 0:
                    avg[idx] = sums[j * n + i] / float(sym)
                else:
                    avg[idx] = global_med

    return avg


def _row_values(avg: List[float], n: int, i: int) -> List[float]:
    base = i * n
    out = []
    for j in range(n):
        if i == j:
            continue
        out.append(avg[base + j])
    return out


def _pick_sfu_central_node(avg: List[float], n: int) -> int:
    # Centrality: minimize median RTT to others
    best_node = 0
    best_med = float("inf")
    for i in range(n):
        vals = _row_values(avg, n, i)
        if not vals:
            continue
        try:
            m = float(statistics.median(vals))
        except Exception:
            continue
        if m < best_med:
            best_med = m
            best_node = i
    return best_node


def _seeded_sample_nodes(n: int, exclude: int, k: int, seed: int) -> List[int]:
    pool = [i for i in range(n) if i != exclude]
    rng = random.Random(int(seed))
    rng.shuffle(pool)
    return pool[:k]


def cmd_prepare_mean(args: argparse.Namespace) -> int:
    slice_files = _find_slice_files(args.dataset_root, args.dataset)

    # dataset-specific default units
    if args.units:
        units = args.units
    else:
        # Per user note: PlanetLab is ms; Seattle likely seconds
        units = "ms" if args.dataset == "PlanetLab" else "seconds"

    n, sums, counts, observed_ms = _prepare_avg_matrix(
        slice_files=slice_files,
        units=units,
        treat_zero_as_missing=bool(args.zero_missing),
    )
    avg = _finalize_avg_matrix(n, sums, counts, observed_ms)

    os.makedirs(args.out_dir, exist_ok=True)
    out_txt = os.path.join(args.out_dir, f"avg_{args.dataset}_rtt_ms.txt")
    _write_mean_matrix_file(
        out_txt,
        dataset=args.dataset,
        dataset_root=args.dataset_root,
        slices=len(slice_files),
        units_input=units,
        n=n,
        avg_rtt_ms=avg,
        observed_ms=observed_ms,
        zero_missing=bool(args.zero_missing),
    )

    # Print the mean matrix path (consumable by bash)
    print(out_txt)
    return 0


def _ip_for(role: str, net_prefix: str) -> str:
    # Docker IP scheme used by experimental_evaluation.sh:
    # host: 172.28.0.2, client i: 172.28.0.(2+i)
    if role == "host":
        return f"{net_prefix}2"
    if role.startswith("client"):
        try:
            i = int(role.replace("client", ""))
        except Exception:
            raise ValueError(f"Invalid role: {role}")
        return f"{net_prefix}{2 + i}"
    raise ValueError(f"Invalid role: {role}")


def _rtt_ms_from_flat(avg_flat: List[float], n: int, src_node: int, dst_node: int) -> float:
    return float(avg_flat[src_node * n + dst_node])


def cmd_generate_tables(args: argparse.Namespace) -> int:
    n, avg_flat = _read_mean_matrix_file(args.mean_matrix)

    clients = int(args.clients)
    if clients <= 0:
        raise ValueError("--clients must be positive")
    if clients >= n:
        raise ValueError(f"Requested clients={clients} too large for n={n} (need clients <= n-1)")

    arch = str(args.arch).strip()
    seed = int(args.seed)

    os.makedirs(args.out_dir, exist_ok=True)

    # build role->node mapping for this scenario
    host_node = int(_pick_sfu_central_node(avg_flat, n))
    client_nodes = _seeded_sample_nodes(n, exclude=host_node, k=clients, seed=seed)
    role_to_node: Dict[str, int] = {"host": host_node}
    for i in range(1, clients + 1):
        role_to_node[f"client{i}"] = int(client_nodes[i - 1])

    # Write effective mapping for this scenario (subset of prepared max_clients)
    mapping_path = os.path.join(args.out_dir, "node_mapping_effective.csv")
    with open(mapping_path, "w", newline="") as f:
        w = csv.writer(f, delimiter=';', lineterminator='\n')
        w.writerow(["role", "dataset_node_index", "container_ip"])
        for role, node in role_to_node.items():
            w.writerow([role, int(node), _ip_for(role, args.net_prefix)])

    # determine dst sets
    roles = ["host"] + [f"client{i}" for i in range(1, clients + 1)]
    def _dst_roles_for(src_role: str) -> List[str]:
        # IMPORTANT: tc rules must be architecture-independent.
        # We always emit destinations for all other roles, even if the app
        # architecture will not use some of those routes.
        return [r for r in roles if r != src_role]

    # write a small info file for traceability
    with open(os.path.join(args.out_dir, "info.txt"), "w", encoding="utf-8") as f:
        f.write(f"mean_matrix={os.path.abspath(args.mean_matrix)}\n")
        f.write(f"clients={clients}\n")
        f.write(f"seed={seed}\n")
        f.write(f"host_node={host_node}\n")
        f.write("client_nodes=" + ",".join(str(int(x)) for x in client_nodes) + "\n")
        f.write(f"arch={arch}\n")
        f.write(f"rtt_units=ms\n")
        f.write(f"oneway_model=rtt/2\n")
        f.write(f"net_prefix={args.net_prefix}\n")

    delay_table_rows: List[Tuple[str, str, int]] = []
    for src_role in roles:
        src_node = role_to_node[src_role]
        dst_roles = _dst_roles_for(src_role)

        out_path = os.path.join(args.out_dir, f"{src_role}.csv")
        with open(out_path, "w", newline="") as f:
            w = csv.writer(f, delimiter=';', lineterminator='\n')
            w.writerow(["dst_ip", "delay_ms"])  # one-way egress delay
            for dst_role in dst_roles:
                dst_node = role_to_node[dst_role]
                rtt = _rtt_ms_from_flat(avg_flat, n, src_node, dst_node)
                delay_ms = max(0, int(round(rtt / 2.0)))
                w.writerow([_ip_for(dst_role, args.net_prefix), delay_ms])
                delay_table_rows.append((src_role, _ip_for(dst_role, args.net_prefix), delay_ms))

    # Write a combined delay table for debugging/auditing
    combined_path = os.path.join(args.out_dir, "delay_table.csv")
    with open(combined_path, "w", newline="") as f:
        w = csv.writer(f, delimiter=';', lineterminator='\n')
        w.writerow(["src_role", "dst_ip", "delay_ms"])
        for src_role, dst_ip, delay_ms in delay_table_rows:
            w.writerow([src_role, dst_ip, int(delay_ms)])

    # Print out_dir for bash convenience
    print(args.out_dir)
    return 0


def cmd_nodes(args: argparse.Namespace) -> int:
    """Emit selected node IDs for the scenario to stdout (human-readable)."""
    n, avg_flat = _read_mean_matrix_file(args.mean_matrix)
    clients = int(args.clients)
    if clients <= 0:
        raise ValueError("--clients must be positive")
    if clients >= n:
        raise ValueError(f"Requested clients={clients} too large for n={n} (need clients <= n-1)")

    seed = int(args.seed)
    host_node = int(_pick_sfu_central_node(avg_flat, n))
    client_nodes = _seeded_sample_nodes(n, exclude=host_node, k=clients, seed=seed)
    # Single-line, easy to embed into metadata
    print(f"host={host_node} clients=" + ",".join(str(x) for x in client_nodes))
    return 0


def cmd_table_for_role(args: argparse.Namespace) -> int:
    """Emit dst_ip;delay_ms table for a single role to stdout (no files)."""
    n, avg_flat = _read_mean_matrix_file(args.mean_matrix)
    clients = int(args.clients)
    if clients <= 0:
        raise ValueError("--clients must be positive")
    if clients >= n:
        raise ValueError(f"Requested clients={clients} too large for n={n} (need clients <= n-1)")

    arch = str(args.arch).strip()
    role = str(args.role).strip()
    seed = int(args.seed)

    host_node = int(_pick_sfu_central_node(avg_flat, n))
    client_nodes = _seeded_sample_nodes(n, exclude=host_node, k=clients, seed=seed)

    role_to_node: Dict[str, int] = {"host": host_node}
    for i in range(1, clients + 1):
        role_to_node[f"client{i}"] = int(client_nodes[i - 1])

    roles = ["host"] + [f"client{i}" for i in range(1, clients + 1)]
    if role not in roles:
        raise ValueError(f"Unknown role '{role}' for clients={clients}")

    def _dst_roles_for(src_role: str) -> List[str]:
        # IMPORTANT: tc rules must be architecture-independent.
        # We always emit destinations for all other roles, even if the app
        # architecture will not use some of those routes.
        return [r for r in roles if r != src_role]

    src_node = role_to_node[role]
    dst_roles = _dst_roles_for(role)

    out = csv.writer(sys.stdout, delimiter=';', lineterminator='\n')
    out.writerow(["dst_ip", "delay_ms"])
    for dst_role in dst_roles:
        dst_node = role_to_node[dst_role]
        rtt = _rtt_ms_from_flat(avg_flat, n, src_node, dst_node)
        delay_ms = max(0, int(round(rtt / 2.0)))
        out.writerow([_ip_for(dst_role, args.net_prefix), delay_ms])
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Dataset-driven latency utilities (NetLatency-Data)")
    sp = p.add_subparsers(dest="cmd", required=True)

    p_prep = sp.add_parser("prepare-mean", help="Compute mean RTT matrix (ms) and write transparent text output")
    p_prep.add_argument("--dataset", choices=["PlanetLab", "Seattle"], required=True)
    p_prep.add_argument("--dataset-root", required=True, help="Path containing PlanetLab/ and Seattle/ folders")
    p_prep.add_argument("--out-dir", required=True, help="Output directory for mean matrix file")
    p_prep.add_argument("--units", default="", help="Override dataset units: ms|seconds")
    p_prep.add_argument("--zero-missing", action="store_true", help="Treat off-diagonal 0.0 as missing")
    p_prep.set_defaults(func=cmd_prepare_mean)

    # Backwards compatible alias (older runner versions used `prepare`)
    p_prep_alias = sp.add_parser("prepare", help="Alias for prepare-mean")
    p_prep_alias.add_argument("--dataset", choices=["PlanetLab", "Seattle"], required=True)
    p_prep_alias.add_argument("--dataset-root", required=True, help="Path containing PlanetLab/ and Seattle/ folders")
    p_prep_alias.add_argument("--out-dir", required=True, help="Output directory for mean matrix file")
    p_prep_alias.add_argument("--units", default="", help="Override dataset units: ms|seconds")
    p_prep_alias.add_argument("--zero-missing", action="store_true", help="Treat off-diagonal 0.0 as missing")
    p_prep_alias.set_defaults(func=cmd_prepare_mean)

    p_gen = sp.add_parser("generate-tables", help="Generate per-container delay CSVs for a scenario (debugging)")
    p_gen.add_argument("--mean-matrix", required=True, help="Path to avg_<Dataset>_rtt_ms.txt")
    p_gen.add_argument("--clients", type=int, required=True)
    p_gen.add_argument("--arch", choices=["P2P_Mesh", "SFU", "Hybrid"], required=True)
    p_gen.add_argument("--out-dir", required=True)
    p_gen.add_argument("--net-prefix", default="172.28.0.", help="IP prefix used by containers")
    p_gen.add_argument("--seed", type=int, default=1)
    p_gen.set_defaults(func=cmd_generate_tables)

    p_nodes = sp.add_parser("nodes", help="Print selected dataset node ids for a scenario")
    p_nodes.add_argument("--mean-matrix", required=True)
    p_nodes.add_argument("--clients", type=int, required=True)
    p_nodes.add_argument("--seed", type=int, default=1)
    p_nodes.set_defaults(func=cmd_nodes)

    p_role = sp.add_parser("table-for-role", help="Print dst_ip;delay_ms table for one role")
    p_role.add_argument("--mean-matrix", required=True)
    p_role.add_argument("--clients", type=int, required=True)
    p_role.add_argument("--arch", choices=["P2P_Mesh", "SFU", "Hybrid"], required=True)
    p_role.add_argument("--role", required=True, help="host|clientN")
    p_role.add_argument("--net-prefix", default="172.28.0.")
    p_role.add_argument("--seed", type=int, default=1)
    p_role.set_defaults(func=cmd_table_for_role)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as e:
        _eprint(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

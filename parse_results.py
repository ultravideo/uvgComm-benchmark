#!/usr/bin/env python3
"""
parse_results.py

Analyze experimental evaluation results for multiple architectures.

Produces:
 - CPU usage vs number of clients (per-architecture)
 - PSNR (Y) vs number of clients (per-architecture)
 - Missing frame detection summaries and simple visualizations
 - Resolution / frame-size summaries
 - Latency/encode/decode summaries

Usage:
    ./parse_results.py /path/to/<timestamp_folder>

The script writes CSV summaries and SVG/PNG plots under <timestamp_folder>/analysis/
"""

import math
import os
import glob
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import argparse
import matplotlib
import numpy as np
import concurrent.futures
from collections import defaultdict
from itertools import groupby

# Centralized architecture color mapping for consistent plots.
# Keys are checked case-insensitively/substrings in `get_color_map`.
ARCH_COLOR_MAP = {
    'p2p': '#1f77b4',      # P2P Mesh
    'sfu': '#ff7f0e',      # SFU
    'hybrid': '#2ca02c'    # Hybrid
}
# Use non-interactive backend for matplotlib
matplotlib.use("Agg")

# limit unmatched-frame debug prints per detect_missing_frames run
_UNMATCHED_PRINT_LIMIT = int(os.environ.get('UVGCOMM_UNMATCHED_PRINT_LIMIT', '5'))
_unmatched_print_count = 0

GRAPH_NUM_CLIENT_LABEL = 'Number of Clients'
FIGSIZE = (6,4)

def read_csv_guess(path, na_values=["", "NA", "null"], dtype=None):
    """Try to read CSV using common separators. Returns DataFrame or None on failure."""
    for sep in [';', ',', '\t']:
        try:
            df = pd.read_csv(path, sep=sep, engine='python', na_values=na_values, dtype=dtype)
            if df is not None and df.shape[1] > 1:
                df.columns = [c.strip() for c in df.columns]
                return df
        except Exception as e:
            continue
    try:
        # fallback: pandas default
        df = pd.read_csv(path, engine='python', na_values=na_values, dtype=dtype)
        if df is not None:
            df.columns = [c.strip() for c in df.columns]
            return df
    except Exception as e:
        print(f'ERROR: read_csv_guess failed for {path}: {e}')
        return None


def parse_metadata(metadata_path):
    d = {}
    if not os.path.isfile(metadata_path):
        return d
    with open(metadata_path, 'r') as f:
        for line in f:
            if ':' not in line:
                continue
            k, v = line.split(':', 1)
            d[k.strip()] = v.strip()
    # try to coerce some common numeric values
    for key in ['Clients', 'Start_Timestamp', 'End_timestamp', 'Run']:
        if key in d:
            try:
                d[key] = int(d[key])
            except Exception as e:
                print(f'WARNING: Failed to coerce {key}={d[key]} to int: {e}')
    return d


def find_latest_run(arch_folder):
    """arch_folder is path to e.g. .../720p/SFU-2 ; find run_* subfolder with largest number."""
    runs = [p for p in glob.glob(os.path.join(arch_folder, 'run_*')) if os.path.isdir(p)]
    if not runs:
        return None
    best = None
    best_n = -1
    for r in runs:
        base = os.path.basename(r)
        try:
            n = int(base.split('_', 1)[1])
        except Exception as e:
            print(f'WARNING: Failed to extract run number from {base}: {e}')
            n = -1
        if n > best_n:
            best_n = n
            best = r
    return best


def ensure_dir(p):
    os.makedirs(p, exist_ok=True)


def find_timestamp_column(df):
    """Return the best candidate timestamp column name or None."""
    if df is None or df.columns is None:
        return None
    for c in df.columns:
        lc = c.lower()
        if 'timestamp' in lc or lc == 'time' or 'timestamp_ms' in lc:
            return c
    # fallback to first column
    try:
        return df.columns[0]
    except Exception as e:
        print(f'WARNING: Failed to get first column from DataFrame: {e}')
        return None
def filter_df_by_ts(df, start_ts, end_ts):
    """Filter DataFrame by timestamp interval [start_ts, end_ts] using a guessed timestamp column.

    If start_ts or end_ts is None, returns the original df.
    If filtering fails for any reason, returns the original df.
    """
    if df is None or start_ts is None or end_ts is None:
        return df
    tscol = find_timestamp_column(df)
    if tscol is None:
        return df
    try:
        df_ts = pd.to_numeric(df[tscol], errors='coerce')
        return df[(df_ts >= start_ts) & (df_ts <= end_ts)]
    except Exception as e:
        print(f'WARNING: Failed to filter DataFrame by timestamp range [{start_ts}, {end_ts}]: {e}')
        return df


def extract_numeric_list(df, candidates, dtype=int):
    """Search candidate column names in df and return a list of numeric values and the found column name.

    Returns (values_list, column_name) where values_list is empty and column_name is None if nothing found.
    """
    if df is None or df.columns is None:
        return [], None
    # exact name match first
    for col in candidates:
        if col in df.columns:
            vals = pd.to_numeric(df[col], errors='coerce').dropna()
            try:
                vals = vals.astype(dtype).tolist()
            except Exception as e:
                print(f'WARNING: Failed to convert column {col} to dtype {dtype}: {e}')
                vals = vals.tolist()
            return vals, col
    # fallback: find by substring match
    clow = [c.lower() for c in df.columns]
    for i, col in enumerate(df.columns):
        for cand in candidates:
            if cand.lower() in clow[i]:
                vals = pd.to_numeric(df[col], errors='coerce').dropna()
                try:
                    vals = vals.astype(dtype).tolist()
                except Exception as e:
                    print(f'WARNING: Failed to convert column {col} to dtype {dtype}: {e}')
                    vals = vals.tolist()
                return vals, col
    return [], None


def get_min_max_ts(df):
    """Return (min_ts, max_ts) found in DataFrame by guessing a timestamp column, or (None, None)."""
    if df is None or df.columns is None:
        return None, None
    tscol = find_timestamp_column(df)
    if tscol is None:
        return None, None
    try:
        tsvals = pd.to_numeric(df[tscol], errors='coerce').dropna().astype(int).values
        if tsvals.size:
            return int(tsvals.min()), int(tsvals.max())
    except Exception as e:
        print(f'WARNING: Failed to extract min/max timestamps: {e}')
    return None, None


def _match_with_offset(local_sizes, part_sizes, lookahead, verbose=False):
    """Greedy matcher: return delivered count.

    delivered: number of matched local frames (normal-frame matching only)
    """
    delivered = 0
    global _unmatched_print_count

    # quick checks
    if not local_sizes or not part_sizes:
        return 0

    n_local = len(local_sizes)
    n_part = len(part_sizes)

    # One-time initial lookahead (in frames) used only to align the first local frames
    # double the previous lookahead as requested
    INITIAL_LOOKAHEAD = 200

    # --- Phase 1: initial alignment search
    # Try to find a starting participant index `k` such that we can match
    # `need_n` consecutive local frames to participant frames while allowing
    # a small number of duplicate/short participant entries between matches.
    need_n = min(3, n_local)
    max_k = min(0 + INITIAL_LOOKAHEAD, n_part - 1)
    found_k = None
    # capture participant index after consuming initial consecutive matches
    found_p_idx = None
    # allow up to this many skipped participant entries between matched local frames
    MAX_PART_SKIP = 20
    for k in range(0, max_k + 1):
        p_idx = k
        ok = True
        for offset in range(need_n):
            ls_n = local_sizes[offset]
            # try to find a participant index p_idx' in [p_idx, p_idx + MAX_PART_SKIP]
            match_found = False
            for skip in range(0, MAX_PART_SKIP + 1):
                p_try = p_idx + skip
                if p_try >= n_part:
                    break
                ps = part_sizes[p_try]
                if ps == ls_n or ps == ls_n + 1 or ps == ls_n - 1:
                    # accept this match and advance p_idx to the next position
                    p_idx = p_try + 1
                    match_found = True
                    break
            if not match_found:
                ok = False
                break
        if ok:
            found_k = k
            # p_idx has been advanced while matching the initial consecutive frames;
            # keep it so sequential phase resumes from the correct participant index
            found_p_idx = p_idx
            break

    if found_k is None:
        if verbose and _unmatched_print_count < _UNMATCHED_PRINT_LIMIT:
            print(f"Could not find initial {need_n}-frame alignment within lookahead starting at local_pos=0")
            _unmatched_print_count += 1
        return 0

    # consume the initial matched run and enter sequential phase
    delivered += need_n
    i = need_n
    # start sequential participant index at the advanced participant position
    if found_p_idx is not None:
        j = found_p_idx
    else:
        j = found_k + need_n

    # --- Phase 2: sequential per-frame matching
    while i < n_local:
        # If participant frames are exhausted, remaining local frames are unmatched -> stop
        if j >= n_part:
            if verbose and _unmatched_print_count < _UNMATCHED_PRINT_LIMIT:
                remaining = n_local - i
                _unmatched_print_count += 1
            break

        ls = local_sizes[i]
        ps = part_sizes[j]

        # direct match
        if ps == ls or ps == ls + 1 or ps == ls - 1:
            delivered += 1
            i += 1
            j += 1
            continue
        # participant lookahead: check the next 1..2 participant entries
        lookahead_matched = False
        for k in (1, 2):
            if (j + k) < n_part:
                ps_k = part_sizes[j + k]
                if ps_k == ls or ps_k == ls + 1 or ps_k == ls - 1:
                    if verbose and _unmatched_print_count < _UNMATCHED_PRINT_LIMIT:
                        skipped_sizes = ','.join(str(x) for x in part_sizes[j:j+k])
                        _unmatched_print_count += 1
                    delivered += 1
                    i += 1
                    j += (k + 1)
                    lookahead_matched = True
                    break

        if lookahead_matched:
            continue

        # no match -> unmatched local frame
        if verbose and _unmatched_print_count < _UNMATCHED_PRINT_LIMIT:
            print(f"Unmatched sequential local frame: pos={i} size={ls} expected_part_size={ps} participant_idx={j}")
            _unmatched_print_count += 1
        i += 1

    return delivered


def detect_missing_frames(local_by_cname, participant_by_cname, start_ts=None, end_ts=None,
                                                    lookahead=3):
    """Detect missing frames by comparing size sequences between local and participant traces.

    Uses a deterministic greedy matcher (normal-frame matching only with a small tolerance).

    Optional arguments:
        start_ts, end_ts: if provided, local frames will be filtered to the closed
            interval [start_ts, end_ts] before matching. Participant traces are trimmed
            at the start to align with start_ts but are not truncated at the end.

    Returns a list of dicts with keys: cname, receiver_folder, total_local_frames,
    delivered, missing, pct_missing
    """
    # reset per-run unmatched-frame print counter
    global _unmatched_print_count
    _unmatched_print_count = 0
    missing_summary = []

    # for each cname in local results
    for cname, local_info in local_by_cname.items():
        # Work on a local copy to avoid mutating input structures
        local_df = local_info['df']
        # apply timestamp window filtering using helper
        local_df = filter_df_by_ts(local_df, start_ts, end_ts)
        # find size values in local using helper
        local_sizes, size_col = extract_numeric_list(local_df, ['Size(Bytes)', 'Size'], dtype=int)
        # If no sizes, skip
        if not local_sizes:
            continue

        if cname not in participant_by_cname:
            print(f"Warning: Could not find cname in participant list: {cname}")
            continue
        for pinfo in participant_by_cname[cname]:
            p_df = pinfo['df']
            # Ensure participant traces are trimmed at the run start timestamp
            if start_ts is not None and p_df is not None:
                try:
                    tscol = find_timestamp_column(p_df)
                    if tscol is not None:
                        df_ts = pd.to_numeric(p_df[tscol], errors='coerce')
                        p_df = p_df[df_ts >= start_ts]
                except Exception as e:
                    # keep original p_df on any failure
                    print(f'WARNING: Failed to trim participant trace at start_ts={start_ts}: {e}')
            # find size column in participant csv results
            part_sizes, part_size_col = extract_numeric_list(p_df, ['Size(Bytes)', 'Size'], dtype=int)

            # If no sizes
            if not part_sizes:
                continue

            # Run matcher once (no intra/offset heuristics)
            delivered = _match_with_offset(local_sizes, part_sizes, lookahead, verbose=True)

            total_local = len(local_sizes)
            missing = max(0, total_local - delivered)
            pct_missing = 100.0 * missing / total_local if total_local > 0 else None
            missing_summary.append({'cname': cname, 'receiver_folder': pinfo.get('client_folder'),
                                     'local_folder': local_info.get('client_folder'),
                                     'total_local_frames': total_local, 'delivered': delivered,
                                     'missing': missing, 'pct_missing': pct_missing})

    return missing_summary


def analyze_run(run_path):
    """Analyze a single run directory (contains cpu_usage.csv, metadata.txt, uvgcomm-client* folders).
    Returns a dictionary of aggregated metrics.
    """
    print(f"Analyzing run: {run_path}")
    metrics = {}
    metadata = parse_metadata(os.path.join(run_path, 'metadata.txt'))
    metrics['metadata'] = metadata
    # Determine visible participants limit from metadata (if present).
    visible_limit = None
    for vk in ('Visible_Participants', 'VisibleParticipants', 'Visible', 'VisibleCount'):
        if vk in metadata:
            try:
                visible_limit = int(metadata[vk])
            except Exception as e:
                print(f'WARNING: Failed to parse visible_limit from {vk}={metadata[vk]}: {e}')
                visible_limit = None
            break
    metrics['visible_participants'] = visible_limit

    # CPU usage
    cpu_file = os.path.join(run_path, 'cpu_usage.csv')
    cpu_avg = None
    if os.path.isfile(cpu_file):
        cpu_df = read_csv_guess(cpu_file)
        if cpu_df is not None:
            # expect timestamp_ms;cpu_percent or similar
            ts_col = None
            for c in cpu_df.columns:
                if 'timestamp' in c.lower() or 'time' in c.lower():
                    ts_col = c
                    break
            pct_col = None
            for c in cpu_df.columns:
                if 'cpu' in c.lower() or 'percent' in c.lower():
                    pct_col = c
                    break
            if ts_col and pct_col:
                cpu_df = cpu_df.dropna(subset=[ts_col, pct_col])
                try:
                    cpu_df[ts_col] = cpu_df[ts_col].astype(int)
                except Exception as e:
                    print(f'WARNING: Failed to convert CPU timestamp column to int: {e}')
                start = metadata.get('Start_Timestamp')
                end = metadata.get('End_timestamp') or metadata.get('End_Timestamp')
                if start and end:
                    sel = cpu_df[(cpu_df[ts_col] >= start) & (cpu_df[ts_col] <= end)]
                else:
                    sel = cpu_df
                try:
                    cpu_avg = float(pd.to_numeric(sel[pct_col], errors='coerce').dropna().mean())
                except Exception as e:
                    print(f'WARNING: Failed to compute average CPU percentage: {e}')
                    cpu_avg = None
    metrics['cpu_avg'] = cpu_avg

    # discover client folders (only client containers) and host folder(s) separately.
    # Host containers never have local/participant CSVs, so avoid scanning them
    # when collecting per-client traces. Keep host folders available for
    # bandwidth parsing by combining them into `bandwidth_folders` below.
    client_folders = sorted([p for p in glob.glob(os.path.join(run_path, 'uvgcomm-client*')) if os.path.isdir(p)])
    host_folders = sorted([p for p in glob.glob(os.path.join(run_path, 'uvgcomm-host')) if os.path.isdir(p)])
    # If a visible participants limit is present, filter client folders so only
    # clients with numeric id <= visible_limit contribute to per-client metrics.
    if metrics.get('visible_participants') is not None:
        vl = metrics['visible_participants']
        def _keep_client(folder):
            try:
                num = _extract_client_num_from_folder(folder)
                return (num is None) or (num <= vl)
            except Exception as e:
                print(f'WARNING: Failed to check client visibility for folder {folder}: {e}')
                return True
        client_folders = [c for c in client_folders if _keep_client(c)]
    # combined list used for measured bandwidth parsing (clients + host)
    bandwidth_folders = client_folders + host_folders

    # apply timestamp filtering window from metadata if available
    start_ts = metadata.get('Start_Timestamp')
    end_ts = metadata.get('End_timestamp') or metadata.get('End_Timestamp')

    # collect per-cname local and participant files
    local_by_cname = {}
    participant_by_cname = defaultdict(list)

    for cfolder in client_folders:
        # local files
        for path in glob.glob(os.path.join(cfolder, 'local_*.csv')):
            base = os.path.basename(path)
            # local_{CNAME}.csv
            cname = base.split('local_', 1)[1].rsplit('.', 1)[0]
            df = read_csv_guess(path)
            if df is None:
                continue
            # filter by timestamp range if possible
            df = filter_df_by_ts(df, start_ts, end_ts)
            local_by_cname[cname] = {'path': path, 'df': df, 'client_folder': cfolder}
        # participant files
        for path in glob.glob(os.path.join(cfolder, 'participant_*.csv')):
            base = os.path.basename(path)
            cname = base.split('participant_', 1)[1].rsplit('.', 1)[0]
            df = read_csv_guess(path)
            if df is None:
                continue
            # Limit participant traces at the beginning of the run window (drop any rows
            # with timestamps earlier than start_ts) but do NOT truncate the end —
            # sent frames may arrive after the run end timestamp.
            if start_ts is not None:
                try:
                    tscol = find_timestamp_column(df)
                    if tscol is not None:
                        df_ts = pd.to_numeric(df[tscol], errors='coerce')
                        df = df[df_ts >= start_ts]
                except Exception as e:
                    # if filtering fails, keep original df
                    print(f'WARNING: Failed to filter participant trace at start_ts={start_ts}: {e}')
            participant_by_cname[cname].append({'path': path, 'df': df, 'client_folder': cfolder})

    metrics['local_by_cname'] = local_by_cname
    metrics['participant_by_cname'] = participant_by_cname

    # Debugging aid: if participant traces exist but later no latencies are found,
    # it helps to know what columns are present in a sample participant file.
    try:
        total_part_files = sum(len(v) for v in participant_by_cname.values())
        if total_part_files == 0:
            print(f"[debug] No participant_*.csv files found in run {run_path}")
        else:
            # find first non-empty df to inspect columns
            sample_cols = None
            for plist in participant_by_cname.values():
                for info in plist:
                    if info.get('df') is not None:
                        try:
                            sample_cols = list(info.get('df').columns)
                        except Exception as e:
                            print(f'WARNING: Failed to inspect sample participant columns: {e}')
                            sample_cols = None
                        break
                if sample_cols is not None:
                    break
            # sample participant columns inspected during development; omit verbose debug output
    except Exception as e:
        print(f'WARNING: Failed to collect participant file metadata: {e}')

    # PSNR: collect per-client PSNR and overall average
    psnr_values = []
    psnr_per_client = []
    for cname, info in local_by_cname.items():
        df = info['df']
        if df is None:
            continue
        if 'PSNR_Y' not in df.columns:
            print(f"ERROR: missing PSNR_Y column for local trace {info.get('path')}")
            continue
        vals = pd.to_numeric(df['PSNR_Y'], errors='coerce').dropna().tolist()
        if vals:
            mean_psnr = float(np.mean(vals))
            client_folder = info.get('client_folder')
            client_num = _extract_client_num_from_folder(client_folder)
            psnr_per_client.append({'client_folder': client_folder, 'client_num': client_num, 'mean_psnr': mean_psnr})
            psnr_values.extend(vals)
    metrics['avg_psnr'] = float(np.mean(psnr_values)) if psnr_values else None
    metrics['psnr_count'] = len(psnr_values)
    metrics['psnr_per_client'] = psnr_per_client

    # Frame sizes and resolution stats from local frames
    # Define column candidates for metrics extraction (controlled by extract_bandwidth flag)
    use_new_size = True
    size_col_candidates = ['BandwidthCost(Bytes)', 'BandwidthCost'] if use_new_size else ['Size(Bytes)', 'Size']
    
    sizes = []
    widths = []
    heights = []
    encode_times = []
    # per-client max encode time (ms)
    max_encode_by_client = {}
    # per-client max decode time (ms)
    max_decode_by_client = {}
    # Client reported network latency (ms) based on RTT measurements
    network_latencies = []
    out_total_bytes = 0
    out_min_ts = None
    out_max_ts = None
    for cname, info in local_by_cname.items():
        df = info['df']
        if df is None:
            continue
        # Extract sizes (required)
        vals, sc = extract_numeric_list(df, size_col_candidates, dtype=int)
        if vals:
            sizes.extend(vals)
            out_total_bytes += sum(vals)
        mn_ts, mx_ts = get_min_max_ts(df)
        if mn_ts is not None:
            out_min_ts = mn_ts if out_min_ts is None else min(out_min_ts, mn_ts)
        if mx_ts is not None:
            out_max_ts = mx_ts if out_max_ts is None else max(out_max_ts, mx_ts)

        wvals, _ = extract_numeric_list(df, ['Width', 'width', 'W'], dtype=float)
        if wvals:
            widths.extend(wvals)

        hvals, _ = extract_numeric_list(df, ['Height', 'height', 'H'], dtype=float)
        if hvals:
            heights.extend(hvals)

        encvals, _ = extract_numeric_list(df, ['EncodeTime(ms)', 'EncodeTime', 'EncodeTimeMs', 'EncodeTime (ms)'], dtype=float)
        if encvals:
            encode_times.extend(encvals)
            # record per-client max encode time (key by client folder path for reliable matching)
            try:
                cfolder = info.get('client_folder')
                key = cfolder if cfolder is not None else None
                cur = max(encvals) if encvals else None
                if cur is not None:
                    if key in max_encode_by_client:
                        try:
                            max_encode_by_client[key] = max(cur, float(max_encode_by_client.get(key, float('-inf'))))
                        except Exception as e:
                            print(f'WARNING: Failed to compute max encode time: {e}')
                            max_encode_by_client[key] = cur
                    else:
                        max_encode_by_client[key] = cur
            except Exception as e:
                print(f'WARNING: Failed to record per-client max encode time: {e}')
        nlvals, _ = extract_numeric_list(df, ['NetworkLatency(ms)', 'NetworkLatency', 'NetworkLatencyMs'], dtype=float)
        if nlvals:
            # Network latency uses -1 as a sentinel for "no measurement"; exclude negatives.
            try:
                for v in nlvals:
                    try:
                        fv = float(v)
                    except Exception:
                        continue
                    if fv >= 0:
                        network_latencies.append(fv)
            except Exception as e:
                print(f'WARNING: Failed to collect network latency values: {e}')
    metrics['avg_frame_size'] = float(np.mean(sizes)) if sizes else None
    metrics['avg_width'] = float(np.mean(widths)) if widths else None
    metrics['avg_height'] = float(np.mean(heights)) if heights else None
    metrics['avg_encode_ms'] = float(np.mean(encode_times)) if encode_times else None
    
    metrics['avg_network_latency_ms'] = float(np.mean(network_latencies)) if network_latencies else None

    # Latency / decode times / participant-side stats
    latencies = []
    decode_times = []
    participant_sizes = []
    in_total_bytes = 0
    in_min_ts = None
    in_max_ts = None
    # per-sender latency means (collect mean latency for each sender cname)
    latency_per_sender = []
    for cname, plist in participant_by_cname.items():
        # collect latency values across all receivers for this sender
        sender_lvals = []
        for info in plist:
            df = info['df']
            if df is None:
                continue
            lvals, _ = extract_numeric_list(df, ['Latency(ms)', 'Latency', 'latency'], dtype=float)
            if lvals:
                # Latency may use negative sentinel values (e.g., -1) for "unknown".
                try:
                    for v in lvals:
                        try:
                            fv = float(v)
                        except Exception:
                            continue
                        if fv >= 0:
                            latencies.append(fv)
                            sender_lvals.append(fv)
                except Exception as e:
                    print(f'WARNING: Failed to collect participant latency values: {e}')

            dvals, _ = extract_numeric_list(df, ['DecodeTime(ms)', 'DecodeTime', 'DecodeTimeMs'], dtype=float)
            if dvals:
                decode_times.extend(dvals)
                # record per-client max decode time (key by client folder path for reliable matching)
                try:
                    cfolder = info.get('client_folder')
                    key = cfolder if cfolder is not None else None
                    curd = max(dvals) if dvals else None
                    if curd is not None:
                        if key in max_decode_by_client:
                            try:
                                max_decode_by_client[key] = max(curd, float(max_decode_by_client.get(key, float('-inf'))))
                            except Exception as e:
                                print(f'WARNING: Failed to compute max decode time: {e}')
                                max_decode_by_client[key] = curd
                        else:
                            max_decode_by_client[key] = curd
                except Exception as e:
                    print(f'WARNING: Failed to record per-client max decode time: {e}')

            pvals, _ = extract_numeric_list(df, size_col_candidates, dtype=int)
            if pvals:
                participant_sizes.extend(pvals)
                in_total_bytes += sum(pvals)

            mn_ts, mx_ts = get_min_max_ts(df)
            if mn_ts is not None:
                in_min_ts = mn_ts if in_min_ts is None else min(in_min_ts, mn_ts)
            if mx_ts is not None:
                in_max_ts = mx_ts if in_max_ts is None else max(in_max_ts, mx_ts)

        # compute mean latency for this sender (across all receivers)
        if sender_lvals:
            try:
                mean_sender_lat = float(np.mean(sender_lvals))
            except Exception as e:
                print(f'WARNING: Failed to compute mean sender latency: {e}')
                mean_sender_lat = None
            sender_folder = None
            if cname in local_by_cname:
                sender_folder = local_by_cname.get(cname, {}).get('client_folder')
            sender_num = _extract_client_num_from_folder(sender_folder)
            if mean_sender_lat is not None:
                latency_per_sender.append({'client_folder': sender_folder, 'client_num': sender_num, 'mean_latency_ms': mean_sender_lat})

    # compute outgoing/incoming average bitrates (bps) using collected bytes and timestamp ranges
    # Prefer using the run metadata window (Start_Timestamp..End_timestamp) to avoid including
    # setup/teardown intervals in the duration calculation. Fall back to trace-derived min/max
    # timestamps when metadata is not available.
    out_bps = None
    try:
        if out_total_bytes:
            if start_ts is not None and end_ts is not None and end_ts > start_ts:
                dur_s = (end_ts - start_ts) / 1000.0
                out_bps = (out_total_bytes * 8.0) / dur_s if dur_s > 0 else None
            elif out_min_ts is not None and out_max_ts is not None and out_max_ts > out_min_ts:
                dur_s = (out_max_ts - out_min_ts) / 1000.0
                out_bps = (out_total_bytes * 8.0) / dur_s if dur_s > 0 else None
    except Exception as e:
        print(f'WARNING: Failed to calculate outgoing bitrate: {e}')
        out_bps = None

    in_bps = None
    try:
        if in_total_bytes:
            if start_ts is not None and end_ts is not None and end_ts > start_ts:
                dur_s = (end_ts - start_ts) / 1000.0
                in_bps = (in_total_bytes * 8.0) / dur_s if dur_s > 0 else None
            elif in_min_ts is not None and in_max_ts is not None and in_max_ts > in_min_ts:
                dur_s = (in_max_ts - in_min_ts) / 1000.0
                in_bps = (in_total_bytes * 8.0) / dur_s if dur_s > 0 else None
    except Exception as e:
        print(f'WARNING: Failed to calculate incoming bitrate: {e}')
        in_bps = None
    metrics['outgoing_bps'] = out_bps
    metrics['incoming_bps'] = in_bps
    metrics['avg_latency_ms'] = float(np.mean(latencies)) if latencies else None
    metrics['avg_decode_ms'] = float(np.mean(decode_times)) if decode_times else None
    metrics['avg_part_frame_size'] = float(np.mean(participant_sizes)) if participant_sizes else None

    # attach per-client maxima for encode/decode (keys may be None if folder name lacks digits)
    metrics['max_encode_by_client'] = max_encode_by_client
    metrics['max_decode_by_client'] = max_decode_by_client

    # Missing frame detection delegated to helper function for clarity
    # Pass the metadata start/end timestamps so matching only considers local
    # frames captured during the run interval. Participant traces remain unfiltered.
    missing_summary = detect_missing_frames(local_by_cname, participant_by_cname,
                                            start_ts=start_ts, end_ts=end_ts)
    metrics['missing_summary'] = missing_summary

    # Parse measured bandwidth using helper (separate client vs host). Pass
    # both client and host folders so host metrics are still captured.
    c_out_bps_meas, c_in_bps_meas, h_out_bps_meas, h_in_bps_meas, per_client_bandwidth = parse_measured_bandwidth(bandwidth_folders, start_ts, end_ts)
    metrics['measured_outgoing_bps_per_client'] = c_out_bps_meas
    metrics['measured_incoming_bps_per_client'] = c_in_bps_meas
    metrics['measured_host_outgoing_bps'] = h_out_bps_meas
    metrics['measured_host_incoming_bps'] = h_in_bps_meas
    metrics['measured_outgoing_bps_per_client_list'] = per_client_bandwidth
    # attach per-sender latency means for speaker vs listeners analysis
    metrics['latency_per_sender'] = latency_per_sender

    return metrics


def plot_cpu(results_by_arch, analysis_folder, scenario):
    # results_by_arch: dict[arch] -> list of (n_clients, cpu_avg)
    plt.figure(figsize=FIGSIZE)
    cmap = get_color_map(results_by_arch.keys())
    markers = ['o', 's', '^', 'D', 'v', 'P', 'X']
    for i, (arch, rows) in enumerate(sorted(results_by_arch.items())):
        rows_sorted = sorted(rows, key=lambda x: x[0])
        xs = [r[0] for r in rows_sorted]
        ys = [r[1] for r in rows_sorted]
        # coerce to numpy arrays and handle missing values
        xs_arr = np.array(xs, dtype=float)
        ys_arr = np.array(ys, dtype=float)
        # plot only where y is finite
        mask = ~np.isnan(ys_arr)
        if np.any(mask):
            m = markers[i % len(markers)]
            ls = '-' if i % 2 == 0 else '--'
            # use default color cycle (will be set externally if desired)
            plt.plot(xs_arr[mask], ys_arr[mask], marker=m, linestyle=ls, label=arch, color=cmap[arch])
    plt.xlabel(GRAPH_NUM_CLIENT_LABEL)
    plt.ylabel('Average Total CPU %')
    #plt.title(f'CPU usage - {scenario}')
    # nicer grid: horizontal lines only
    plt.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
    ncol = len(results_by_arch) if len(results_by_arch) <= 3 else math.ceil(len(results_by_arch) / 2)
    plt.legend(loc="lower center", bbox_to_anchor=(0.5, -0.3),  ncol=ncol, prop={'size': 10})
    # enforce y-axis 0-100 and ticks every 10%
    #plt.ylim(0, 100)
    #plt.yticks(np.arange(0, 101, 10))
    # x-axis should be whole numbers (participants)
    all_x = []
    for rows in results_by_arch.values():
        all_x.extend([r[0] for r in rows])
    if all_x:
        xticks = sorted(set(int(x) for x in all_x if x is not None))
        plt.xticks(xticks)
    plt.tight_layout()
    out = os.path.join(analysis_folder, 'cpu.svg')
    plt.savefig(out)
    plt.close()
    print('Saved CPU plot:', out)


def parse_measured_bandwidth(client_folders, start_ts=None, end_ts=None):
    """Parse per-container bandwidth.csv files and return per-client mean (out_bps, in_bps).

    Returns tuple (measured_outgoing_bps_per_client, measured_incoming_bps_per_client) in bps
    or (None, None) if no data.
    """
    measured_out_clients = []
    measured_in_clients = []
    measured_out_host = []
    measured_in_host = []
    per_client_bandwidth = []
    try:
        for cfolder in client_folders:
            bw_path = os.path.join(cfolder, 'bandwidth.csv')
            if not os.path.isfile(bw_path):
                continue
            df = read_csv_guess(bw_path)
            if df is None:
                continue

            # Filter by run window if possible
            df = filter_df_by_ts(df, start_ts, end_ts)

            cols_lc = [c.lower() for c in df.columns]
            rx_bps_col = None
            tx_bps_col = None
            rx_bytes_col = None
            tx_bytes_col = None
            for i, c in enumerate(cols_lc):
                if 'rx_bps' in c or 'rxps' in c or 'rx_bytes/s' in c or 'rx_b/s' in c:
                    rx_bps_col = df.columns[i]
                if 'tx_bps' in c or 'txps' in c or 'tx_bytes/s' in c or 'tx_b/s' in c:
                    tx_bps_col = df.columns[i]
                if 'rx_bytes' in c:
                    rx_bytes_col = df.columns[i]
                if 'tx_bytes' in c:
                    tx_bytes_col = df.columns[i]

            tscol = find_timestamp_column(df)
            duration_s = None
            if tscol is not None:
                try:
                    ts = pd.to_numeric(df[tscol], errors='coerce').dropna()
                    if len(ts) >= 2:
                        interval_ms = float(ts.iloc[-1] - ts.iloc[0])
                        if interval_ms > 0:
                            duration_s = interval_ms / 1000.0
                    if duration_s is not None:
                        duration_s = max(duration_s, 0.001)
                except Exception as e:
                    print(f'WARNING: Failed to calculate bandwidth interval duration: {e}')
                    duration_s = None

            def _rate_from_bytes(col_name):
                if col_name is None or duration_s is None:
                    return None
                vals = pd.to_numeric(df[col_name], errors='coerce').dropna()
                if len(vals) < 2:
                    return None
                return float((vals.iloc[-1] - vals.iloc[0]) / duration_s) * 8.0

            rx_mean = _rate_from_bytes(rx_bytes_col)
            tx_mean = _rate_from_bytes(tx_bytes_col)

            if rx_mean is None and rx_bps_col is not None:
                try:
                    rx_mean = float(pd.to_numeric(df[rx_bps_col], errors='coerce').dropna().mean()) * 8.0
                except Exception as e:
                    print(f'WARNING: Failed to compute rx mean bandwidth: {e}')
                    rx_mean = None
            if tx_mean is None and tx_bps_col is not None:
                try:
                    tx_mean = float(pd.to_numeric(df[tx_bps_col], errors='coerce').dropna().mean()) * 8.0
                except Exception as e:
                    print(f'WARNING: Failed to compute tx mean bandwidth: {e}')
                    tx_mean = None

            # classify folder: treat explicit host folder separately
            base = os.path.basename(cfolder)
            is_host = (base == 'uvgcomm-host' or 'host' in base.lower())
            if is_host:
                if rx_mean is not None:
                    measured_in_host.append(rx_mean)
                if tx_mean is not None:
                    measured_out_host.append(tx_mean)
            else:
                if rx_mean is not None:
                    measured_in_clients.append(rx_mean)
                if tx_mean is not None:
                    measured_out_clients.append(tx_mean)
                # record per-client measured values for later first-vs-others analysis
                try:
                    client_num = _extract_client_num_from_folder(cfolder)
                except Exception as e:
                    print(f'WARNING: Failed to extract client number from folder {cfolder}: {e}')
                    client_num = None
                per_client_bandwidth.append({'client_folder': cfolder, 'client_num': client_num, 'tx_bps': tx_mean, 'rx_bps': rx_mean})
    except Exception as e:
        print(f'WARNING: Failed to parse measured bandwidth data: {e}')

    c_out_mean = float(np.mean(measured_out_clients)) if measured_out_clients else None
    c_in_mean = float(np.mean(measured_in_clients)) if measured_in_clients else None
    h_out_mean = float(np.mean(measured_out_host)) if measured_out_host else None
    h_in_mean = float(np.mean(measured_in_host)) if measured_in_host else None
    return c_out_mean, c_in_mean, h_out_mean, h_in_mean, per_client_bandwidth


def plot_psnr(mean_df, std_df, analysis_folder, scenario):
    plt.figure(figsize=FIGSIZE)
    cmap = get_color_map(mean_df.columns.unique())
    markers = ['o', 's', '^', 'D', 'v', 'P', 'X', '*']
    for i, col in enumerate(sorted(mean_df.columns)):
        plt.plot(mean_df.index, mean_df[col], marker=markers[i % len(markers)], label=col, color=cmap.get(col))
        if col in std_df.columns:
            std = std_df[col].fillna(0)
            plt.fill_between(mean_df.index, mean_df[col] - std, mean_df[col] + std, alpha=0.15)
    plt.xlabel(GRAPH_NUM_CLIENT_LABEL)
    plt.ylabel('Average PSNR (Y)')
    #plt.title(f'Average PSNR - {scenario}')
    plt.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
    # set x-axis to whole numbers
    try:
        xt = [int(x) for x in mean_df.index]
        plt.xticks(xt)
    except Exception as e:
        print(f'WARNING: Failed to set x-axis ticks for PSNR plot: {e}')
    # Force y-limits to 0..50 and add horizontal 8-bit max line before legend so it's shown
    plt.ylim(0, 50)
    plt.axhline(48.131, color='gray', linestyle=':', linewidth=2.0, label='Max PSNR (8-bit)', zorder=5)
    ncol = len(mean_df.columns)+1
    plt.legend(loc="lower center", bbox_to_anchor=(0.5, -0.4),  ncol=ncol, prop={'size': 10})
    plt.tight_layout()
    out = os.path.join(analysis_folder, 'psnr.svg')
    plt.savefig(out)
    plt.close()
    print('Saved PSNR plot:', out)


def plot_psnr_speaker_vs_listeners(psnr_speaker_stats, psnr_listeners_stats, analysis_folder, scenario):
    """Plot mean PSNR for speaker client vs average of other visible clients."""
    try:
        mean_speaker, std_speaker = build_psnr_dfs(psnr_speaker_stats)
        mean_listeners, std_listeners = build_psnr_dfs(psnr_listeners_stats)
        plt.figure(figsize=FIGSIZE)
        markers = ['o', 's', '^', 'D', 'v', 'P', 'X']
        all_archs = sorted(set(list(mean_speaker.columns) + list(mean_listeners.columns)))
        cmap = get_color_map(all_archs)
        for i, arch in enumerate(all_archs):
            color = cmap.get(arch)
            mk = markers[i % len(markers)]
            # first: solid line, filled marker
            if arch in mean_speaker.columns:
                plt.plot(mean_speaker.index, mean_speaker[arch], marker=mk, markersize=8, linewidth=2.0, markeredgewidth=1.4, linestyle='-', label=f'{arch} (speaker)', color=color)
                if arch in std_speaker.columns:
                    plt.fill_between(mean_speaker.index, mean_speaker[arch] - std_speaker[arch], mean_speaker[arch] + std_speaker[arch], alpha=0.16, color=color)
            # others: dashed line, open marker
            if arch in mean_listeners.columns:
                plt.plot(mean_listeners.index, mean_listeners[arch], marker=mk, markersize=8, linewidth=2.0, markeredgewidth=1.4, markerfacecolor='none', linestyle='--', label=f'{arch} (listeners)', color=color)
                if arch in std_listeners.columns:
                    plt.fill_between(mean_listeners.index, mean_listeners[arch] - std_listeners[arch], mean_listeners[arch] + std_listeners[arch], alpha=0.10, color=color)
        plt.xlabel(GRAPH_NUM_CLIENT_LABEL)
        plt.ylabel('PSNR (Y)')
        #plt.title(f'PSNR: Speaker vs Listeners - {scenario}')
        plt.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
        plt.ylim(0, 50)
        plt.axhline(48.131, color='gray', linestyle=':', linewidth=2.0, label='Max PSNR (8-bit)', zorder=5)
        ncol = len(all_archs) if len(all_archs) <= 3 else math.ceil(len(all_archs) / 2)
        plt.legend(loc="lower center", bbox_to_anchor=(0.5, -0.3),  ncol=ncol, prop={'size': 9})
        # force x-axis to whole numbers (participants)
        try:
            idx = sorted(set(list(mean_speaker.index) + list(mean_listeners.index)))
            xt = [int(x) for x in idx]
            plt.xticks(xt)
        except Exception as e:
            print(f'WARNING: Failed to set x-axis ticks for PSNR speaker-vs-listeners plot: {e}')
        plt.tight_layout()
        out = os.path.join(analysis_folder, 'psnr_speaker_vs_listeners.svg')
        plt.savefig(out)
        plt.close()
        print('Saved PSNR speaker-vs-listeners plot:', out)
    except Exception as e:
        print('Failed to create PSNR speaker-vs-listeners plot:', e)

def plot_measured_bandwidth_speaker_vs_listeners(measured_speaker_stats, measured_listeners_stats, analysis_folder, scenario):
    """Plot measured outgoing bandwidth (Mbps) for speaker client vs avg of others."""
    try:
        # build participants idx
        idx = sorted({n for arch in measured_speaker_stats for n in measured_speaker_stats[arch]} | {n for arch in measured_listeners_stats for n in measured_listeners_stats[arch]})
        if not idx:
            return
        fig, ax = plt.subplots(figsize=FIGSIZE)
        markers = ['o', 's', '^', 'D', 'v', 'P', 'X']
        cmap = get_color_map(sorted(set(list(measured_speaker_stats.keys()) + list(measured_listeners_stats.keys()))))
        for i, arch in enumerate(sorted(set(list(measured_speaker_stats.keys()) + list(measured_listeners_stats.keys())))):
            speaker_means = []
            listeners_means = []
            for n in idx:
                fvals = measured_speaker_stats.get(arch, {}).get(n, [])
                rvals = measured_listeners_stats.get(arch, {}).get(n, [])
                speaker_means.append(float(np.mean(fvals))/1e6 if fvals else np.nan)
                listeners_means.append(float(np.mean(rvals))/1e6 if rvals else np.nan)
            x = idx
            color = cmap.get(arch)
            mk = markers[i % len(markers)]
            # speaker: solid, filled marker
            ax.plot(x, speaker_means, marker=mk, markersize=7, linewidth=2.0, markeredgewidth=1.2, linestyle='-', label=f'{arch} (speaker)', color=color)
            # listeners: dashed, open marker
            ax.plot(x, listeners_means, marker=mk, markersize=7, linewidth=2.0, markeredgewidth=1.2, markerfacecolor='none', linestyle='--', label=f'{arch} (listeners)', color=color)
        ax.set_xlabel(GRAPH_NUM_CLIENT_LABEL)
        ax.set_ylabel('Measured Outgoing Bandwidth (Mbps)')
        #ax.set_title(f'Measured Bandwidth: Speaker vs Listeners - {scenario}')
        ax.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
        try:
            ax.set_xticks(idx)
        except Exception as e:
            print(f'WARNING: Failed to set x-axis ticks for measured bandwidth plot: {e}')
        ncol = len(cmap) if len(cmap) <= 3 else math.ceil(len(cmap) / 2)
        ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.3),  ncol=ncol, fontsize=8)
        plt.tight_layout()
        out = os.path.join(analysis_folder, 'measured_bandwidth_speaker_vs_listeners.svg')
        fig.savefig(out)
        plt.close(fig)
        print('Saved measured bandwidth speaker-vs-listeners plot:', out)
    except Exception as e:
        print('Failed to create measured bandwidth speaker-vs-listeners plot:', e)


def plot_latency_speaker_vs_listeners(latency_speaker_stats, latency_listeners_stats, analysis_folder, scenario):
    """Plot mean latency (ms) for speaker client vs average of other visible clients."""
    try:
        # reuse the build_psnr_dfs helper to construct mean/std DataFrames
        mean_speaker, std_speaker = build_psnr_dfs(latency_speaker_stats)
        mean_listeners, std_listeners = build_psnr_dfs(latency_listeners_stats)
        plt.figure(figsize=FIGSIZE)
        markers = ['o', 's', '^', 'D', 'v', 'P', 'X']
        all_archs = sorted(set(list(mean_speaker.columns) + list(mean_listeners.columns)))
        cmap = get_color_map(all_archs)
        for i, arch in enumerate(all_archs):
            color = cmap.get(arch)
            mk = markers[i % len(markers)]
            # speaker: solid line, filled marker
            if arch in mean_speaker.columns:
                plt.plot(mean_speaker.index, mean_speaker[arch], marker=mk, markersize=8, linewidth=2.0, markeredgewidth=1.4, linestyle='-', label=f'{arch} (speaker)', color=color)
                if arch in std_speaker.columns:
                    plt.fill_between(mean_speaker.index, mean_speaker[arch] - std_speaker[arch], mean_speaker[arch] + std_speaker[arch], alpha=0.16, color=color)
            # listeners: dashed line, open marker
            if arch in mean_listeners.columns:
                plt.plot(mean_listeners.index, mean_listeners[arch], marker=mk, markersize=8, linewidth=2.0, markeredgewidth=1.4, markerfacecolor='none', linestyle='--', label=f'{arch} (listeners)', color=color)
                if arch in std_listeners.columns:
                    plt.fill_between(mean_listeners.index, mean_listeners[arch] - std_listeners[arch], mean_listeners[arch] + std_listeners[arch], alpha=0.10, color=color)
        plt.xlabel(GRAPH_NUM_CLIENT_LABEL)
        plt.ylabel('Mean Total Latency (ms)')
        #plt.title(f'Latency: Speaker vs Listeners - {scenario}')
        plt.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
        # compute automatic upper bound from mean+std and force y-axis to start at 0
        try:
            combined = (mean_speaker.fillna(0) + std_speaker.fillna(0)).values
            combined2 = (mean_listeners.fillna(0) + std_listeners.fillna(0)).values
            max_val1 = float(np.nanmax(combined)) if combined.size else None
            max_val2 = float(np.nanmax(combined2)) if combined2.size else None
            max_val = None
            if max_val1 is not None and max_val2 is not None:
                max_val = max(max_val1, max_val2)
            elif max_val1 is not None:
                max_val = max_val1
            elif max_val2 is not None:
                max_val = max_val2
        except Exception as e:
            print(f'WARNING: Failed to compute automatic y-axis bounds: {e}')
            max_val = None
        if max_val is not None and max_val > 0:
            plt.ylim(0, max(max_val * 1.05, 1.0))
        else:
            plt.ylim(0, 1)
        ncol = len(all_archs) if len(all_archs) <= 3 else math.ceil(len(all_archs) / 2)
        plt.legend(loc="lower center", bbox_to_anchor=(0.5, -0.3),  ncol=ncol, prop={'size': 9})
        # force integer x-ticks
        try:
            idx = sorted(set(list(mean_speaker.index) + list(mean_listeners.index)))
            xt = [int(x) for x in idx]
            plt.xticks(xt)
        except Exception as e:
            print(f'WARNING: Failed to set x-axis ticks for latency speaker-vs-listeners plot: {e}')
        plt.tight_layout()
        out = os.path.join(analysis_folder, 'latency_speaker_vs_listener.svg')
        plt.savefig(out)
        plt.close()
        print('Saved latency speaker-vs-listener plot:', out)
    except Exception as e:
        print('Failed to create latency speaker-vs-listeners plot:', e)


def build_arch_map(scenario_folder):
    """Detect architecture-client folders and pick the latest run if present.

    Returns a dict mapping arch -> list of (nclients, run_path).
    """
    arch_folders = [p for p in glob.glob(os.path.join(scenario_folder, '*')) if os.path.isdir(p)]
    arch_map = defaultdict(list)
    for af in arch_folders:
        base = os.path.basename(af)
        if '-' not in base:
            continue
        try:
            arch, clients = base.rsplit('-', 1)
            n = int(clients)
        except Exception as e:
            print(f'WARNING: Failed to parse architecture folder {base}: {e}')
            continue
        # include all inner run_* directories if present, otherwise the folder itself
        runs = sorted([p for p in glob.glob(os.path.join(af, 'run_*')) if os.path.isdir(p)])
        if runs:
            for r in runs:
                arch_map[arch].append((n, r))
        else:
            arch_map[arch].append((n, af))
    return arch_map


def get_color_map(keys):
    """Return a dict mapping each key to a color from matplotlib's default cycle."""
    prop_cycle = plt.rcParams['axes.prop_cycle'].by_key().get('color', None)
    if not prop_cycle:
        prop_cycle = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b']
    cmap = {}
    for i, k in enumerate(sorted(keys)):
        # Prefer canonical ARCH_COLOR_MAP entries for well-known architectures.
        try:
            if isinstance(k, str):
                kn = k.strip().lower()
                if 'p2p' in kn:
                    cmap[k] = ARCH_COLOR_MAP.get('p2p')
                    continue
                if 'sfu' in kn:
                    cmap[k] = ARCH_COLOR_MAP.get('sfu')
                    continue
                if 'hybrid' in kn:
                    cmap[k] = ARCH_COLOR_MAP.get('hybrid')
                    continue
        except Exception as e:
            print(f'WARNING: Failed to map color for architecture {k}: {e}')
        # Fallback to matplotlib cycle for unknown keys
        cmap[k] = prop_cycle[i % len(prop_cycle)]
    return cmap


def _extract_client_num_from_folder(folder_path):
    """Helper to extract numeric client id from a folder name like uvgcomm-3. Falls back to None."""
    if not folder_path:
        return None
    base = os.path.basename(folder_path)
    nums = ''.join([c for c in base if c.isdigit()])
    try:
        return int(nums) if nums else None
    except Exception as e:
        print(f'WARNING: Failed to extract client number from folder {folder_path}: {e}')
        return None


def _diagnostics_sort_key(r):
    """Sort key for diagnostics rows.

    Orders by architecture priority (P2P, SFU, Hybrid), numeric participants,
    numeric run index (extracted from RunPath basename), and numeric client id.
    """
    arch = r.get('Architecture') or r.get('arch') or ''
    arch_norm = str(arch).strip().lower()
    arch_prio_map = {'p2p': 0, 'sfu': 1, 'hybrid': 2}
    arch_idx = arch_prio_map.get(arch_norm, 99)

    parts = r.get('Participants') if 'Participants' in r else r.get('participants')
    try:
        parts_i = int(parts) if parts is not None else 999999
    except Exception as e:
        print(f'WARNING: Failed to convert participants to int for sort: {e}')
        parts_i = 999999

    runp = r.get('RunPath') or r.get('run_path') or ''
    run_base = os.path.basename(runp) if runp else ''
    digits = ''.join([c for c in run_base if c.isdigit()])
    try:
        run_i = int(digits) if digits else (0 if run_base else 999999)
    except Exception as e:
        print(f'WARNING: Failed to extract run number for sort: {e}')
        run_i = 999999

    client = r.get('Client') if 'Client' in r else r.get('client')
    try:
        client_i = int(client) if client is not None else 999999
    except Exception as e:
        print(f'WARNING: Failed to convert client to int for sort: {e}')
        client_i = 999999

    return (arch_idx, parts_i, run_i, client_i)


def _client_crash_reason(run_path, client_num):
    """Try to find a docker.log for the given client number and return a short
    crash reason string if a crash / non-zero exit appears in the log.
    Returns None if no evident crash is found.
    """
    try:
        client_folders = sorted([p for p in glob.glob(os.path.join(run_path, 'uvgcomm-client*')) if os.path.isdir(p)])
        for cf in client_folders:
            num = _extract_client_num_from_folder(cf)
            if num is None:
                continue
            if int(num) != int(client_num):
                continue
            log_path = os.path.join(cf, 'docker.log')
            if not os.path.isfile(log_path):
                continue
            try:
                with open(log_path, 'r', errors='ignore') as f:
                    # read tail to keep memory use small for large logs
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    tail_size = min(32768, size)
                    f.seek(max(0, size - tail_size))
                    tail = f.read()
            except Exception as e:
                print(f'WARNING: Failed to read docker.log for client {client_num}: {e}')
                continue
            tail_lc = tail.lower()
            if 'segmentation fault' in tail_lc or 'core dumped' in tail_lc:
                return 'segmentation fault'
            # docker exit codes are often printed as 'exited with code <n>'
            import re
            m = re.search(r'exited with code\s*(\d+)', tail_lc)
            if m:
                code = int(m.group(1))
                return f'exited code {code}'
            # generic non-zero exit hints
            if 'exited' in tail_lc and 'code' in tail_lc:
                return 'exited'
    except Exception as e:
        print(f'WARNING: Failed to detect client crash reason: {e}')
        pass
    return None


def write_diagnostics(presence_records, missing_records, analysis_folder):
    """Write per-run diagnostics CSVs with one row per run+client.

    Writes one CSV per architecture under `analysis_folder` and omits the
    Architecture column from those outputs.

    Status is one of: OK, missing frames, broken
    """
    pres_df = pd.DataFrame(presence_records)
    miss_df = pd.DataFrame(missing_records)

    rows = []
    # Build a lookup for frames lost per sender (arch, participants, run_path, sender_client_num)
    miss_lookup = {}
    missing_partners = defaultdict(set)
    if not miss_df.empty:
        if 'sender_client_num' not in miss_df.columns:
            miss_df['sender_client_num'] = miss_df.get('local_folder', '').apply(
                lambda x: _extract_client_num_from_folder(x))
        if 'receiver_client_num' not in miss_df.columns:
            miss_df['receiver_client_num'] = miss_df.get('receiver_folder', '').apply(
                lambda x: _extract_client_num_from_folder(x))
        for _, r in miss_df.iterrows():
            sender = r.get('sender_client_num')
            if sender is None:
                continue
            key = (r.get('arch'), r.get('participants'), r.get('run_path'), sender)
            missing = int(r.get('missing') or 0)
            analyzed = int(r.get('total_local_frames') or 0)
            entry = miss_lookup.setdefault(key, {'missing': 0, 'analyzed_frames': 0})
            entry['missing'] += missing
            if analyzed and not entry['analyzed_frames']:
                entry['analyzed_frames'] = analyzed
            if missing > 0:
                receiver = r.get('receiver_client_num')
                if receiver is not None:
                    missing_partners[key].add(receiver)

    # For every presence record (per run/client) create a diagnostics row
    if not pres_df.empty:
        for _, p in pres_df.iterrows():
            arch = p.get('arch')
            parts = p.get('participants')
            runp = p.get('run_path')
            client = p.get('client_num')
            localc = int(p.get('local_count') or 0)
            partc = int(p.get('part_count') or 0)
            key = (arch, parts, runp, client)
            mk = miss_lookup.get(key, {'missing': 0, 'analyzed_frames': 0})
            frames_lost = mk.get('missing', 0)
            analyzed = mk.get('analyzed_frames', 0)
            partners = sorted(missing_partners.get(key, []))
            partners_str = ','.join(str(int(p)) for p in partners) if partners else ''
            if localc == 0 and partc == 0:
                # If both traces are missing, try to detect if the client container
                # crashed (segfault / non-zero exit) and prefer a more informative
                # status string for diagnostics.
                crash = None
                try:
                    crash = _client_crash_reason(runp, client)
                except Exception:
                    crash = None
                if crash:
                    status = f'crashed ({crash})'
                else:
                    status = 'broken'
            elif frames_lost > 0:
                status = 'missing frames'
            else:
                status = 'OK'
            # run number extraction from RunPath basename (digits if present)
            run_base = os.path.basename(runp) if runp else ''
            run_digits = ''.join([c for c in run_base if c.isdigit()])
            run_number = int(run_digits) if run_digits else None
            local_yesno = 'yes' if localc else 'no'
            rows.append({'RunPath': runp, 'Architecture': arch, 'Participants': parts, 'Run': run_number,
                         'Client': client, 'Local results': local_yesno, 'Participant results': partc,
                         'Analyzed Frames': analyzed, 'Frames Lost': frames_lost, 'Missing Participants': partners_str,
                         'Max Encode (ms)': p.get('max_encode_ms'), 'Max Decode (ms)': p.get('max_decode_ms'),
                         'Status': status})
    else:
        # No presence records: still write a row per missing record (or one OK row)
        if miss_lookup:
            for (arch, parts, runp, client), val in miss_lookup.items():
                frames_lost = val.get('missing', 0)
                partners = sorted(missing_partners.get((arch, parts, runp, client), []))
                partners_str = ','.join(str(int(p)) for p in partners) if partners else ''
                # run number extraction from RunPath basename (digits if present)
                run_base = os.path.basename(runp) if runp else ''
                run_digits = ''.join([c for c in run_base if c.isdigit()])
                run_number = int(run_digits) if run_digits else None
                rows.append({'RunPath': runp, 'Architecture': arch, 'Participants': parts, 'Run': run_number,
                             'Client': client, 'Local results': 'no', 'Participant results': 0,
                             'Analyzed Frames': int(val.get('analyzed_frames', 0)),
                             'Frames Lost': frames_lost, 'Missing Participants': partners_str,
                             'Max Encode (ms)': None, 'Max Decode (ms)': None,
                             'Status': ('missing frames' if frames_lost > 0 else 'OK')})
        else:
            rows.append({'RunPath': None, 'Architecture': None, 'Participants': None, 'Run': None,
                         'Client': None, 'Local results': 0, 'Participant results': 0,
                         'Analyzed Frames': 0, 'Frames Lost': 0, 'Missing Participants': '', 
                         'Max Encode (ms)': None, 'Max Decode (ms)': None, 'Status': 'OK'})

    # Notify user that sorting is starting, then sort rows using a module-level helper.
    print(f"Sorting diagnostics {len(rows)} rows...")
    rows = sorted(rows, key=_diagnostics_sort_key)

    cols = ['RunPath', 'Architecture', 'Participants', 'Run', 'Client',
            'Local results', 'Participant results', 'Analyzed Frames', 'Frames Lost', 'Missing Participants',
            'Max Encode (ms)', 'Max Decode (ms)', 'Status']
    diag_df = pd.DataFrame(rows, columns=cols)

    # Split into one CSV per architecture and drop the Architecture column.
    if diag_df.empty:
        return

    diag_df['Architecture'] = diag_df['Architecture'].fillna('unknown')
    for arch, g in diag_df.groupby('Architecture', dropna=False):
        out_cols = [c for c in cols if c != 'Architecture']
        out_df = g[out_cols].copy()
        out_df = out_df.sort_values(by=['Participants', 'Run', 'Client'], kind='mergesort')
        diag_csv = os.path.join(analysis_folder, f'diagnostics_summary_{arch}.csv')
        out_df.to_csv(diag_csv, index=False, sep=';')
        print('Wrote diagnostics summary to', diag_csv)


def setup_analysis_folders(ROOT_FOLDER, scenario):
    """Create and return the per-scenario analysis folder path."""
    base_analysis = os.path.join(ROOT_FOLDER, 'analysis')
    ensure_dir(base_analysis)
    scenario_analysis = os.path.join(base_analysis, scenario)
    ensure_dir(scenario_analysis)
    return scenario_analysis


def collect_presence_records_for_run(run_path, arch, participants):
    """Return a tuple (filtered_records, unfiltered_records) of presence record dicts for given run_path.

    The filtered list respects any `Visible_*` limit in metadata and is used for analysis.
    The unfiltered list contains all client folders and is intended for diagnostics output.
    `participants` is the number of participants for this run.
    """
    # gather all client folders first (unfiltered)
    client_folders_all = sorted([p for p in glob.glob(os.path.join(run_path, 'uvgcomm-client*')) if os.path.isdir(p)])

    # determine visible limit (may be None)
    visible_limit = None
    try:
        meta = parse_metadata(os.path.join(run_path, 'metadata.txt'))
        for vk in ('Visible_Participants', 'VisibleParticipants', 'Visible', 'VisibleCount'):
            if vk in meta:
                try:
                    visible_limit = int(meta[vk])
                except Exception as e:
                    print(f'WARNING: Failed to parse visible_limit from {vk}={meta[vk]}: {e}')
                    visible_limit = None
                break
    except Exception as e:
        print(f'WARNING: Failed to read metadata for visible limit: {e}')
        visible_limit = None

    # produce filtered client folders according to visible_limit
    client_folders_filtered = client_folders_all
    if visible_limit is not None:
        def _keep(folder):
            try:
                num = _extract_client_num_from_folder(folder)
                return (num is None) or (num <= visible_limit)
            except Exception as e:
                print(f'WARNING: Failed to check visible limit for folder {folder}: {e}')
                return True
        client_folders_filtered = [c for c in client_folders_all if _keep(c)]

    def _build_records(client_folders):
        recs = []
        for idx, cf in enumerate(client_folders, start=1):
            client_num = _extract_client_num_from_folder(cf) or idx
            local_paths = glob.glob(os.path.join(cf, 'local_*.csv'))
            part_paths = glob.glob(os.path.join(cf, 'participant_*.csv'))
            local_present = bool(local_paths)
            part_present = bool(part_paths)
            local_count = len(local_paths)
            part_count = len(part_paths)
            local_valid = False
            part_valid = False
            if local_present:
                try:
                    local_df = read_csv_guess(local_paths[0])
                    local_valid = local_df is not None
                except Exception as e:
                    print(f'WARNING: Failed to read local CSV {local_paths[0]}: {e}')
                    local_valid = False
            if part_present:
                try:
                    part_df = read_csv_guess(part_paths[0])
                    part_valid = part_df is not None
                except Exception as e:
                    print(f'WARNING: Failed to read participant CSV {part_paths[0]}: {e}')
                    part_valid = False
            # compute single-letter code similar to previous implementation
            code = None
            if local_valid and part_valid:
                code = 'B'
            elif local_valid and not part_present:
                code = 'L'
            elif part_valid and not local_present:
                code = 'P'
            elif not local_present and not part_present:
                code = 'M'
            else:
                code = '-'
            recs.append({'arch': arch, 'participants': participants, 'client_num': client_num,
                         'client_folder': cf,
                         'local_present': local_present, 'part_present': part_present,
                         'local_valid': local_valid, 'part_valid': part_valid,
                         'local_count': local_count, 'part_count': part_count,
                         'code': code, 'run_path': run_path})
        return recs

    filtered_records = _build_records(client_folders_filtered)
    unfiltered_records = _build_records(client_folders_all)
    return filtered_records, unfiltered_records


def accumulate_run_results(metrics, arch, participants, run_path,
                           cpu_results, psnr_stats, missing_rows, resolution_rows, latency_rows, measured_rows,
                           measured_speaker_stats, measured_listeners_stats, psnr_speaker_stats, psnr_listeners_stats,
                           latency_speaker_stats=None, latency_listeners_stats=None,
                           client_max_rows=None):
    """Accumulate per-run metrics into the provided containers (mutates lists/dicts).

    Mirrors the original inlined logic.
    """
    # Use visible participants limit when present so aggregated rows/plots
    # reflect only the visible senders. Fall back to the provided participants value.
    parts_eff = metrics.get('visible_participants', participants)
    cpu_results[arch].append((parts_eff, metrics.get('cpu_avg')))

    # PSNR per run - we have avg_psnr and count. Keep mean and std as single-run values.
    psnr_val = metrics.get('avg_psnr')
    if psnr_val is not None:
        if parts_eff not in psnr_stats[arch]:
            psnr_stats[arch][parts_eff] = []
        psnr_stats[arch][parts_eff].append(psnr_val)

    # Speaker-vs-listeners PSNR aggregation (per-client mean values collected in analyze_run)
    try:
        pclients = metrics.get('psnr_per_client') or []
        if pclients:
            # visible filtering: include clients with no numeric id or id <= parts_eff when parts_eff present
            visible = [p for p in pclients if (parts_eff is None) or (p.get('client_num') is None) or (p.get('client_num') <= parts_eff)]
            if visible:
                # select first as speaker client: prefer client_num==1, else lowest numeric, else first entry
                speaker = next((p for p in visible if p.get('client_num') == 1), None)
                if speaker is None:
                    numeric = [p for p in visible if p.get('client_num') is not None]
                    if numeric:
                        speaker = min(numeric, key=lambda x: x['client_num'])
                    else:
                        speaker = visible[0]
                listeners = [p for p in visible if p is not speaker]
                speaker_val = float(speaker.get('mean_psnr')) if speaker.get('mean_psnr') is not None else None
                listeners_vals = [float(p.get('mean_psnr')) for p in listeners if p.get('mean_psnr') is not None]
                listeners_mean = float(np.mean(listeners_vals)) if listeners_vals else None
                if parts_eff not in psnr_speaker_stats[arch]:
                    psnr_speaker_stats[arch][parts_eff] = []
                if parts_eff not in psnr_listeners_stats[arch]:
                    psnr_listeners_stats[arch][parts_eff] = []
                if speaker_val is not None:
                    psnr_speaker_stats[arch][parts_eff].append(speaker_val)
                if listeners_mean is not None:
                    psnr_listeners_stats[arch][parts_eff].append(listeners_mean)
    except Exception as e:
        print(f'WARNING: Failed to aggregate speaker-vs-listeners PSNR: {e}')

    # missing frames summary appended (attach client_num inferred from receiver_folder)
    for m in metrics.get('missing_summary', []):
        row = dict(m)
        sender_num = _extract_client_num_from_folder(m.get('local_folder'))
        receiver_num = _extract_client_num_from_folder(m.get('receiver_folder'))
        row.update({'arch': arch, 'participants': parts_eff, 'run_path': run_path,
                'sender_client_num': sender_num, 'receiver_client_num': receiver_num})
        missing_rows.append(row)

    # resolution/frame size + bitrates
    # For the synthetic bandwidth diagnostic plot, we need to normalize by the
    # number of visible participants (senders) when a visible limit is used.
    # Do NOT change the x-axis participant count used elsewhere; only attach
    # this as an extra field for `process_resolution_rows`.
    visible_parts = None
    try:
        vp = metrics.get('visible_participants')
        if vp is None:
            md = metrics.get('metadata') or {}
            for vk in ('Visible_Participants', 'VisibleParticipants', 'Visible', 'VisibleCount'):
                if vk in md and md.get(vk) not in (None, ''):
                    vp = md.get(vk)
                    break
        if vp is not None:
            visible_parts = int(vp)
    except Exception:
        visible_parts = None
    row_rs = {'arch': arch, 'participants': parts_eff, 'avg_width': metrics.get('avg_width'),
              'avg_height': metrics.get('avg_height'), 'avg_frame_size': metrics.get('avg_frame_size'),
              'outgoing_bps': metrics.get('outgoing_bps'), 'incoming_bps': metrics.get('incoming_bps'),
              'visible_participants': visible_parts}
    resolution_rows.append(row_rs)

    # Measured bandwidth collected from per-container monitoring (clients vs host)
    measured_row = {
        'arch': arch,
        'participants': parts_eff,
        'measured_outgoing_bps_clients': metrics.get('measured_outgoing_bps_per_client'),
        'measured_incoming_bps_clients': metrics.get('measured_incoming_bps_per_client'),
        'measured_outgoing_bps_host': metrics.get('measured_host_outgoing_bps'),
        'measured_incoming_bps_host': metrics.get('measured_host_incoming_bps')
    }
    measured_rows.append(measured_row)

    # First-vs-others measured outgoing bandwidth aggregation (per-client tx_bps list)
    try:
        pc_bw = metrics.get('measured_outgoing_bps_per_client_list') or []
        if pc_bw:
            visible_bw = [p for p in pc_bw if (parts_eff is None) or (p.get('client_num') is None) or (p.get('client_num') <= parts_eff)]
            if visible_bw:
                first = next((p for p in visible_bw if p.get('client_num') == 1), None)
                if first is None:
                    numeric = [p for p in visible_bw if p.get('client_num') is not None]
                    if numeric:
                        first = min(numeric, key=lambda x: x['client_num'])
                    else:
                        first = visible_bw[0]
                rest = [p for p in visible_bw if p is not first]
                first_tx = float(first.get('tx_bps')) if first.get('tx_bps') is not None else None
                rest_vals = [float(p.get('tx_bps')) for p in rest if p.get('tx_bps') is not None]
                rest_mean = float(np.mean(rest_vals)) if rest_vals else None
                if parts_eff not in measured_speaker_stats[arch]:
                    measured_speaker_stats[arch][parts_eff] = []
                if parts_eff not in measured_listeners_stats[arch]:
                    measured_listeners_stats[arch][parts_eff] = []
                if first_tx is not None:
                    measured_speaker_stats[arch][parts_eff].append(first_tx)
                if rest_mean is not None:
                    measured_listeners_stats[arch][parts_eff].append(rest_mean)
    except Exception as e:
        print(f'WARNING: Failed to aggregate speaker-vs-listeners measured bandwidth: {e}')

    # Speaker-vs-listeners latency aggregation (per-sender mean latency collected in analyze_run)
    try:
        lclients = metrics.get('latency_per_sender') or []
        if lclients and latency_speaker_stats is not None and latency_listeners_stats is not None:
            visible = [p for p in lclients if (parts_eff is None) or (p.get('client_num') is None) or (p.get('client_num') <= parts_eff)]
            if visible:
                # select speaker: prefer client_num==1, else lowest numeric, else first
                speaker = next((p for p in visible if p.get('client_num') == 1), None)
                if speaker is None:
                    numeric = [p for p in visible if p.get('client_num') is not None]
                    if numeric:
                        speaker = min(numeric, key=lambda x: x['client_num'])
                    else:
                        speaker = visible[0]
                listeners = [p for p in visible if p is not speaker]
                speaker_val = float(speaker.get('mean_latency_ms')) if speaker.get('mean_latency_ms') is not None else None
                listeners_vals = [float(p.get('mean_latency_ms')) for p in listeners if p.get('mean_latency_ms') is not None]
                listeners_mean = float(np.mean(listeners_vals)) if listeners_vals else None
                if parts_eff not in latency_speaker_stats.setdefault(arch, {}):
                    latency_speaker_stats[arch][parts_eff] = []
                if parts_eff not in latency_listeners_stats.setdefault(arch, {}):
                    latency_listeners_stats[arch][parts_eff] = []
                if speaker_val is not None:
                    latency_speaker_stats[arch][parts_eff].append(speaker_val)
                if listeners_mean is not None:
                    latency_listeners_stats[arch][parts_eff].append(listeners_mean)
    except Exception as e:
        print(f'WARNING: Failed to aggregate speaker-vs-listeners latency: {e}')

    # latency/encode/decode
    latency_rows.append({'arch': arch, 'participants': parts_eff,
                         'avg_latency_ms': metrics.get('avg_latency_ms'),
                         'avg_network_latency_ms': metrics.get('avg_network_latency_ms'),
                         'avg_encode_ms': metrics.get('avg_encode_ms'),
                         'avg_decode_ms': metrics.get('avg_decode_ms'),})
    # collect per-client max encode/decode reported by analyze_run (may use None keys)
    try:
        me = metrics.get('max_encode_by_client', {}) or {}
        md = metrics.get('max_decode_by_client', {}) or {}
        # union of client keys
        keys = set(list(me.keys()) + list(md.keys()))
        for k in keys:
            # k is the key used in analyze_run for per-client maxima (now a client_folder path or None)
            client_folder_key = k
            try:
                client_num_val = _extract_client_num_from_folder(k) if isinstance(k, str) else k
            except Exception as e:
                print(f'WARNING: Failed to extract client num from key {k}: {e}')
                client_num_val = None
            client_max_rows.append({'arch': arch, 'participants': participants, 'run_path': run_path,
                                     'client_folder': client_folder_key, 'client_num': client_num_val,
                                     'max_encode_ms': me.get(k), 'max_decode_ms': md.get(k)})
    except Exception as e:
        print(f'WARNING: Failed to collect per-client max encode/decode times: {e}')


def build_psnr_dfs(psnr_stats):
    """Build psnr_mean and psnr_std DataFrames from psnr_stats mapping."""
    idx = sorted({n for arch in psnr_stats for n in psnr_stats[arch]})
    psnr_mean = pd.DataFrame(index=idx)
    psnr_std = pd.DataFrame(index=idx)
    for arch in psnr_stats:
        means = []
        stds = []
        for n in idx:
            vals = psnr_stats[arch].get(n, [])
            if vals:
                means.append(float(np.mean(vals)))
                stds.append(float(np.std(vals)))
            else:
                means.append(np.nan)
                stds.append(np.nan)
        psnr_mean[arch] = means
        psnr_std[arch] = stds
    return psnr_mean, psnr_std


def process_resolution_rows(resolution_rows, ANALYSIS_FOLDER):
    """Write resolution/frame-size CSV and bandwidth plot from resolution_rows."""
    res_df = pd.DataFrame(resolution_rows)
    if res_df.empty:
        return
    # Convert aggregate bps to per-client Mbps for clearer interpretation
    def _bps_to_mbps_per_client(bps, participants):
        try:
            if bps is None:
                return None
            if participants is None or participants == 0:
                return float(bps) / 1e6
            return float(bps) / float(participants) / 1e6
        except Exception as e:
            print(f'WARNING: Failed to convert {bps} bps to Mbps per client ({participants} participants): {e}')
            return None

    out_rows = []
    for _, r in res_df.iterrows():
        parts = int(r.get('participants')) if pd.notna(r.get('participants')) else None
        # IMPORTANT: only fix the y-values for the diagnostic synthetic-bandwidth plot.
        # If the run was executed with a visible participant limit, outgoing/incoming
        # bps are based on that visible subset, so normalize per-client using the
        # visible participant count (senders) instead of total participants.
        div_parts = parts
        try:
            vp = r.get('visible_participants')
            if pd.notna(vp):
                vp_i = int(vp)
                if vp_i > 0:
                    # Use visible participant limit only when it is smaller than
                    # the total participants. This keeps 1..9 correct even if
                    # metadata always records Visible_Participants: 9.
                    if parts is None or parts == 0:
                        div_parts = vp_i
                    else:
                        div_parts = min(int(parts), vp_i)
        except Exception:
            div_parts = parts
        out_mbps = _bps_to_mbps_per_client(r.get('outgoing_bps'), div_parts)
        in_mbps = _bps_to_mbps_per_client(r.get('incoming_bps'), div_parts)
        out_rows.append({
            'Architecture': r.get('arch'),
            'Participants': parts,
            'Avg_Width_px': r.get('avg_width'),
            'Avg_Height_px': r.get('avg_height'),
            'Avg_FrameSize_bytes': r.get('avg_frame_size'),
            # Use per-client Mbps (not aggregate kbps)
            'Outgoing_Mbps_per_client': out_mbps,
            'Incoming_Mbps_per_client': in_mbps
        })
    out_df = pd.DataFrame(out_rows)
    # Mark this CSV as diagnostic because the bandwidth values are estimated
    # from frame sizes and are not always accurate. Keep it for diagnostics.
    res_csv = os.path.join(ANALYSIS_FOLDER, f'diagnostic_resolution_framesize.csv')
    out_df.to_csv(res_csv, index=False, sep=';')
    print('Wrote diagnostic resolution/frame-size summary to', res_csv)

    # Plot outgoing/incoming bandwidth per architecture/participants using
    # the generic measured-bandwidth plotting helper. Mark the plot as
    # diagnostic to indicate estimates may be inaccurate.
    try:
        _plot_measured_bandwidth_generic(out_df,
                                         'Participants', 'Architecture',
                                         ['Outgoing_Mbps_per_client', 'Incoming_Mbps_per_client'],
                                         ['Out', 'In'],
                                         ANALYSIS_FOLDER,
                                         'diagnostic_bandwidth_mbps_per_client.svg',
                                         'Outgoing and Incoming Bandwidth (per-client) (diagnostic)',
                                         'Per-Client Bandwidth (Mbps)')
    except Exception as e:
        print('Failed to create diagnostic bandwidth plot:', e)


def _plot_measured_bandwidth_generic(df, participants_col, arch_col, ycols, labels, ANALYSIS_FOLDER, out_filename, title, ylabel):
    """Generic helper to plot measured bandwidth grouped by architecture and participants.

    df: DataFrame containing at least arch_col, participants_col and the ycols.
    ycols: list of column names to plot (one or more), labels: list of labels for legend suffix.
    out_filename: relative filename to write under ANALYSIS_FOLDER.
    """
    fig, ax = plt.subplots(figsize=FIGSIZE)
    cmap = get_color_map(df[arch_col].unique())
    markers = ['o', 's', '^', 'D', 'v', 'P', 'X', '*']
    groups = df.groupby(arch_col)
    max_val = 0.0

    # linestyles for multiple series per-arch
    linestyles = ['-', '--', ':', '-.']

    for i, (arch_name, g) in enumerate(groups):
        color = cmap.get(arch_name)
        marker = markers[i % len(markers)]
        try:
            gagg_mean = g.groupby(participants_col)[ycols].mean()
            gagg_std = g.groupby(participants_col)[ycols].std().fillna(0.0)
        except Exception as e:
            print(f'WARNING: Failed to aggregate bandwidth data: {e}')
            try:
                gagg_mean = g.set_index(participants_col)[ycols]
                gagg_std = gagg_mean * 0.0
            except Exception as e2:
                print(f'WARNING: Failed fallback aggregation: {e2}')
                continue

        x = list(gagg_mean.index)
        for j, ycol in enumerate(ycols):
            y = gagg_mean[ycol]
            ystd = gagg_std[ycol]
            ls = linestyles[j % len(linestyles)]
            mk = marker if j == 0 else 'x'
            try:
                max_val = max(max_val, float(np.nanmax((y + ystd).fillna(0.0))))
            except Exception as e:
                print(f'WARNING: Failed to compute max bandwidth value: {e}')
            ax.plot(x, y, marker=mk, linestyle=ls, label=f'{arch_name} {labels[j]}', color=color)
            try:
                ax.fill_between(x, (y - ystd), (y + ystd), color=color, alpha=0.12)
            except Exception as e:
                print(f'WARNING: Failed to fill between values for bandwidth plot: {e}')

    ax.set_xlabel(GRAPH_NUM_CLIENT_LABEL)
    ax.set_ylabel(ylabel)
    #ax.set_title(title)
    ax.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
    try:
        xt = sorted(set(int(x) for x in df[participants_col].dropna().unique()))
        ax.set_xticks(xt)
    except Exception as e:
        print(f'WARNING: Failed to set x-axis ticks for bandwidth plot: {e}')
    if max_val is not None and max_val > 0:
        ax.set_ylim(0, max(max_val * 1.05, 0.1))
    else:
        ax.set_ylim(0, 1)
    ncol = len(groups) if len(groups) <= 3 else math.ceil(len(groups) / 2)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.35),  ncol=ncol, fontsize=8)
    plt.tight_layout()
    out_path = os.path.join(ANALYSIS_FOLDER, out_filename)
    try:
        fig.savefig(out_path)
    except Exception as e:
        print(f'WARNING: Failed to save bandwidth plot to {out_path}: {e}')
    try:
        plt.close(fig)
    except Exception as e:
        print(f'WARNING: Failed to close figure: {e}')
    print('Wrote measured bandwidth plot to', out_path)


def process_measured_bandwidth_rows(measured_rows, ANALYSIS_FOLDER):
    """Write measured per-client bandwidth CSV and produce a plot similar to
    bandwidth_mbps_per_client.svg but using the docker-measured bandwidth.csv
    values.
    """
    mb_df = pd.DataFrame(measured_rows)
    if mb_df.empty:
        return

    out_rows = []
    for _, r in mb_df.iterrows():
        parts = int(r.get('participants')) if pd.notna(r.get('participants')) else None
        out_bps_clients = r.get('measured_outgoing_bps_clients')
        in_bps_clients = r.get('measured_incoming_bps_clients')
        out_bps_host = r.get('measured_outgoing_bps_host')
        in_bps_host = r.get('measured_incoming_bps_host')
        out_rows.append({
            'Architecture': r.get('arch'),
            'Participants': parts,
            'Measured_Client_Outgoing_Mbps_per_client': (float(out_bps_clients) / 1e6) if out_bps_clients is not None else None,
            'Measured_Client_Incoming_Mbps_per_client': (float(in_bps_clients) / 1e6) if in_bps_clients is not None else None,
            'Measured_Host_Outgoing_Mbps': (float(out_bps_host) / 1e6) if out_bps_host is not None else None,
            'Measured_Host_Incoming_Mbps': (float(in_bps_host) / 1e6) if in_bps_host is not None else None
        })

    out_df = pd.DataFrame(out_rows)
    mb_csv = os.path.join(ANALYSIS_FOLDER, 'measured_bandwidth_per_client.csv')
    out_df.to_csv(mb_csv, index=False, sep=';')
    print('Wrote measured bandwidth summary to', mb_csv)

    # Split dataframes for client-only and host-only plotting
    client_cols = ['Architecture', 'Participants', 'Measured_Client_Outgoing_Mbps_per_client', 'Measured_Client_Incoming_Mbps_per_client']
    host_cols = ['Architecture', 'Participants', 'Measured_Host_Outgoing_Mbps', 'Measured_Host_Incoming_Mbps']

    client_df = out_df[client_cols].copy()
    host_df = out_df[host_cols].copy()

    # Clients plot (per-client Mbps) using generic helper
    try:
        _plot_measured_bandwidth_generic(client_df,
                                         'Participants', 'Architecture',
                                         ['Measured_Client_Outgoing_Mbps_per_client', 'Measured_Client_Incoming_Mbps_per_client'],
                                         ['Client Out', 'Client In'],
                                         ANALYSIS_FOLDER,
                                         'measured_bandwidth_clients_mbps_per_client.svg',
                                         'Measured Outgoing and Incoming Bandwidth (clients only)',
                                         'Measured Per-Client Bandwidth (Mbps)')
    except Exception as e:
        print('Failed to create measured client bandwidth plot:', e)

    # Host plot (host total Mbps) - separate graph because host scale can dominate
    try:
        _plot_measured_bandwidth_generic(host_df,
                                         'Participants', 'Architecture',
                                         ['Measured_Host_Outgoing_Mbps', 'Measured_Host_Incoming_Mbps'],
                                         ['Host Out', 'Host In'],
                                         ANALYSIS_FOLDER,
                                         'measured_bandwidth_host_mbps.svg',
                                         'Measured Host Outgoing and Incoming Bandwidth',
                                         'Measured Host Bandwidth (Mbps)')
    except Exception as e:
        print('Failed to create measured host bandwidth plot:', e)


def process_latency_rows(latency_rows, arch_map, ANALYSIS_FOLDER):
    """Write latency CSV and latency breakdown plot from latency_rows."""
    lat_df = pd.DataFrame(latency_rows)
    if lat_df.empty:
        return

    lat_out = lat_df.rename(columns={
        'arch': 'Architecture', 'participants': 'Participants',
        'avg_latency_ms': 'Avg. Latency(ms)', 'avg_network_latency_ms': 'Avg. Network Latency(ms)',
        'avg_encode_ms': 'Avg. Encoding Time (ms)',
        'avg_decode_ms': 'Avg. Decoding Time (ms)'
    })
    lat_csv = os.path.join(ANALYSIS_FOLDER, f'latency_summary.csv')
    lat_out.to_csv(lat_csv, index=False, sep=';')
    print('Wrote latency summary to', lat_csv)

    try:
        expected_runs = {}
        for arch, entries in arch_map.items():
            for n, _ in entries:
                try:
                    expected_runs[(arch, n)] = expected_runs.get((arch, n), 0) + 1
                except Exception as e:
                    print(f'WARNING: Failed to build expected_runs entry for arch={arch} n={n}: {e}')

        def _mean_from_group(g, col):
            """Return mean of numeric column `col` in group `g`, or None if empty/unavailable."""
            try:
                if col not in g:
                    return None
                vals = pd.to_numeric(g[col], errors='coerce').dropna().astype(float).tolist()
                return float(np.mean(vals)) if vals else None
            except Exception:
                return None

        agg_rows = []
        grouped = lat_df.groupby(['arch', 'participants'])
        for (arch, parts), g in grouped:
            try:
                parts_i = int(parts)
            except Exception as e:
                print(f'WARNING: Failed to convert participants {parts} to int: {e}')
                parts_i = parts
            key = (arch, parts_i)
            exp = expected_runs.get(key, 1)
            # Compute means from whatever samples are available.
            # A single broken/discarded repeat should not wipe out the whole participant count.
            try:
                tvals = pd.to_numeric(g['avg_latency_ms'], errors='coerce').dropna().astype(float).tolist() if 'avg_latency_ms' in g else []
            except Exception:
                tvals = []
            if exp and len(tvals) < exp:
                try:
                    print(f"Latency samples fewer than expected for arch={arch} participants={parts_i}: expected {exp}, got {len(tvals)}; using available samples")
                except Exception:
                    pass
            mean_t = float(np.mean(tvals)) if tvals else None
            # treat unreasonably large means as missing and log
            if mean_t is not None and mean_t >= 900.0:
                try:
                    print(f"Unreasonable mean latency for arch={arch} participants={parts_i}: {mean_t}; omitting from aggregates")
                except Exception:
                    print("Unreasonable mean latency encountered; omitting from aggregates")
                mean_t = None

            mean_e = _mean_from_group(g, 'avg_encode_ms')
            mean_d = _mean_from_group(g, 'avg_decode_ms')
            mean_n = _mean_from_group(g, 'avg_network_latency_ms')

            agg_rows.append({'arch': arch, 'participants': parts_i, 'mean_encode': mean_e,
                             'mean_decode': mean_d, 'mean_network': mean_n, 'mean_total': mean_t})

        if agg_rows:
            # Reorganize latency breakdown as grouped bars by participant count
            # x-axis: participant counts; for each participant show architectures side-by-side
            # with stacked segments (Encoding, Decoding, Other) so architectures are
            # comparable per participant count.
            # Build mapping participants -> arch -> segments
            data_map = {}
            archs = sorted({r['arch'] for r in agg_rows})
            parts = sorted({int(r['participants']) for r in agg_rows})
            for p in parts:
                data_map[p] = {}
                for a in archs:
                    data_map[p][a] = {'enc': 0.0, 'dec': 0.0, 'net': 0.0, 'oth': 0.0}
            for r in agg_rows:
                a = r['arch']
                p = int(r['participants'])
                t = r.get('mean_total') or 0.0
                e = r.get('mean_encode')
                d = r.get('mean_decode')
                n = r.get('mean_network')
                enc_val = float(e) if e is not None else 0.0
                dec_val = float(d) if d is not None else 0.0
                net_val = float(n) if n is not None else 0.0
                oth_val = max(0.0, float(t) - enc_val - dec_val - net_val)
                data_map[p][a] = {'enc': enc_val, 'dec': min(dec_val, 999.0), 'net': net_val, 'oth': oth_val}

            fig, ax = plt.subplots(figsize=FIGSIZE)
            x = np.arange(len(parts))
            total_width = 0.8
            n_arch = max(1, len(archs))
            bar_w = total_width / n_arch
            cmap = get_color_map(archs)
            hatch_map = {'enc': '', 'dec': '//', 'net': '++', 'oth': '..'}
            # draw bars per-architecture offset within each participant group
            for i, arch in enumerate(archs):
                pos = x - total_width/2 + i * bar_w + bar_w/2
                enc_vals = [data_map[p][arch]['enc'] for p in parts]
                dec_vals = [data_map[p][arch]['dec'] for p in parts]
                net_vals = [data_map[p][arch]['net'] for p in parts]
                oth_vals = [data_map[p][arch]['oth'] for p in parts]
                col = cmap.get(arch)
                p1 = ax.bar(pos, enc_vals, bar_w, color=col, label=arch if i == 0 else None, hatch=hatch_map['enc'], edgecolor='black')
                p2 = ax.bar(pos, dec_vals, bar_w, bottom=enc_vals, color=col, hatch=hatch_map['dec'], edgecolor='black')
                bottom_ed = np.array(enc_vals) + np.array(dec_vals)
                p3 = ax.bar(pos, net_vals, bar_w, bottom=bottom_ed, color=col, hatch=hatch_map['net'], edgecolor='black')
                bottom_ed_net = bottom_ed + np.array(net_vals)
                #p4 = ax.bar(pos, oth_vals, bar_w, bottom=bottom_ed_net, color=col, hatch=hatch_map['oth'], edgecolor='black')

            # X axis ticks are participant counts (one group per participant count)
            ax.set_xticks(x)
            ax.set_xticklabels([str(p) for p in parts], rotation=0)
            ax.set_xlabel(GRAPH_NUM_CLIENT_LABEL)
            ax.set_ylabel('Time (ms)')
            #ax.set_title('Latency breakdown (grouped by participants, architectures side-by-side)')
            ax.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
            # Build legend: show architectures (color) and stack segments (hatch)
            arch_handles = [mpatches.Patch(facecolor=cmap.get(a), edgecolor='black', label=a) for a in archs]
            seg_handles = [mpatches.Patch(facecolor='white', edgecolor='black', hatch=hatch_map[k], label=lab) for k, lab in [('enc','Encoding'), ('dec','Decoding'), ('net','Network'), ]]#('oth','Other')]]
            # Place two legends: architectures on upper left, segments on upper right
            if arch_handles:
                leg1 = ax.legend(handles=arch_handles, title='Architectures', fontsize=8, loc='upper left')
                ax.add_artist(leg1)
            ax.legend(handles=seg_handles, title='Segments', fontsize=8, loc='upper right')
            plt.tight_layout()
            bar_out = os.path.join(ANALYSIS_FOLDER, 'latency_barchart.svg')
            fig.savefig(bar_out)
            plt.close(fig)
            print('Wrote latency barchart to', bar_out)
        else:
            fig, ax = plt.subplots(figsize=FIGSIZE)
            ax.text(0.5, 0.5, 'No complete aggregated latency data available to plot', ha='center', va='center')
            ax.axis('off')
            bar_out = os.path.join(ANALYSIS_FOLDER, 'latency_barchart.svg')
            fig.savefig(bar_out)
            plt.close(fig)
            print('Wrote placeholder latency barchart to', bar_out)

        # --- Simple line graph: mean_total per architecture vs participants ---
        try:
            # Use only aggregated rows with a valid mean_total
            valid_parts = sorted({int(r['participants']) for r in agg_rows if r.get('mean_total') is not None})
            if not valid_parts:
                fig, ax = plt.subplots(figsize=FIGSIZE)
                ax.text(0.5, 0.5, 'No latency totals available to plot', ha='center', va='center')
                ax.axis('off')
                line_out = os.path.join(ANALYSIS_FOLDER, 'latency_linechart.svg')
                fig.savefig(line_out)
                plt.close(fig)
                print('Wrote placeholder latency linechart to', line_out)
            else:
                # Build mean/std DataFrames similar to PSNR plotting helper
                participants_idx = valid_parts
                mean_df = pd.DataFrame(index=participants_idx)
                std_df = pd.DataFrame(index=participants_idx)
                arch_keys = sorted({r['arch'] for r in agg_rows})
                for arch in arch_keys:
                    vals_map = {int(r['participants']): float(r['mean_total']) for r in agg_rows if r['arch'] == arch and r.get('mean_total') is not None}
                    means = []
                    stds = []
                    for n in participants_idx:
                        v = vals_map.get(n)
                        if v is None:
                            means.append(np.nan)
                            stds.append(np.nan)
                        else:
                            means.append(v)
                            stds.append(0.0)
                    mean_df[arch] = means
                    std_df[arch] = stds

                # Plot with shading for std (mirrors plot_psnr style)
                fig, ax = plt.subplots(figsize=FIGSIZE)
                markers = ['o', 's', '^', 'D', 'v', 'P', 'X', '*']
                cmap = get_color_map(mean_df.columns)
                for i, col in enumerate(mean_df.columns):
                    y = mean_df[col]
                    ax.plot(mean_df.index, y, marker=markers[i % len(markers)], label=col, color=cmap.get(col))
                    if col in std_df.columns:
                        std = std_df[col].fillna(0)
                        try:
                            ax.fill_between(mean_df.index, (y - std), (y + std), alpha=0.15, color=cmap.get(col))
                        except Exception:
                            pass
                ax.set_xlabel(GRAPH_NUM_CLIENT_LABEL)
                ax.set_ylabel('Mean Total Latency (ms)')
                #ax.set_title('Mean Total Latency - aggregated runs')
                ax.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
                try:
                    xt = [int(x) for x in mean_df.index]
                    ax.set_xticks(xt)
                except Exception:
                    pass
                # enforce y-axis starting at zero; compute automatic upper bound from mean+std
                try:
                    combined = (mean_df.fillna(0) + std_df.fillna(0)).values
                    max_val = float(np.nanmax(combined)) if combined.size else None
                except Exception:
                    max_val = None
                if max_val is not None and max_val > 0:
                    ax.set_ylim(0, max(max_val * 1.05, 1.0))
                else:
                    ax.set_ylim(0, 1)
                ncol = len(mean_df.columns) if len(mean_df.columns) <= 3 else math.ceil(len(mean_df.columns) / 2)
                ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.4),  ncol=ncol, prop={'size': 10})
                plt.tight_layout()
                line_out = os.path.join(ANALYSIS_FOLDER, 'latency_linechart.svg')
                fig.savefig(line_out)
                plt.close(fig)
                print('Wrote latency linechart to', line_out)
        except Exception as e:
            print('Failed to create latency totals line plot:', e)
        # Simple line graph of the sum of mean_encode + mean_decode + mean_network (if available) per architecture vs participants, with shading for stddev if available. This is a simpler alternative to the stacked bar chart and may be more robust when there are fewer complete samples.
        try:
            # Use only aggregated rows with a valid mean_encode + mean_decode + mean_network
            valid_parts = sorted({int(r['participants']) for r in agg_rows if r.get('mean_encode') is not None and r.get('mean_decode') is not None and r.get('mean_network') is not None})
            if not valid_parts:
                fig, ax = plt.subplots(figsize=FIGSIZE)
                ax.text(0.5, 0.5, 'No latency available to plot', ha='center', va='center')
                ax.axis('off')
                line_out = os.path.join(ANALYSIS_FOLDER, 'latency_encode_decode_network_linechart.svg')
                fig.savefig(line_out)
                plt.close(fig)
                print('Wrote placeholder latency linechart to', line_out)
            else:
                # Build mean/std DataFrames similar to PSNR plotting helper
                participants_idx = valid_parts
                mean_df = pd.DataFrame(index=participants_idx)
                std_df = pd.DataFrame(index=participants_idx)
                arch_keys = sorted({r['arch'] for r in agg_rows})
                for arch in arch_keys:
                    #vals_map = {int(r['participants']): float(r['mean_encode'] + r['mean_decode'] + r['mean_network']) for r in agg_rows if r['arch'] == arch and r.get('mean_encode') is not None and r.get('mean_decode') is not None and r.get('mean_network') is not None}
                    vals_map = {int(r['participants']): float(r['mean_network']) for r in agg_rows if r['arch'] == arch and r.get('mean_network') is not None}
                    means = []
                    stds = []
                    for n in participants_idx:
                        v = vals_map.get(n)
                        if v is None:
                            means.append(np.nan)
                            stds.append(np.nan)
                        else:
                            means.append(v)
                            stds.append(0.0)
                    mean_df[arch] = means
                    std_df[arch] = stds

                # Plot with shading for std (mirrors plot_psnr style)
                fig, ax = plt.subplots(figsize=FIGSIZE)
                markers = ['o', 's', '^', 'D', 'v', 'P', 'X', '*']
                cmap = get_color_map(mean_df.columns)
                for i, col in enumerate(mean_df.columns):
                    y = mean_df[col]
                    ax.plot(mean_df.index, y, marker=markers[i % len(markers)], label=col, color=cmap.get(col))
                    if col in std_df.columns:
                        std = std_df[col].fillna(0)
                        try:
                            ax.fill_between(mean_df.index, (y - std), (y + std), alpha=0.15, color=cmap.get(col))
                        except Exception:
                            pass
                ax.set_xlabel(GRAPH_NUM_CLIENT_LABEL)
                ax.set_ylabel('Mean Total Latency (ms)')
                #ax.set_title('Mean Total Latency - aggregated runs')
                ax.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
                try:
                    xt = [int(x) for x in mean_df.index]
                    ax.set_xticks(xt)
                except Exception:
                    pass
                # enforce y-axis starting at zero; compute automatic upper bound from mean+std
                try:
                    combined = (mean_df.fillna(0) + std_df.fillna(0)).values
                    max_val = float(np.nanmax(combined)) if combined.size else None
                except Exception:
                    max_val = None
                if max_val is not None and max_val > 0:
                    ax.set_ylim(0, max(max_val * 1.05, 1.0))
                else:
                    ax.set_ylim(0, 1)
                ncol = len(mean_df.columns) if len(mean_df.columns) <= 3 else math.ceil(len(mean_df.columns) / 2)
                ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.4),  ncol=ncol, prop={'size': 10})
                plt.tight_layout()
                line_out = os.path.join(ANALYSIS_FOLDER, 'latency_encode_decode_network_linechart.svg')
                fig.savefig(line_out)
                plt.close(fig)
                print('Wrote latency encode+decode+network linechart to', line_out)
        except Exception as e:
            print('Failed to create latency encode+decode+network line plot:', e)
    except Exception as e:
        print('Failed to create latency breakdown plot:', e)


def finalize_cpu_and_psnr(cpu_results, psnr_mean, psnr_std, ANALYSIS_FOLDER, scenario):
    """Aggregate CPU results and produce CPU/PSNR plots."""
    averaged_cpu = {}
    for arch, rows in cpu_results.items():
        by_n = defaultdict(list)
        for participants, val in rows:
            try:
                by_n[int(participants)].append(float(val))
            except Exception as e:
                # ignore malformed entries
                print(f'WARNING: Ignoring malformed CPU result for {arch}: participants={participants} val={val}: {e}')
                continue
        averaged = []
        for participants, vals in sorted(by_n.items()):
            try:
                meanv = float(np.nanmean(vals)) if vals else None
            except Exception as e:
                print(f'WARNING: Failed to compute mean CPU for arch={arch} participants={participants}: {e}')
                meanv = None
            averaged.append((participants, meanv))
        averaged_cpu[arch] = averaged
    plot_cpu(averaged_cpu, ANALYSIS_FOLDER, scenario)

    if not psnr_mean.empty:
        plot_psnr(psnr_mean, psnr_std, ANALYSIS_FOLDER, scenario)
    # return mapping for root-level aggregation
    return averaged_cpu


def process_group(participants, entries):
    """Process all runs for a given participant count `participants`.

    Runs analyze_run sequentially for each (arch, run_path) in entries and
    returns a list of tuples (arch, participants, run_path, pruned_metrics).

    Pruned metrics contains only the scalar/serializable fields needed by
    `accumulate_run_results` to avoid heavy DataFrame pickling between processes.
    """
    pruned_results = []
    # keys to keep from analyze_run's metrics (avoid DataFrames)
    keep_keys = ['metadata', 'cpu_avg', 'avg_psnr', 'psnr_count', 'avg_frame_size',
                 'avg_width', 'avg_height', 'avg_encode_ms', 'outgoing_bps', 'incoming_bps',
                 'avg_latency_ms', 'avg_network_latency_ms', 'avg_decode_ms', 'avg_part_frame_size', 'missing_summary',
                 'measured_outgoing_bps_per_client', 'measured_incoming_bps_per_client',
                 'measured_outgoing_bps_per_client_list', 'psnr_per_client',
                 'measured_host_outgoing_bps', 'measured_host_incoming_bps',
                 'latency_per_sender',
                 'max_encode_by_client', 'max_decode_by_client']
    for arch, run_path in entries:
        try:
            metrics = analyze_run(run_path)
        except Exception as e:
            print(f"analyze_run failed in group participants={participants} for {run_path}: {e}")
            pruned_results.append((arch, participants, run_path, None))
            continue
        if metrics is None:
            pruned_results.append((arch, participants, run_path, None))
            continue
        pr = {k: metrics.get(k) for k in keep_keys if k in metrics}
        # Keep the original missing_summary if present (it's a list of dicts and serializable)
        pruned_results.append((arch, participants, run_path, pr))
    return pruned_results


def write_root_cpu_summary(results_by_arch, ROOT_FOLDER):
    """Write a root-level CPU summary CSV and plot under ROOT_FOLDER/analysis.

    results_by_arch: dict arch -> list of (participants, cpu_avg)
    """
    if not results_by_arch:
        return
    summary_rows = []
    for arch, rows in results_by_arch.items():
        # build sorted arrays, filter none/NaN
        vals = [(int(p), float(v)) for (p, v) in rows if p is not None and v is not None]
        if not vals:
            continue
        vals_sorted = sorted(vals, key=lambda x: x[0])
        xs = np.array([v[0] for v in vals_sorted], dtype=float)
        ys = np.array([v[1] for v in vals_sorted], dtype=float)
        # compute AUC using trapezoidal rule; normalize by participant span if >0
        try:
            auc = float(np.trapz(ys, xs))
        except Exception:
            auc = float(np.nan)
        span = float(xs.max() - xs.min()) if xs.size > 1 else 0.0
        norm_auc = (auc / span) if span > 0 else auc
        meanv = float(np.nanmean(ys))
        medianv = float(np.nanmedian(ys))
        summary_rows.append({'Architecture': arch, 'AUC': auc, 'Normalized_AUC': norm_auc,
                             'Mean_CPU': meanv, 'Median_CPU': medianv, 'DataPoints': len(xs)})

    if not summary_rows:
        return
    analysis_root = os.path.join(ROOT_FOLDER, 'analysis')
    ensure_dir(analysis_root)

    # Also produce per-participant aggregation (mean/std/count) across scenarios
    cpu_by_parts_rows = []
    mean_map = {}
    for arch, rows in results_by_arch.items():
        by_n = defaultdict(list)
        for p, v in rows:
            try:
                if p is None or v is None:
                    continue
                by_n[int(p)].append(float(v))
            except Exception as e:
                print(f'WARNING: Ignoring malformed CPU result in root summary for arch={arch}: p={p} v={v}: {e}')
                continue
        for parts, vals in sorted(by_n.items()):
            meanv = float(np.nanmean(vals)) if vals else np.nan
            stdv = float(np.nanstd(vals)) if vals else np.nan
            cnt = len(vals)
            try:
                cpu_by_parts_rows.append({'Architecture': arch, 'Participants': parts, 'Mean_CPU': meanv, 'Std_CPU': stdv, 'Count': cnt})
            except Exception as e:
                print(f'WARNING: Failed to append CPU summary row for arch={arch} participants={parts}: {e}')
        # prepare mapping for plotting
        mean_map[arch] = [(parts, float(np.nanmean(vals))) for parts, vals in sorted(by_n.items())]

    # write per-participant CSV
    parts_csv = os.path.join(analysis_root, 'cpu_summary_by_participants.csv')
    pd.DataFrame(cpu_by_parts_rows).to_csv(parts_csv, index=False, sep=';')
    print('Wrote CPU summary by participants to', parts_csv)

    # Note: per-user request, the aggregate AUC CSV and bar plot are omitted.

    # Plot 2: per-participant mean with errorbars (std) per architecture
    try:
        fig, ax = plt.subplots(figsize=FIGSIZE)
        cmap = get_color_map(sorted(mean_map.keys()))
        markers = ['o', 's', '^', 'D', 'v', 'P', 'X']
        for i, (arch, pts) in enumerate(sorted(mean_map.items())):
            if not pts:
                continue
            xs = [p for p, _ in pts]
            ys = [y for _, y in pts]
            # find stds from cpu_by_parts_rows
            stds = []
            for x in xs:
                row = next((r for r in cpu_by_parts_rows if r['Architecture'] == arch and r['Participants'] == x), None)
                stds.append(row['Std_CPU'] if row is not None else np.nan)
            color = cmap.get(arch)
            mk = markers[i % len(markers)]
            ax.errorbar(xs, ys, yerr=stds, marker=mk, linestyle='-', label=arch, color=color, capsize=3)
        ax.set_xlabel(GRAPH_NUM_CLIENT_LABEL)
        ax.set_ylabel('Average Total CPU %')
        #ax.set_title('CPU by Participants (root summary)')
        ax.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
        # Force y-axis 0..100 and ticks every 10% for consistent CPU % visualization
        #ax.set_ylim(0, 100)
        #ax.set_yticks(np.arange(0, 101, 10))
        
        try:
            # set integer xticks
            all_x = sorted({int(r['Participants']) for r in cpu_by_parts_rows})
            ax.set_xticks(all_x)
        except Exception as e:
            print(f'WARNING: Failed to set x-axis ticks for CPU plot: {e}')
        ncol = len(mean_map) if len(mean_map) <= 3 else math.ceil(len(mean_map) / 2)
        ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.3),  ncol=ncol, fontsize=9)
        plt.tight_layout()
        outp2 = os.path.join(analysis_root, 'cpu_summary_by_participants.svg')
        fig.savefig(outp2)
        plt.close(fig)
        print('Wrote CPU by-participants plot to', outp2)
    except Exception as e:
        print('Failed to create CPU by-participants plot:', e)


def write_root_latency_summary(scenarios, ROOT_FOLDER):
    """Aggregate per-scenario latency summaries into a root-level CSV and line plot.

    - `scenarios` is a list of scenario folder names under ROOT_FOLDER
    - Reads each `ROOT_FOLDER/analysis/<scenario>/latency_summary.csv` if present
    - Detects latency type from the scenario name (lat-none, lat-local, lat-global)
    - Groups by (LatencyType, Architecture, Participants) and averages `Avg. Latency(ms)`
    - Writes `ROOT_FOLDER/analysis/latency_root_summary.csv` and
      `ROOT_FOLDER/analysis/latency_root_linechart.svg`.
    """
    analysis_root = os.path.join(ROOT_FOLDER, 'analysis')
    ensure_dir(analysis_root)

    rows = []
    for scenario in scenarios:
        lat_csv = os.path.join(analysis_root, scenario, 'latency_summary.csv')
        if not os.path.isfile(lat_csv):
            continue
        # read latency summary written by process_latency_rows (semicolon-separated)
        try:
            df = pd.read_csv(lat_csv, sep=';', engine='python')
        except Exception as e:
            # skip unreadable files
            print(f'WARNING: Failed to read latency CSV {lat_csv}: {e}')
            continue
        if df.empty:
            continue
        sname = scenario.lower()
        # Only accept the predetermined latency types to keep code minimal.
        if 'lat-none' in sname:
            ltype = 'none'
        elif 'lat-local' in sname:
            ltype = 'local'
        elif 'lat-global' in sname:
            ltype = 'global'
        else:
            # skip scenarios that are not one of the three known latency types
            continue

        # assume `process_latency_rows` produced these canonical columns
        if not {'Architecture', 'Participants', 'Avg. Latency(ms)'}.issubset(set(df.columns)):
            continue

        # Try to extract resolution and upload-limit (upload bitrate configuration) from scenario name
        res = None
        upload = None
        for token in scenario.split('_'):
            if res is None and 'x' in token and token.split('x', 1)[0].isdigit():
                res = token
            # tokens like 'ul-all1', 'ul-all10', 'ul-500kbps' etc.
            if upload is None and token.lower().startswith('ul'):
                upload = token

        # fallback: try to read metadata under scenario folder for Resolution / Upload limit
        if (res is None or upload is None):
            meta_path = os.path.join(ROOT_FOLDER, scenario, 'metadata.txt')
            try:
                if os.path.isfile(meta_path):
                    meta = parse_metadata(meta_path)
                    if res is None:
                        res = meta.get('Resolution')
                    if upload is None:
                        # permissive keys
                        upload = meta.get('Upload_Limit') or meta.get('Upload') or meta.get('Upload_Bitrate')
            except Exception as e:
                print(f'WARNING: Failed to read metadata from {meta_path}: {e}')

        if res is None:
            # skip scenarios without resolution (maintain original behavior)
            continue
        if upload is None:
            upload = 'unknown'

        for _, r in df.iterrows():
            arch = r['Architecture']
            parts = r['Participants']
            try:
                parts = int(parts) if pd.notna(parts) else None
            except Exception as e:
                print(f'WARNING: Failed to convert participants to int: {e}')
                parts = None
            latv = r['Avg. Latency(ms)']
            try:
                latv = float(latv) if pd.notna(latv) else None
            except Exception as e:
                print(f'WARNING: Failed to convert latency to float: {e}')
                latv = None
            if latv is None:
                continue
            rows.append({'Upload': str(upload), 'Resolution': str(res), 'LatencyType': ltype, 'Architecture': arch, 'Participants': parts, 'AvgLatencyMs': latv})

    if not rows:
        print('No per-scenario latency summaries found for root aggregation.')
        return

    rdf = pd.DataFrame(rows)
    if rdf.empty:
        print('No per-scenario latency summaries found for root aggregation.')
        return

    # Aggregate by Upload, Resolution, LatencyType, Architecture, Participants
    agg = rdf.groupby(['Upload', 'Resolution', 'LatencyType', 'Architecture', 'Participants'], dropna=False)['AvgLatencyMs'].mean().reset_index()

    out_csv = os.path.join(analysis_root, 'latency_root_summary.csv')
    # rename column on the DataFrame used for plotting as well
    agg = agg.rename(columns={'AvgLatencyMs': 'Mean_Avg_Latency_ms'})
    agg.to_csv(out_csv, index=False, sep=';')
    print('Wrote root latency summary to', out_csv)

    # For each unique (Upload, Resolution) pair produce a separate latency linechart
    uploads = sorted(agg['Upload'].dropna().unique())
    resolutions = sorted(agg['Resolution'].dropna().unique())
    linestyle_map = {'none': '-', 'local': '--', 'global': ':'}
    latency_types = ['global', 'local', 'none']
    markers = ['o', 's', '^', 'D', 'v', 'P', 'X']

    for up in uploads:
        for res in resolutions:
            sub = agg[(agg['Upload'] == up) & (agg['Resolution'] == res)]
            if sub.empty:
                continue

            fig, ax = plt.subplots(figsize=(10, 6))
            archs = sorted(sub['Architecture'].dropna().unique())
            cmap = get_color_map(archs)
            max_val = 0.0
            try:
                xticks = sorted(set(int(x) for x in sub['Participants'].dropna().unique()))
            except Exception:
                xticks = []

            for i, arch in enumerate(archs):
                marker = markers[i % len(markers)]
                for lt in latency_types:
                    sel = sub[(sub['LatencyType'] == lt) & (sub['Architecture'] == arch)]
                    if sel.empty:
                        continue
                    sel_sorted = sel.sort_values('Participants')
                    xs = sel_sorted['Participants'].astype(float).tolist()
                    ys = sel_sorted['Mean_Avg_Latency_ms'].astype(float).tolist()
                    if not xs or not ys:
                        continue
                    label = f"{arch} - {lt}"
                    ax.plot(xs, ys, label=label, color=cmap.get(arch), linestyle=linestyle_map.get(lt, '-'), marker=marker)
                    max_val = max(max_val, max(ys))

            ax.set_xlabel(GRAPH_NUM_CLIENT_LABEL)
            ax.set_ylabel('Mean Total Latency (ms)')
            ax.set_title(f'Root Latency Summary - upload={up} res={res}')
            ax.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
            if max_val <= 0:
                ax.set_ylim(0, 1)
            else:
                ax.set_ylim(0, max_val * 1.05)
            if xticks:
                ax.set_xticks(xticks)
            ncol = len(archs) if len(archs) <= 3 else math.ceil(len(archs) / 2)
            ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.3),  ncol=ncol, fontsize=8)
            plt.tight_layout()
            safe_up = str(up).replace('/', '-').replace(' ', '_')
            safe_res = str(res).replace('/', '-').replace(' ', '_')
            outp = os.path.join(analysis_root, f'latency_root_linechart_ul-{safe_up}_res-{safe_res}.svg')
            fig.savefig(outp)
            plt.close(fig)
            print('Wrote root latency linechart to', outp)


def write_root_measured_bandwidth_summary(scenarios, ROOT_FOLDER):
    """Aggregate per-scenario measured bandwidth CSVs into root-level CSVs and plots.

    For each resolution and view mode (parsed from scenario name), create a
    CSV and an SVG plot under `ROOT_FOLDER/analysis/` showing mean client
    outgoing/incoming Mbps per-client per-architecture with min/max filled ranges.
    """
    analysis_root = os.path.join(ROOT_FOLDER, 'analysis')
    ensure_dir(analysis_root)

    # collect rows per (resolution, view)
    grouped = defaultdict(list)
    for scenario in scenarios:
        scen_csv = os.path.join(analysis_root, scenario, 'measured_bandwidth_per_client.csv')
        if not os.path.isfile(scen_csv):
            continue
        try:
            df = pd.read_csv(scen_csv, sep=';', engine='python')
        except Exception as e:
            print(f'WARNING: Failed to read measured bandwidth CSV {scen_csv}: {e}')
            continue

        # attempt to extract resolution and view from scenario name
        res = None
        view = None
        # resolution like 1920x1080
        for token in scenario.split('_'):
            if 'x' in token and token.split('x', 1)[0].isdigit():
                res = token
                break
        if 'view-gallery' in scenario.lower() or 'view-gallery' in scen_csv.lower():
            view = 'gallery'
        elif 'view-speaker' in scenario.lower() or 'view-speaker' in scen_csv.lower():
            view = 'speaker'

        # fallback: try to read metadata under scenario folder for Resolution / View_Mode
        if (res is None or view is None):
            meta_path = os.path.join(ROOT_FOLDER, scenario, 'metadata.txt')
            try:
                if os.path.isfile(meta_path):
                    meta = parse_metadata(meta_path)
                    if res is None:
                        res = meta.get('Resolution')
                    if view is None:
                        v = meta.get('View_Mode') or meta.get('View') or meta.get('ViewMode')
                        if v:
                            view = str(v).strip().lower()
            except Exception as e:
                print(f'WARNING: Failed to read metadata from {meta_path}: {e}')

        if res is None:
            # skip scenarios without resolution
            continue
        if view is None:
            # default to gallery if unknown
            view = 'gallery'

        # attach resolution/view as attributes and collect rows
        df['_scenario'] = scenario
        df['_resolution'] = res
        df['_view'] = view
        grouped[(res, view)].append(df)

    # For each resolution/view group, aggregate and write CSV + plot
    for (res, view), dfs in grouped.items():
        try:
            all_df = pd.concat(dfs, ignore_index=True, sort=False)
        except Exception as e:
            print(f'WARNING: Failed to concatenate DataFrames for res={res} view={view}: {e}')
            continue

        # normalize participants to numeric
        try:
            all_df['Participants'] = pd.to_numeric(all_df['Participants'], errors='coerce')
        except Exception as e:
            print(f'WARNING: Failed to convert participants to numeric for res={res} view={view}: {e}')

        # group by Architecture and Participants and compute mean/min/max for client in/out and host in/out
        agg_rows = []
        group_keys = ['Architecture', 'Participants']
        grouped2 = all_df.groupby(group_keys, dropna=False)
        for (arch, parts), g in grouped2:
            def col_stats(col):
                if col in g.columns:
                    vals = pd.to_numeric(g[col], errors='coerce').dropna().values
                    if vals.size:
                        return float(np.mean(vals)), float(np.min(vals)), float(np.max(vals))
                return None, None, None

            out_mean, out_min, out_max = col_stats('Measured_Client_Outgoing_Mbps_per_client')
            in_mean, in_min, in_max = col_stats('Measured_Client_Incoming_Mbps_per_client')
            host_out_mean, host_out_min, host_out_max = col_stats('Measured_Host_Outgoing_Mbps')
            host_in_mean, host_in_min, host_in_max = col_stats('Measured_Host_Incoming_Mbps')

            agg_rows.append({'Architecture': arch, 'Participants': int(parts) if pd.notna(parts) else None,
                             'Client_Out_Mean': out_mean, 'Client_Out_Min': out_min, 'Client_Out_Max': out_max,
                             'Client_In_Mean': in_mean, 'Client_In_Min': in_min, 'Client_In_Max': in_max,
                             'Host_Out_Mean': host_out_mean, 'Host_Out_Min': host_out_min, 'Host_Out_Max': host_out_max,
                             'Host_In_Mean': host_in_mean, 'Host_In_Min': host_in_min, 'Host_In_Max': host_in_max})

        if not agg_rows:
            continue

        out_df = pd.DataFrame(agg_rows)
        csv_name = os.path.join(analysis_root, f'measured_bandwidth_root_{res}_view-{view}.csv')
        out_df.to_csv(csv_name, index=False, sep=';')
        print('Wrote root measured bandwidth CSV to', csv_name)

        # Plot mean lines with min/max filled ranges for client Out/In per architecture
        try:
            fig, ax = plt.subplots(figsize=(10, 5))
            archs = sorted([a for a in out_df['Architecture'].dropna().unique()])
            cmap = get_color_map(archs)
            markers = ['o', 's', '^', 'D', 'v', 'P', 'X']
            for i, arch in enumerate(archs):
                sub = out_df[out_df['Architecture'] == arch].sort_values('Participants')
                xs = sub['Participants'].tolist()

                # client out
                ys_mean = sub['Client_Out_Mean'].tolist()
                ys_min = sub['Client_Out_Min'].tolist()
                ys_max = sub['Client_Out_Max'].tolist()
                if any(pd.notna(ys_mean)):
                    ax.plot(xs, ys_mean, marker=markers[i % len(markers)], linestyle='-', label=f'{arch} Out', color=cmap.get(arch))
                    # show discrete min/max as errorbars (non-continuous)
                    try:
                        xm = np.array(xs, dtype=float)
                        ym = np.array(ys_mean, dtype=float)
                        ymi = np.array(ys_min, dtype=float)
                        yma = np.array(ys_max, dtype=float)
                        mask = ~np.isnan(ym)
                        if np.any(mask):
                            xm2 = xm[mask]
                            ym2 = ym[mask]
                            lower = ym2 - ymi[mask]
                            upper = yma[mask] - ym2
                            # replace negative or nan errs with 0
                            lower = np.where(np.isfinite(lower) & (lower > 0), lower, 0.0)
                            upper = np.where(np.isfinite(upper) & (upper > 0), upper, 0.0)
                            ax.errorbar(xm2, ym2, yerr=[lower, upper], fmt='none', ecolor=cmap.get(arch), capsize=4, linewidth=1.2)
                    except Exception:
                        pass

                # client in
                ys_mean_in = sub['Client_In_Mean'].tolist()
                ys_min_in = sub['Client_In_Min'].tolist()
                ys_max_in = sub['Client_In_Max'].tolist()
                if any(pd.notna(ys_mean_in)):
                    ax.plot(xs, ys_mean_in, marker=markers[i % len(markers)], linestyle='--', label=f'{arch} In', color=cmap.get(arch))
                    try:
                        xm = np.array(xs, dtype=float)
                        ym = np.array(ys_mean_in, dtype=float)
                        ymi = np.array(ys_min_in, dtype=float)
                        yma = np.array(ys_max_in, dtype=float)
                        mask = ~np.isnan(ym)
                        if np.any(mask):
                            xm2 = xm[mask]
                            ym2 = ym[mask]
                            lower = ym2 - ymi[mask]
                            upper = yma[mask] - ym2
                            lower = np.where(np.isfinite(lower) & (lower > 0), lower, 0.0)
                            upper = np.where(np.isfinite(upper) & (upper > 0), upper, 0.0)
                            ax.errorbar(xm2, ym2, yerr=[lower, upper], fmt='none', ecolor=cmap.get(arch), capsize=4, linewidth=1.0)
                    except Exception:
                        pass

            ax.set_xlabel(GRAPH_NUM_CLIENT_LABEL)
            ax.set_ylabel('Measured Per-Client Bandwidth (Mbps)')
            #ax.set_title(f'Measured Bandwidth (per-client) - {res} view={view}')
            ax.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
            try:
                xt = sorted(set(int(x) for x in out_df['Participants'].dropna().unique()))
                ax.set_xticks(xt)
            except Exception:
                pass
            ncol = len(archs) if len(archs) <= 3 else math.ceil(len(archs) / 2)
            ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.3),  ncol=ncol, fontsize=9)
            plt.tight_layout()
            outp = os.path.join(analysis_root, f'measured_bandwidth_root_{res}_view-{view}.svg')
            fig.savefig(outp)
            plt.close(fig)
            print('Wrote root measured bandwidth plot to', outp)
        except Exception as e:
            print('Failed to create root measured bandwidth plot for', res, view, e)



def process_scenario(ROOT_FOLDER, scenario):
    """Process one scenario directory: analyze architectures and write CSVs/plots.

    Returns a tuple `(averaged_cpu, discarded_runs, failed_runs)`.  *discarded_runs* are
    runs dropped from aggregation (broken clients, analysis failure, etc.).  *failed_runs*
    are those with missing-frame errors; they are still accumulated but reported
    separately for visibility.
    """
    scenario_folder = os.path.join(ROOT_FOLDER, scenario)
    ANALYSIS_FOLDER = setup_analysis_folders(ROOT_FOLDER, scenario)

    arch_map = build_arch_map(scenario_folder)

    # compute per-architecture aggregated metrics
    cpu_results = defaultdict(list)  # arch -> list of (participants, cpu_avg) across all runs
    psnr_stats = defaultdict(dict)
    missing_rows = []
    resolution_rows = []
    measured_rows = []
    # New aggregators for first-vs-others analysis
    psnr_first_stats = defaultdict(dict)
    psnr_rest_stats = defaultdict(dict)
    measured_first_stats = defaultdict(dict)
    measured_rest_stats = defaultdict(dict)
    latency_first_stats = defaultdict(dict)
    latency_rest_stats = defaultdict(dict)
    latency_rows = []
    client_max_rows = []
    presence_records = []
    # preserve an unfiltered copy of presence records for diagnostics
    presence_records_unfiltered = []
    # collect runs that are discarded due to broken clients or other analysis failures
    discarded_runs = []
    # runs that failed because of missing frames; we still include their metrics in the
    # aggregated results but mark them separately so callers can inspect them.
    failed_runs = []

    # Build list of runs and collect presence records first (cheap). Parallelize collection
    runs = []
    for arch, entries in arch_map.items():
        for participants, run_path in sorted(entries, key=lambda x: (x[0], x[1])):
            runs.append((arch, participants, run_path))

    total_runs = len(runs)
    tasks = []
    if total_runs:
        # Use process-based parallelism for presence collection to utilize multiple CPU cores.
        # This script prefers clarity: presence collection runs exclusively with processes.
        max_workers = min((os.cpu_count() or 4), total_runs)
        run_counter = 0
        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as exc:
            future_to_run = {exc.submit(collect_presence_records_for_run, run_path, arch, participants): (arch, participants, run_path)
                             for (arch, participants, run_path) in runs}
            for fut in concurrent.futures.as_completed(future_to_run):
                arch, participants, run_path = future_to_run[fut]
                # Let exceptions surface or be logged here for visibility.
                try:
                    filtered, unfiltered = fut.result()
                except Exception as e:
                    print(f"[presence] failed for {arch}-{participants} {run_path}: {e}")
                    filtered, unfiltered = [], []
                run_counter += 1
                presence_records.extend(filtered)
                presence_records_unfiltered.extend(unfiltered)
                print(f"[presence {run_counter}/{total_runs}] collected filtered={len(filtered)} unfiltered={len(unfiltered)} record(s) for {arch}-{participants} run {os.path.basename(run_path)}")
                tasks.append((arch, participants, run_path))

    # Analyze runs grouped by participant count. Parallelize across distinct participant counts
    # to reduce low-level task overhead and better utilize CPU across groups.
    if tasks:
        # Group runs by participant count
        groups = defaultdict(list)
        for arch, participants, run_path in tasks:
            groups[participants].append((arch, run_path))

        max_workers = min((os.cpu_count() or 4), len(groups))
        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as exc:
            future_to_participants = {exc.submit(process_group, participants, groups[participants]): participants for participants in groups}
            for fut in concurrent.futures.as_completed(future_to_participants):
                participants = future_to_participants[fut]
                try:
                    group_results = fut.result()
                except Exception as e:
                    print(f"process_group failed for participants={participants}: {e}")
                    continue
                # Merge per-run metrics returned from the group worker
                for arch, participants, run_path, metrics in group_results:
                    # Treat analyze failures as discarded runs
                    if metrics is None:
                        discarded_runs.append({'arch': arch, 'participants': participants, 'run_path': run_path, 'reason': 'analyze_failed'})
                        # still continue to next; presence_records will contain any available diagnostics
                        continue

                    # Always add missing-frame diagnostics to missing_rows so diagnostics CSVs include discarded runs
                    for m in metrics.get('missing_summary', []):
                        row = dict(m)
                        sender_num = _extract_client_num_from_folder(m.get('local_folder'))
                        receiver_num = _extract_client_num_from_folder(m.get('receiver_folder'))
                        row.update({'arch': arch, 'participants': participants, 'run_path': run_path,
                                    'sender_client_num': sender_num, 'receiver_client_num': receiver_num})
                        missing_rows.append(row)

                    # Determine if run should be discarded: any missing frames OR any broken client presence
                    run_failed = False
                    reasons = []
                    # Missing frames
                    for m in metrics.get('missing_summary', []):
                        try:
                            if int(m.get('missing', 0)) > 0:
                                run_failed = True
                                reasons.append('missing_frames')
                                break
                        except Exception:
                            continue

                    # Broken clients from presence records: look for entries matching this run with both traces missing/invalid
                    for p in presence_records:
                        try:
                            if p.get('run_path') == run_path and p.get('arch') == arch:
                                code = p.get('code')
                                local_valid = p.get('local_valid')
                                part_valid = p.get('part_valid')
                                # code 'M' means both traces missing; also treat both invalid as broken
                                if code == 'M' or (local_valid is False and part_valid is False):
                                    run_failed = True
                                    reasons.append('broken_client')
                                    break
                        except Exception:
                            continue

                    if run_failed:
                        # runs where the only failure reason is missing frames are classified as
                        # "failed_runs" but are still accumulated into the final metrics.
                        if len(reasons) == 1 and reasons[0] == 'missing_frames':
                            failed_runs.append({'arch': arch, 'participants': participants, 'run_path': run_path, 'reason': 'missing_frames'})
                            print(f"Marking run as failed (missing frames) but including in results: {arch} participants={participants} run={run_path}")
                            # do not `continue` here; we want to accumulate these metrics below
                        else:
                            discarded_runs.append({'arch': arch, 'participants': participants, 'run_path': run_path, 'reason': ','.join(sorted(set(reasons))) or 'failed'})
                            print(f"Discarding run due to failure: {arch} participants={participants} run={run_path} reason={','.join(sorted(set(reasons))) }")
                            # do NOT accumulate this run's metrics into aggregate results; diagnostics already preserved
                            continue

                    # Otherwise, include the run in aggregated results
                    accumulate_run_results(metrics, arch, participants, run_path,
                                               cpu_results, psnr_stats, missing_rows, resolution_rows, latency_rows, measured_rows,
                                               measured_first_stats, measured_rest_stats, psnr_first_stats, psnr_rest_stats,
                                               latency_first_stats, latency_rest_stats,
                                               client_max_rows)

    # Prepare PSNR mean/std and write diagnostics
    psnr_mean, psnr_std = build_psnr_dfs(psnr_stats)
    # Create and save first-vs-others PSNR and measured-bandwidth plots using
    # aggregators populated during accumulation.
    try:
        plot_psnr_speaker_vs_listeners(psnr_first_stats, psnr_rest_stats, ANALYSIS_FOLDER, scenario)
    except Exception as e:
        print('Failed to create PSNR speaker-vs-listeners plot:', e)
    # Merge per-client max encode/decode info collected during accumulation into presence records
    try:
        # client_max_rows samples were printed during debugging; removed

        for cm in client_max_rows:
            # find matching presence record: prefer exact client_folder match, fall back to numeric client_num
            matched = None
            for p in presence_records:
                try:
                    if p.get('arch') != cm.get('arch') or p.get('run_path') != cm.get('run_path'):
                        continue
                    # match by folder if available
                    if p.get('client_folder') and cm.get('client_folder') and p.get('client_folder') == cm.get('client_folder'):
                        matched = p
                        break
                    # fall back to numeric client id match
                    if p.get('client_num') is not None and cm.get('client_num') is not None and int(p.get('client_num')) == int(cm.get('client_num')):
                        matched = p
                        break
                except Exception:
                    continue
            if matched is not None:
                matched['max_encode_ms'] = cm.get('max_encode_ms')
                matched['max_decode_ms'] = cm.get('max_decode_ms')
                # Also update the unfiltered presence records so diagnostics
                # written from `presence_records_unfiltered` include these values.
                try:
                    for pu in presence_records_unfiltered:
                        try:
                            if pu.get('arch') != cm.get('arch') or pu.get('run_path') != cm.get('run_path'):
                                continue
                            if pu.get('client_folder') and cm.get('client_folder') and pu.get('client_folder') == cm.get('client_folder'):
                                pu['max_encode_ms'] = cm.get('max_encode_ms')
                                pu['max_decode_ms'] = cm.get('max_decode_ms')
                                break
                            if pu.get('client_num') is not None and cm.get('client_num') is not None and int(pu.get('client_num')) == int(cm.get('client_num')):
                                pu['max_encode_ms'] = cm.get('max_encode_ms')
                                pu['max_decode_ms'] = cm.get('max_decode_ms')
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
            else:
                # append a minimal presence record if none exists
                minrec = {'arch': cm.get('arch'), 'participants': cm.get('participants'),
                          'client_folder': cm.get('client_folder'), 'client_num': cm.get('client_num'),
                          'local_present': False, 'part_present': False,
                          'local_valid': False, 'part_valid': False, 'local_count': 0, 'part_count': 0,
                          'code': 'M', 'run_path': cm.get('run_path'),
                          'max_encode_ms': cm.get('max_encode_ms'), 'max_decode_ms': cm.get('max_decode_ms')}
                presence_records.append(minrec)
                # also ensure diagnostics sees this row
                try:
                    presence_records_unfiltered.append(dict(minrec))
                except Exception:
                    pass
    except Exception:
        pass

    try:
        write_diagnostics(presence_records=presence_records_unfiltered or presence_records, missing_records=missing_rows, analysis_folder=ANALYSIS_FOLDER)
    except Exception as e:
        print('Failed to write diagnostics summary:', e)

    try:
        plot_measured_bandwidth_speaker_vs_listeners(measured_first_stats, measured_rest_stats, ANALYSIS_FOLDER, scenario)
    except Exception as e:
        print('Failed to create measured bandwidth speaker-vs-listeners plot:', e)

    try:
        plot_latency_speaker_vs_listeners(latency_first_stats, latency_rest_stats, ANALYSIS_FOLDER, scenario)
    except Exception as e:
        print('Failed to create latency speaker-vs-listeners plot:', e)

    # Handle resolution/bitrate summaries and plots
    process_resolution_rows(resolution_rows, ANALYSIS_FOLDER)

    # Handle latency/encode/decode summaries and plots
    process_latency_rows(latency_rows, arch_map, ANALYSIS_FOLDER)

    # Handle measured docker bandwidth summaries and plots (separate from
    # synthetic bandwidth estimates computed from frame sizes).
    try:
        process_measured_bandwidth_rows(measured_rows, ANALYSIS_FOLDER)
    except Exception as e:
        print('Failed to process measured bandwidth rows:', e)

    # Finalize CPU and PSNR plots
    averaged_cpu = finalize_cpu_and_psnr(cpu_results, psnr_mean, psnr_std, ANALYSIS_FOLDER, scenario)

    # Return per-scenario averaged CPU mapping for root-level aggregation
    # and lists of discarded & failed runs for centralized reporting
    return averaged_cpu, discarded_runs, failed_runs

def main():
    parser = argparse.ArgumentParser(description='Analyze experimental results')
    parser.add_argument('run_folder', help='Path to timestamp folder (e.g., results/2025-10-08-1200)')
    args = parser.parse_args()

    ROOT_FOLDER = args.run_folder
    print(f"Starting parse_results for run folder: {ROOT_FOLDER}")

    # scenarios are subdirectories under the timestamp
    scenarios = [s for s in os.listdir(ROOT_FOLDER) if os.path.isdir(os.path.join(ROOT_FOLDER, s))]
    # exclude any analysis folder that might already exist to avoid double-processing
    scenarios = sorted([s for s in scenarios if s.lower() != 'analysis'])
    print(f"Discovered {len(scenarios)} scenario(s): {scenarios}")
    if not scenarios:
        print('No scenarios found in', ROOT_FOLDER)
        return

    all_results_by_arch = defaultdict(list)
    all_discarded_runs = []
    all_failed_runs = []
    # If there are multiple scenarios, run them in parallel at the scenario level.
    if len(scenarios) > 1:
        max_workers = min(len(scenarios), (os.cpu_count() or 2))
        # Use process-based parallelism to better utilize multiple CPUs for heavy analysis
        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as exc:
            futures = {exc.submit(process_scenario, ROOT_FOLDER, sc): sc for sc in scenarios}
            for fut in concurrent.futures.as_completed(futures):
                sc = futures.get(fut)
                try:
                    res = fut.result()
                    if res:
                        # process_scenario now returns (averaged_cpu, discarded_runs, failed_runs)
                        averaged_cpu = None
                        discarded = []
                        failed = []
                        if isinstance(res, tuple):
                            if len(res) == 3:
                                averaged_cpu, discarded, failed = res
                            elif len(res) == 2:
                                averaged_cpu, discarded = res
                            else:
                                averaged_cpu = res
                        else:
                            averaged_cpu = res
                        if averaged_cpu:
                            for arch, rows in averaged_cpu.items():
                                all_results_by_arch[arch].extend(rows)
                        if discarded:
                            for d in discarded:
                                # attach scenario for context
                                d['scenario'] = sc
                            all_discarded_runs.extend(discarded)
                        if failed:
                            for f in failed:
                                f['scenario'] = sc
                            all_failed_runs.extend(failed)
                except Exception as e:
                    print(f"process_scenario failed for {sc}: {e}")
    else:
        for scenario in scenarios:
            try:
                res = process_scenario(ROOT_FOLDER, scenario)
                if res:
                    averaged_cpu = None
                    discarded = []
                    failed = []
                    if isinstance(res, tuple):
                        if len(res) == 3:
                            averaged_cpu, discarded, failed = res
                        elif len(res) == 2:
                            averaged_cpu, discarded = res
                        else:
                            averaged_cpu = res
                    else:
                        averaged_cpu = res
                    if averaged_cpu:
                        for arch, rows in averaged_cpu.items():
                            all_results_by_arch[arch].extend(rows)
                    if discarded:
                        for d in discarded:
                            d['scenario'] = scenario
                        all_discarded_runs.extend(discarded)
                    if failed:
                        for f in failed:
                            f['scenario'] = scenario
                        all_failed_runs.extend(failed)
            except Exception as e:
                print(f"process_scenario failed for {scenario}: {e}")

    # After all scenarios processed, write a root-level CPU summary
    try:
        write_root_cpu_summary(all_results_by_arch, ROOT_FOLDER)
    except Exception as e:
        print('Failed to write root CPU summary:', e)

    # Aggregate per-scenario latency summaries into a root-level summary
    try:
        write_root_latency_summary(scenarios, ROOT_FOLDER)
    except Exception as e:
        print('Failed to write root latency summary:', e)

    # Aggregate per-scenario measured bandwidth summaries into root-level CSVs/plots
    try:
        write_root_measured_bandwidth_summary(scenarios, ROOT_FOLDER)
    except Exception as e:
        print('Failed to write root measured bandwidth summary:', e)

    # Write a single discarded runs summary into ROOT/analysis/discarded_runs.csv
    try:
        analysis_root = os.path.join(ROOT_FOLDER, 'analysis')
        ensure_dir(analysis_root)
        if all_discarded_runs:
            out_rows = []
            for d in all_discarded_runs:
                out_rows.append({'Architecture': d.get('arch'), 'Participants': d.get('participants'),
                                 'RunPath': d.get('run_path'), 'Reason': d.get('reason'), 'Scenario': d.get('scenario')})
            df_disc = pd.DataFrame(out_rows)
            disc_csv = os.path.join(analysis_root, 'discarded_runs.csv')
            df_disc.to_csv(disc_csv, index=False, sep=';')
            print(f'Wrote discarded runs summary to {disc_csv}')
            # Print concise summary list
            print('Discarded runs:')
            for r in out_rows:
                print(f" - {r.get('Architecture')} participants={r.get('Participants')} run={r.get('RunPath')} reason={r.get('Reason')}")
        else:
            print('No discarded runs detected.')
        # also write failed_runs summary if any
        if all_failed_runs:
            out_rows = []
            for f in all_failed_runs:
                out_rows.append({'Architecture': f.get('arch'), 'Participants': f.get('participants'),
                                 'RunPath': f.get('run_path'), 'Reason': f.get('reason'), 'Scenario': f.get('scenario')})
            df_fail = pd.DataFrame(out_rows)
            fail_csv = os.path.join(analysis_root, 'failed_runs.csv')
            df_fail.to_csv(fail_csv, index=False, sep=';')
            print(f'Wrote failed runs summary to {fail_csv}')
            print('Failed runs:')
            for r in out_rows:
                print(f" - {r.get('Architecture')} participants={r.get('Participants')} run={r.get('RunPath')} reason={r.get('Reason')}")
        else:
            print('No failed runs detected.')
    except Exception as e:
        print('Failed to write discarded/failed runs summaries:', e)

    print('Analysis complete.')


if __name__ == '__main__':
    main()

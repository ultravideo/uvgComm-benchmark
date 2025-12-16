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

import os
import glob
import pandas as pd
import matplotlib.pyplot as plt
import argparse
import matplotlib
import numpy as np
import concurrent.futures
from collections import defaultdict

# Use non-interactive backend for matplotlib
matplotlib.use("Agg")

# limit unmatched-frame debug prints per detect_missing_frames run
_UNMATCHED_PRINT_LIMIT = int(os.environ.get('UVGCOMM_UNMATCHED_PRINT_LIMIT', '5'))
_unmatched_print_count = 0


def read_csv_guess(path, na_values=["", "NA", "null"], dtype=None):
    """Try to read CSV using common separators. Returns DataFrame or None on failure."""
    for sep in [';', ',', '\t']:
        try:
            df = pd.read_csv(path, sep=sep, engine='python', na_values=na_values, dtype=dtype)
            if df is not None and df.shape[1] > 1:
                df.columns = [c.strip() for c in df.columns]
                return df
        except Exception:
            continue
    try:
        # fallback: pandas default
        df = pd.read_csv(path, engine='python', na_values=na_values, dtype=dtype)
        if df is not None:
            df.columns = [c.strip() for c in df.columns]
            return df
    except Exception:
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
            except Exception:
                pass
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
        except Exception:
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
    except Exception:
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
    except Exception:
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
            except Exception:
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
                except Exception:
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
    except Exception:
        pass
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
    MAX_PART_SKIP = 2
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
                if ps == ls_n or ps == ls_n + 1:
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
        if ps == ls or ps == ls + 1:
            delivered += 1
            i += 1
            j += 1
            continue
        # participant lookahead: check the next 1..2 participant entries
        lookahead_matched = False
        for k in (1, 2):
            if (j + k) < n_part:
                ps_k = part_sizes[j + k]
                if ps_k == ls or ps_k == ls + 1:
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
                except Exception:
                    # keep original p_df on any failure
                    pass
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
                except Exception:
                    pass
                start = metadata.get('Start_Timestamp')
                end = metadata.get('End_timestamp') or metadata.get('End_Timestamp')
                if start and end:
                    sel = cpu_df[(cpu_df[ts_col] >= start) & (cpu_df[ts_col] <= end)]
                else:
                    sel = cpu_df
                try:
                    cpu_avg = float(pd.to_numeric(sel[pct_col], errors='coerce').dropna().mean())
                except Exception:
                    cpu_avg = None
    metrics['cpu_avg'] = cpu_avg

    # discover client folders (only client containers) and host folder(s) separately.
    # Host containers never have local/participant CSVs, so avoid scanning them
    # when collecting per-client traces. Keep host folders available for
    # bandwidth parsing by combining them into `bandwidth_folders` below.
    client_folders = sorted([p for p in glob.glob(os.path.join(run_path, 'uvgcomm-client*')) if os.path.isdir(p)])
    host_folders = sorted([p for p in glob.glob(os.path.join(run_path, 'uvgcomm-host')) if os.path.isdir(p)])
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
                except Exception:
                    # if filtering fails, keep original df
                    pass
            participant_by_cname[cname].append({'path': path, 'df': df, 'client_folder': cfolder})

    metrics['local_by_cname'] = local_by_cname
    metrics['participant_by_cname'] = participant_by_cname

    # PSNR: assume PSNR_Y column exists; if not, emit an error message
    psnr_values = []
    for cname, info in local_by_cname.items():
        df = info['df']
        if df is None:
            continue
        if 'PSNR_Y' not in df.columns:
            print(f"ERROR: missing PSNR_Y column for local trace {info.get('path')}")
            continue
        vals = pd.to_numeric(df['PSNR_Y'], errors='coerce').dropna().tolist()
        psnr_values.extend(vals)
    metrics['avg_psnr'] = float(np.mean(psnr_values)) if psnr_values else None
    metrics['psnr_count'] = len(psnr_values)

    # Frame sizes and resolution stats from local frames
    sizes = []
    widths = []
    heights = []
    encode_times = []
    # per-client max encode time (ms)
    max_encode_by_client = {}
    # per-client max decode time (ms)
    max_decode_by_client = {}
    out_total_bytes = 0
    out_min_ts = None
    out_max_ts = None
    for cname, info in local_by_cname.items():
        df = info['df']
        if df is None:
            continue
        vals, sc = extract_numeric_list(df, ['Size(Bytes)', 'Size'], dtype=int)
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
            # record per-client max encode time
            try:
                cfolder = info.get('client_folder')
                cnum = _extract_client_num_from_folder(cfolder)
                if cnum is None:
                    # fallback: use folder basename index
                    cnum = None
                cur = max(encvals) if encvals else None
                if cur is not None:
                    max_encode_by_client[cnum] = max(cur, float(max_encode_by_client.get(cnum, float('-inf')))) if cnum in max_encode_by_client else cur
            except Exception:
                pass
    metrics['avg_frame_size'] = float(np.mean(sizes)) if sizes else None
    metrics['avg_width'] = float(np.mean(widths)) if widths else None
    metrics['avg_height'] = float(np.mean(heights)) if heights else None
    metrics['avg_encode_ms'] = float(np.mean(encode_times)) if encode_times else None

    # Latency / decode times / participant-side stats
    latencies = []
    decode_times = []
    participant_sizes = []
    in_total_bytes = 0
    in_min_ts = None
    in_max_ts = None
    for cname, plist in participant_by_cname.items():
        for info in plist:
            df = info['df']
            if df is None:
                continue
            lvals, _ = extract_numeric_list(df, ['Latency(ms)', 'Latency', 'latency'], dtype=float)
            if lvals:
                latencies.extend(lvals)

            dvals, _ = extract_numeric_list(df, ['DecodeTime(ms)', 'DecodeTime', 'DecodeTimeMs'], dtype=float)
            if dvals:
                decode_times.extend(dvals)
                # record per-client max decode time
                try:
                    cfolder = info.get('client_folder')
                    cnum = _extract_client_num_from_folder(cfolder)
                    if cnum is None:
                        cnum = None
                    curd = max(dvals) if dvals else None
                    if curd is not None:
                        max_decode_by_client[cnum] = max(curd, float(max_decode_by_client.get(cnum, float('-inf')))) if cnum in max_decode_by_client else curd
                except Exception:
                    pass

            pvals, _ = extract_numeric_list(df, ['Size(Bytes)', 'Size'], dtype=int)
            if pvals:
                participant_sizes.extend(pvals)
                in_total_bytes += sum(pvals)

            mn_ts, mx_ts = get_min_max_ts(df)
            if mn_ts is not None:
                in_min_ts = mn_ts if in_min_ts is None else min(in_min_ts, mn_ts)
            if mx_ts is not None:
                in_max_ts = mx_ts if in_max_ts is None else max(in_max_ts, mx_ts)

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
    except Exception:
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
    except Exception:
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
    c_out_bps_meas, c_in_bps_meas, h_out_bps_meas, h_in_bps_meas = parse_measured_bandwidth(bandwidth_folders, start_ts, end_ts)
    metrics['measured_outgoing_bps_per_client'] = c_out_bps_meas
    metrics['measured_incoming_bps_per_client'] = c_in_bps_meas
    metrics['measured_host_outgoing_bps'] = h_out_bps_meas
    metrics['measured_host_incoming_bps'] = h_in_bps_meas

    return metrics


def plot_cpu(results_by_arch, analysis_folder, scenario):
    # results_by_arch: dict[arch] -> list of (n_clients, cpu_avg)
    plt.figure(figsize=(6,4))
    markers = ['o', 's', '^', 'D', 'v', 'P', 'X']
    for i, (arch, rows) in enumerate(results_by_arch.items()):
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
            plt.plot(xs_arr[mask], ys_arr[mask], marker=m, linestyle=ls, label=arch)
    plt.xlabel('Number of Participants')
    plt.ylabel('Average Total CPU %')
    plt.title(f'CPU usage - {scenario}')
    # nicer grid: horizontal lines only
    plt.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
    plt.legend(prop={'size': 10})
    # enforce y-axis 0-100 and ticks every 10%
    plt.ylim(0, 100)
    plt.yticks(np.arange(0, 101, 10))
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
                except Exception:
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
                except Exception:
                    rx_mean = None
            if tx_mean is None and tx_bps_col is not None:
                try:
                    tx_mean = float(pd.to_numeric(df[tx_bps_col], errors='coerce').dropna().mean()) * 8.0
                except Exception:
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
    except Exception:
        pass

    c_out_mean = float(np.mean(measured_out_clients)) if measured_out_clients else None
    c_in_mean = float(np.mean(measured_in_clients)) if measured_in_clients else None
    h_out_mean = float(np.mean(measured_out_host)) if measured_out_host else None
    h_in_mean = float(np.mean(measured_in_host)) if measured_in_host else None
    return c_out_mean, c_in_mean, h_out_mean, h_in_mean


def plot_psnr(mean_df, std_df, analysis_folder, scenario):
    plt.figure(figsize=(6,4))
    markers = ['o', 's', '^', 'D', 'v', 'P', 'X']
    for i, col in enumerate(mean_df.columns):
        plt.plot(mean_df.index, mean_df[col], marker=markers[i % len(markers)], label=col)
        if col in std_df.columns:
            std = std_df[col].fillna(0)
            plt.fill_between(mean_df.index, mean_df[col] - std, mean_df[col] + std, alpha=0.15)
    plt.xlabel('Number of Participants')
    plt.ylabel('Average PSNR (Y)')
    plt.title(f'Average PSNR - {scenario}')
    plt.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
    # set x-axis to whole numbers
    try:
        xt = [int(x) for x in mean_df.index]
        plt.xticks(xt)
    except Exception:
        pass
    # Force y-limits to 0..50 and add horizontal 8-bit max line before legend so it's shown
    plt.ylim(0, 50)
    plt.axhline(48.131, color='gray', linestyle=':', linewidth=2.0, label='Max PSNR (8-bit)', zorder=5)
    plt.legend(prop={'size': 10})
    plt.tight_layout()
    out = os.path.join(analysis_folder, 'psnr.svg')
    plt.savefig(out)
    plt.close()
    print('Saved PSNR plot:', out)


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
        except Exception:
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
    except Exception:
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
    except Exception:
        parts_i = 999999

    runp = r.get('RunPath') or r.get('run_path') or ''
    run_base = os.path.basename(runp) if runp else ''
    digits = ''.join([c for c in run_base if c.isdigit()])
    try:
        run_i = int(digits) if digits else (0 if run_base else 999999)
    except Exception:
        run_i = 999999

    client = r.get('Client') if 'Client' in r else r.get('client')
    try:
        client_i = int(client) if client is not None else 999999
    except Exception:
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
            except Exception:
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
    except Exception:
        pass
    return None


def write_diagnostics(presence_records, missing_records, analysis_folder):
    """Write a simple per-run diagnostics CSV with one row per run+client.

    Columns (in order):
      Architecture, Participants, RunPath, Client, LocalCSVCount, ParticipantCSVCount, FramesLost, Status

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
    diag_csv = os.path.join(analysis_folder, 'diagnostics_summary.csv')
    diag_df.to_csv(diag_csv, index=False, sep=';')
    print('Wrote diagnostics summary to', diag_csv)


def setup_analysis_folders(ROOT_FOLDER, scenario):
    """Create and return the per-scenario analysis folder path."""
    base_analysis = os.path.join(ROOT_FOLDER, 'analysis')
    ensure_dir(base_analysis)
    scenario_analysis = os.path.join(base_analysis, scenario)
    ensure_dir(scenario_analysis)
    return scenario_analysis


def collect_presence_records_for_run(run_path, arch, participants):
    """Return a list of presence record dicts for given run_path (same logic previously inlined).

    `participants` is the number of participants for this run.
    """
    records = []
    client_folders = sorted([p for p in glob.glob(os.path.join(run_path, 'uvgcomm-client*')) if os.path.isdir(p)])
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
            except Exception:
                local_valid = False
        if part_present:
            try:
                part_df = read_csv_guess(part_paths[0])
                part_valid = part_df is not None
            except Exception:
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
        records.append({'arch': arch, 'participants': participants, 'client_num': client_num,
                        'local_present': local_present, 'part_present': part_present,
                        'local_valid': local_valid, 'part_valid': part_valid,
                        'local_count': local_count, 'part_count': part_count,
                        'code': code, 'run_path': run_path})
    return records


def accumulate_run_results(metrics, arch, participants, run_path,
                           cpu_results, psnr_stats, missing_rows, resolution_rows, latency_rows, measured_rows,
                           client_max_rows):
    """Accumulate per-run metrics into the provided containers (mutates lists/dicts).

    Mirrors the original inlined logic.
    """
    cpu_results[arch].append((participants, metrics.get('cpu_avg')))

    # PSNR per run - we have avg_psnr and count. Keep mean and std as single-run values.
    psnr_val = metrics.get('avg_psnr')
    if psnr_val is not None:
        if participants not in psnr_stats[arch]:
            psnr_stats[arch][participants] = []
        psnr_stats[arch][participants].append(psnr_val)

    # missing frames summary appended (attach client_num inferred from receiver_folder)
    for m in metrics.get('missing_summary', []):
        row = dict(m)
        sender_num = _extract_client_num_from_folder(m.get('local_folder'))
        receiver_num = _extract_client_num_from_folder(m.get('receiver_folder'))
        row.update({'arch': arch, 'participants': participants, 'run_path': run_path,
                    'sender_client_num': sender_num, 'receiver_client_num': receiver_num})
        missing_rows.append(row)

    # resolution/frame size + bitrates
    row_rs = {'arch': arch, 'participants': participants, 'avg_width': metrics.get('avg_width'),
              'avg_height': metrics.get('avg_height'), 'avg_frame_size': metrics.get('avg_frame_size'),
              'outgoing_bps': metrics.get('outgoing_bps'), 'incoming_bps': metrics.get('incoming_bps')}
    resolution_rows.append(row_rs)

    # Measured bandwidth collected from per-container monitoring (clients vs host)
    measured_row = {
        'arch': arch,
        'participants': participants,
        'measured_outgoing_bps_clients': metrics.get('measured_outgoing_bps_per_client'),
        'measured_incoming_bps_clients': metrics.get('measured_incoming_bps_per_client'),
        'measured_outgoing_bps_host': metrics.get('measured_host_outgoing_bps'),
        'measured_incoming_bps_host': metrics.get('measured_host_incoming_bps')
    }
    measured_rows.append(measured_row)

    # latency/encode/decode
    latency_rows.append({'arch': arch, 'participants': participants,
                         'avg_latency_ms': metrics.get('avg_latency_ms'),
                         'avg_encode_ms': metrics.get('avg_encode_ms'),
                         'avg_decode_ms': metrics.get('avg_decode_ms')})
    # collect per-client max encode/decode reported by analyze_run (may use None keys)
    try:
        me = metrics.get('max_encode_by_client', {}) or {}
        md = metrics.get('max_decode_by_client', {}) or {}
        # union of client keys
        keys = set(list(me.keys()) + list(md.keys()))
        for k in keys:
            client_max_rows.append({'arch': arch, 'participants': participants, 'run_path': run_path,
                                     'client_num': k, 'max_encode_ms': me.get(k), 'max_decode_ms': md.get(k)})
    except Exception:
        pass


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
        except Exception:
            return None

    out_rows = []
    for _, r in res_df.iterrows():
        parts = int(r.get('participants')) if pd.notna(r.get('participants')) else None
        out_mbps = _bps_to_mbps_per_client(r.get('outgoing_bps'), parts)
        in_mbps = _bps_to_mbps_per_client(r.get('incoming_bps'), parts)
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
    fig, ax = plt.subplots(figsize=(8,4))
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
        except Exception:
            try:
                gagg_mean = g.set_index(participants_col)[ycols]
                gagg_std = gagg_mean * 0.0
            except Exception:
                continue

        x = list(gagg_mean.index)
        for j, ycol in enumerate(ycols):
            y = gagg_mean[ycol]
            ystd = gagg_std[ycol]
            ls = linestyles[j % len(linestyles)]
            mk = marker if j == 0 else 'x'
            try:
                max_val = max(max_val, float(np.nanmax((y + ystd).fillna(0.0))))
            except Exception:
                pass
            ax.plot(x, y, marker=mk, linestyle=ls, label=f'{arch_name} {labels[j]}', color=color)
            try:
                ax.fill_between(x, (y - ystd), (y + ystd), color=color, alpha=0.12)
            except Exception:
                pass

    ax.set_xlabel('Participants')
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(axis='y', linestyle='--', linewidth=0.6, alpha=0.7)
    try:
        xt = sorted(set(int(x) for x in df[participants_col].dropna().unique()))
        ax.set_xticks(xt)
    except Exception:
        pass
    if max_val is not None and max_val > 0:
        ax.set_ylim(0, max(max_val * 1.05, 0.1))
    else:
        ax.set_ylim(0, 1)
    ax.legend(fontsize=8)
    plt.tight_layout()
    out_path = os.path.join(ANALYSIS_FOLDER, out_filename)
    fig.savefig(out_path)
    plt.close(fig)
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
        'avg_latency_ms': 'Avg. Latency(ms)', 'avg_encode_ms': 'Avg. Encoding Time (ms)',
        'avg_decode_ms': 'Avg. Decoding Time (ms)'
    })
    lat_csv = os.path.join(ANALYSIS_FOLDER, f'latency_summary.csv')
    lat_out.to_csv(lat_csv, index=False, sep=';')
    print('Wrote latency summary to', lat_csv)

    try:
        expected_runs = {}
        for arch, entries in arch_map.items():
            for n, _ in entries:
                expected_runs[(arch, n)] = expected_runs.get((arch, n), 0) + 1

        agg_rows = []
        grouped = lat_df.groupby(['arch', 'participants'])
        for (arch, parts), g in grouped:
            try:
                parts_i = int(parts)
            except Exception:
                parts_i = parts
            key = (arch, parts_i)
            exp = expected_runs.get(key, 1)
            if 'avg_latency_ms' in g:
                tvals = list(pd.to_numeric(g['avg_latency_ms'], errors='coerce').fillna(999).astype(float).tolist())
            else:
                tvals = []
            if len(tvals) < exp:
                tvals.extend([999.0] * (exp - len(tvals)))
            mean_t = float(np.mean(tvals)) if tvals else 999.0

            mean_e = None
            mean_d = None
            if len(g) == exp and 'avg_encode_ms' in g and 'avg_decode_ms' in g:
                if not g['avg_encode_ms'].isnull().any() and not g['avg_decode_ms'].isnull().any():
                    mean_e = float(np.mean(g['avg_encode_ms']))
                    mean_d = float(np.mean(g['avg_decode_ms']))

            agg_rows.append({'arch': arch, 'participants': parts_i, 'mean_encode': mean_e,
                             'mean_decode': mean_d, 'mean_total': mean_t})

        if agg_rows:
            fig, ax = plt.subplots(figsize=(8,4))
            labels = []
            enc = []
            dec = []
            oth = []
            agg_rows = sorted(agg_rows, key=lambda x: (x['arch'], x['participants']))
            for r in agg_rows:
                labels.append(f"{r['arch']}-{int(r['participants'])}")
                t = r['mean_total']
                e = r.get('mean_encode')
                d = r.get('mean_decode')
                enc_present = e is not None
                dec_present = d is not None
                enc_val = float(e) if enc_present else 0.0
                dec_val = float(d) if dec_present else 0.0
                o = max(0.0, float(t) - enc_val - dec_val)
                enc.append(enc_val if enc_present else 0.0)
                dec.append(min(dec_val, 999.0) if dec_present else 0.0)
                oth.append(o)
            x = np.arange(len(labels))
            ax.bar(x, enc, label='Encoding')
            ax.bar(x, dec, bottom=enc, label='Decoding')
            bottom_ed = np.array(enc) + np.array(dec)
            ax.bar(x, oth, bottom=bottom_ed, label='Other')
            ax.set_xticks(x)
            ax.set_xticklabels(labels, rotation=45, ha='right')
            ax.set_ylabel('Time (ms)')
            ax.set_title('Latency breakdown')
            ax.legend()
            plt.tight_layout()
            bar_out = os.path.join(ANALYSIS_FOLDER, 'latency_breakdown.svg')
            fig.savefig(bar_out)
            plt.close(fig)
            print('Wrote latency breakdown plot to', bar_out)
        else:
            fig, ax = plt.subplots(figsize=(6,3))
            ax.text(0.5, 0.5, 'No complete aggregated latency data available to plot', ha='center', va='center')
            ax.axis('off')
            bar_out = os.path.join(ANALYSIS_FOLDER, 'latency_breakdown.svg')
            fig.savefig(bar_out)
            plt.close(fig)
            print('Wrote placeholder latency breakdown plot to', bar_out)
    except Exception as e:
        print('Failed to create latency breakdown plot:', e)


def finalize_cpu_and_psnr(cpu_results, psnr_mean, psnr_std, ANALYSIS_FOLDER, scenario):
    """Aggregate CPU results and produce CPU/PSNR plots."""
    averaged_cpu = {}
    for arch, rows in cpu_results.items():
        by_n = defaultdict(list)
        for participants, val in rows:
            if val is not None:
                by_n[participants].append(val)
        averaged = []
        for participants, vals in sorted(by_n.items()):
            try:
                averaged.append((participants, float(np.mean(vals))))
            except Exception:
                averaged.append((participants, None))
        averaged_cpu[arch] = averaged
    plot_cpu(averaged_cpu, ANALYSIS_FOLDER, scenario)

    if not psnr_mean.empty:
        plot_psnr(psnr_mean, psnr_std, ANALYSIS_FOLDER, scenario)


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
                 'avg_latency_ms', 'avg_decode_ms', 'avg_part_frame_size', 'missing_summary',
                 'measured_outgoing_bps_per_client', 'measured_incoming_bps_per_client',
                 'measured_host_outgoing_bps', 'measured_host_incoming_bps',
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


def process_scenario(ROOT_FOLDER, scenario):
    """Process one scenario directory: analyze architectures and write CSVs/plots."""
    scenario_folder = os.path.join(ROOT_FOLDER, scenario)
    ANALYSIS_FOLDER = setup_analysis_folders(ROOT_FOLDER, scenario)

    arch_map = build_arch_map(scenario_folder)

    # compute per-architecture aggregated metrics
    cpu_results = defaultdict(list)  # arch -> list of (participants, cpu_avg) across all runs
    psnr_stats = defaultdict(dict)
    missing_rows = []
    resolution_rows = []
    measured_rows = []
    latency_rows = []
    client_max_rows = []
    presence_records = []

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
                    records = fut.result()
                except Exception as e:
                    print(f"[presence] failed for {arch}-{participants} {run_path}: {e}")
                    records = []
                run_counter += 1
                presence_records.extend(records)
                print(f"[presence {run_counter}/{total_runs}] collected {len(records)} record(s) for {arch}-{participants} run {os.path.basename(run_path)}")
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
                    if metrics is None:
                        continue
                    accumulate_run_results(metrics, arch, participants, run_path,
                                           cpu_results, psnr_stats, missing_rows, resolution_rows, latency_rows, measured_rows,
                                           client_max_rows)

    # Prepare PSNR mean/std and write diagnostics
    psnr_mean, psnr_std = build_psnr_dfs(psnr_stats)
    # Merge per-client max encode/decode info collected during accumulation into presence records
    try:
        for cm in client_max_rows:
            # find matching presence record
            matched = None
            for p in presence_records:
                if p.get('arch') == cm.get('arch') and p.get('run_path') == cm.get('run_path') and (p.get('client_num') == cm.get('client_num')):
                    matched = p
                    break
            if matched is not None:
                matched['max_encode_ms'] = cm.get('max_encode_ms')
                matched['max_decode_ms'] = cm.get('max_decode_ms')
            else:
                # append a minimal presence record if none exists
                presence_records.append({'arch': cm.get('arch'), 'participants': cm.get('participants'),
                                         'client_num': cm.get('client_num'), 'local_present': False, 'part_present': False,
                                         'local_valid': False, 'part_valid': False, 'local_count': 0, 'part_count': 0,
                                         'code': 'M', 'run_path': cm.get('run_path'),
                                         'max_encode_ms': cm.get('max_encode_ms'), 'max_decode_ms': cm.get('max_decode_ms')})
    except Exception:
        pass

    try:
        write_diagnostics(presence_records=presence_records, missing_records=missing_rows, analysis_folder=ANALYSIS_FOLDER)
    except Exception as e:
        print('Failed to write diagnostics summary:', e)

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
    finalize_cpu_and_psnr(cpu_results, psnr_mean, psnr_std, ANALYSIS_FOLDER, scenario)

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

    # If there are multiple scenarios, run them in parallel at the scenario level.
    if len(scenarios) > 1:
        max_workers = min(len(scenarios), (os.cpu_count() or 2))
        # Use process-based parallelism to better utilize multiple CPUs for heavy analysis
        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as exc:
            futures = {exc.submit(process_scenario, ROOT_FOLDER, sc): sc for sc in scenarios}
            for fut in concurrent.futures.as_completed(futures):
                sc = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    print(f"process_scenario failed for {sc}: {e}")
    else:
        for scenario in scenarios:
            process_scenario(ROOT_FOLDER, scenario)
    print('Analysis complete.')


if __name__ == '__main__':
    main()

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
from collections import defaultdict

# Use non-interactive backend for matplotlib
matplotlib.use("Agg")


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


def analyze_run(run_path):
    """Analyze a single run directory (contains cpu_usage.csv, metadata.txt, uvgcomm-client* folders).
    Returns a dictionary of aggregated metrics.
    """
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

    # discover client folders
    client_folders = sorted([p for p in glob.glob(os.path.join(run_path, 'uvgcomm-*')) if os.path.isdir(p)])

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
            if start_ts and end_ts:
                # find a timestamp column
                tscol = None
                for c in df.columns:
                    if 'timestamp' in c.lower() or 'time' == c.lower() or 'timestamp_ms' in c.lower():
                        tscol = c
                        break
                if tscol is None:
                    tscol = df.columns[0]
                try:
                    df_ts = pd.to_numeric(df[tscol], errors='coerce')
                    df = df[(df_ts >= start_ts) & (df_ts <= end_ts)]
                except Exception:
                    pass
            local_by_cname[cname] = {'path': path, 'df': df, 'client_folder': cfolder}
        # participant files
        for path in glob.glob(os.path.join(cfolder, 'participant_*.csv')):
            base = os.path.basename(path)
            cname = base.split('participant_', 1)[1].rsplit('.', 1)[0]
            df = read_csv_guess(path)
            if df is None:
                continue
            # filter by timestamp range if possible
            if start_ts and end_ts:
                tscol = None
                for c in df.columns:
                    if 'timestamp' in c.lower() or 'time' == c.lower() or 'timestamp_ms' in c.lower():
                        tscol = c
                        break
                if tscol is None:
                    tscol = df.columns[0]
                try:
                    df_ts = pd.to_numeric(df[tscol], errors='coerce')
                    df = df[(df_ts >= start_ts) & (df_ts <= end_ts)]
                except Exception:
                    pass
            participant_by_cname[cname].append({'path': path, 'df': df, 'client_folder': cfolder})

    metrics['local_by_cname'] = local_by_cname
    metrics['participant_by_cname'] = participant_by_cname

    # PSNR: use PSNR_Y column if present, else try PSNR
    psnr_values = []
    for cname, info in local_by_cname.items():
        df = info['df']
        col = None
        for c in df.columns:
            if c.strip().upper().startswith('PSNR_Y'):
                col = c
                break
        if col is None:
            for c in df.columns:
                if 'psnr' in c.lower():
                    col = c
                    break
        if col is None:
            continue
        vals = pd.to_numeric(df[col], errors='coerce').dropna().tolist()
        psnr_values.extend(vals)
    metrics['avg_psnr'] = float(np.mean(psnr_values)) if psnr_values else None
    metrics['psnr_count'] = len(psnr_values)

    # Frame sizes and resolution stats from local frames
    sizes = []
    widths = []
    heights = []
    encode_times = []
    out_total_bytes = 0
    out_min_ts = None
    out_max_ts = None
    for cname, info in local_by_cname.items():
        df = info['df']
        for col in ['Size(Bytes)', 'Size']:
            if col in df.columns:
                vals = pd.to_numeric(df[col], errors='coerce').dropna().tolist()
                sizes.extend(vals)
                out_total_bytes += sum(vals)
                break
        # collect timestamps for duration
        for c in df.columns:
            if 'timestamp' in c.lower() or 'time' == c.lower() or 'timestamp_ms' in c.lower():
                try:
                    tsvals = pd.to_numeric(df[c], errors='coerce').dropna().astype(int).values
                    if tsvals.size:
                        mn = int(tsvals.min())
                        mx = int(tsvals.max())
                        out_min_ts = mn if out_min_ts is None else min(out_min_ts, mn)
                        out_max_ts = mx if out_max_ts is None else max(out_max_ts, mx)
                except Exception:
                    pass
                break
        for col in ['Width', 'width', 'W']:
            if col in df.columns:
                widths.extend(pd.to_numeric(df[col], errors='coerce').dropna().tolist())
                break
        for col in ['Height', 'height', 'H']:
            if col in df.columns:
                heights.extend(pd.to_numeric(df[col], errors='coerce').dropna().tolist())
                break
        for col in ['EncodeTime(ms)', 'EncodeTime', 'EncodeTimeMs', 'EncodeTime (ms)']:
            if col in df.columns:
                encode_times.extend(pd.to_numeric(df[col], errors='coerce').dropna().tolist())
                break
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
            for col in ['Latency(ms)', 'Latency', 'latency']:
                if col in df.columns:
                    latencies.extend(pd.to_numeric(df[col], errors='coerce').dropna().tolist())
                    break
            for col in ['DecodeTime(ms)', 'DecodeTime', 'DecodeTimeMs']:
                if col in df.columns:
                    decode_times.extend(pd.to_numeric(df[col], errors='coerce').dropna().tolist())
                    break
            for col in ['Size(Bytes)', 'Size']:
                if col in df.columns:
                    vals = pd.to_numeric(df[col], errors='coerce').dropna().tolist()
                    participant_sizes.extend(vals)
                    in_total_bytes += sum(vals)
                    break
            # collect timestamps
            for c in df.columns:
                if 'timestamp' in c.lower() or 'time' == c.lower() or 'timestamp_ms' in c.lower():
                    try:
                        tsvals = pd.to_numeric(df[c], errors='coerce').dropna().astype(int).values
                        if tsvals.size:
                            mn = int(tsvals.min())
                            mx = int(tsvals.max())
                            in_min_ts = mn if in_min_ts is None else min(in_min_ts, mn)
                            in_max_ts = mx if in_max_ts is None else max(in_max_ts, mx)
                    except Exception:
                        pass
                    break

    # compute outgoing/incoming average bitrates (bps) using collected bytes and timestamp ranges
    out_bps = None
    if out_total_bytes and out_min_ts is not None and out_max_ts is not None and out_max_ts > out_min_ts:
        dur_s = (out_max_ts - out_min_ts) / 1000.0
        out_bps = (out_total_bytes * 8.0) / dur_s if dur_s > 0 else None
    in_bps = None
    if in_total_bytes and in_min_ts is not None and in_max_ts is not None and in_max_ts > in_min_ts:
        dur_s = (in_max_ts - in_min_ts) / 1000.0
        in_bps = (in_total_bytes * 8.0) / dur_s if dur_s > 0 else None
    metrics['outgoing_bps'] = out_bps
    metrics['incoming_bps'] = in_bps
    metrics['avg_latency_ms'] = float(np.mean(latencies)) if latencies else None
    metrics['avg_decode_ms'] = float(np.mean(decode_times)) if decode_times else None
    metrics['avg_part_frame_size'] = float(np.mean(participant_sizes)) if participant_sizes else None

    # Missing frame detection: for each cname where we have both local and participant traces, match by nearest timestamp
    missing_summary = []
    for cname, local_info in local_by_cname.items():
        local_df = local_info['df']
        # find timestamp column in local
        ts_local_col = None
        for c in local_df.columns:
            if 'timestamp' in c.lower() or 'time' == c.lower() or 'timestamp_ms' in c.lower():
                ts_local_col = c
                break
        if ts_local_col is None:
            # try first column
            ts_local_col = local_df.columns[0]
        local_ts = pd.to_numeric(local_df[ts_local_col], errors='coerce').dropna().astype(int).values

        # for each participant that has this cname
        if cname not in participant_by_cname:
            continue
        for pinfo in participant_by_cname[cname]:
            p_df = pinfo['df']
            ts_part_col = None
            for c in p_df.columns:
                if 'timestamp' in c.lower() or 'time' == c.lower() or 'timestamp_ms' in c.lower():
                    ts_part_col = c
                    break
            if ts_part_col is None:
                ts_part_col = p_df.columns[0]
            part_ts = pd.to_numeric(p_df[ts_part_col], errors='coerce').dropna().astype(int).values

            # match each local timestamp to nearest participant timestamp within tolerance
            # tolerance chosen as 200 ms (can be adjusted)
            tol = 200
            delivered = 0
            partial = 0
            for lt in local_ts:
                # find any part_ts within tol
                diffs = np.abs(part_ts - lt)
                if diffs.size == 0:
                    # no frames received at all
                    continue
                min_idx = np.argmin(diffs)
                if diffs[min_idx] <= tol:
                    delivered += 1
                else:
                    # not delivered
                    pass
            total_local = len(local_ts)
            missing = max(0, total_local - delivered)
            pct_missing = 100.0 * missing / total_local if total_local > 0 else None
            missing_summary.append({'cname': cname, 'receiver_folder': pinfo.get('client_folder'),
                                     'total_local_frames': total_local, 'delivered': delivered,
                                     'missing': missing, 'pct_missing': pct_missing})

    metrics['missing_summary'] = missing_summary

    return metrics


def plot_cpu(results_by_arch, analysis_folder, scenario):
    # results_by_arch: dict[arch] -> list of (n_clients, cpu_avg)
    plt.figure(figsize=(6,4))
    for arch, rows in results_by_arch.items():
        rows_sorted = sorted(rows, key=lambda x: x[0])
        xs = [r[0] for r in rows_sorted]
        ys = [r[1] for r in rows_sorted]
        # coerce to numpy arrays and handle missing values
        xs_arr = np.array(xs, dtype=float)
        ys_arr = np.array(ys, dtype=float)
        # plot only points where x is finite
        mask = ~np.isnan(xs_arr)
        if np.any(mask):
            plt.plot(xs_arr[mask], ys_arr[mask], marker='o', label=arch)
    plt.xlabel('Number of Participants')
    plt.ylabel('Average CPU % (server)')
    plt.title(f'CPU usage - {scenario}')
    plt.grid(True)
    plt.legend(fontsize=10)
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
    out = os.path.join(analysis_folder, f'{scenario}_cpu.svg')
    plt.savefig(out)
    plt.close()
    print('Saved CPU plot:', out)


def plot_psnr(mean_df, std_df, analysis_folder, scenario):
    plt.figure(figsize=(6,4))
    for col in mean_df.columns:
        plt.plot(mean_df.index, mean_df[col], marker='o', label=col)
        if col in std_df.columns:
            std = std_df[col].fillna(0)
            plt.fill_between(mean_df.index, mean_df[col] - std, mean_df[col] + std, alpha=0.15)
    plt.xlabel('Number of Participants')
    plt.ylabel('Average PSNR (Y)')
    plt.title(f'Average PSNR - {scenario}')
    plt.grid(True)
    plt.legend(fontsize=10)
    # set x-axis to whole numbers
    try:
        xt = [int(x) for x in mean_df.index]
        plt.xticks(xt)
    except Exception:
        pass
    # enforce y limits 0..50 (PSNR range for display)
    try:
        plt.ylim(0, 50)
    except Exception:
        pass
    # mark maximum 8-bit PSNR as dotted line
    try:
        plt.axhline(48.131, color='gray', linestyle=':', linewidth=1.2, label='8-bit max (48.131)')
    except Exception:
        pass
    # expand y-limits for visibility: add margin, ensure reasonable upper bound
    vals = mean_df.values.flatten()
    vals = vals[~np.isnan(vals)] if vals.size else np.array([])
    if vals.size:
        vmin = float(np.min(vals))
        vmax = float(np.max(vals))
        ymin = max(0, vmin - 5)
        ymax = max(40, vmax + 5)
        plt.ylim(ymin, ymax)
    plt.tight_layout()
    out = os.path.join(analysis_folder, f'{scenario}_psnr.svg')
    plt.savefig(out)
    plt.close()
    print('Saved PSNR plot:', out)

def main():
    parser = argparse.ArgumentParser(description='Analyze experimental results')
    parser.add_argument('run_folder', help='Path to timestamp folder (e.g., results/2025-10-08-1200)')
    args = parser.parse_args()

    ROOT_FOLDER = args.run_folder

    # scenarios are subdirectories under the timestamp
    scenarios = [s for s in os.listdir(ROOT_FOLDER) if os.path.isdir(os.path.join(ROOT_FOLDER, s))]
    # exclude any analysis folder that might already exist to avoid double-processing
    scenarios = sorted([s for s in scenarios if s.lower() != 'analysis'])
    if not scenarios:
        print('No scenarios found in', ROOT_FOLDER)
        return

    for scenario in scenarios:
        scenario_folder = os.path.join(ROOT_FOLDER, scenario)
        # create analysis folder under the run root and then per-scenario subfolder
        base_analysis = os.path.join(ROOT_FOLDER, 'analysis')
        ensure_dir(base_analysis)
        scenario_analysis = os.path.join(base_analysis, scenario)
        ensure_dir(scenario_analysis)
        ANALYSIS_FOLDER = scenario_analysis

        # detect architecture-client folders (e.g., SFU-2)
        arch_folders = [p for p in glob.glob(os.path.join(scenario_folder, '*')) if os.path.isdir(p)]
        arch_map = defaultdict(list)  # arch -> list of (nclients, run_path)
        for af in arch_folders:
            base = os.path.basename(af)
            if '-' not in base:
                continue
            try:
                arch, clients = base.rsplit('-', 1)
                n = int(clients)
            except Exception:
                continue
            # find latest run inside this folder
            run_path = find_latest_run(af) or af
            arch_map[arch].append((n, run_path))

        # compute per-architecture aggregated metrics
        cpu_results = {}
        psnr_stats = {}
        missing_rows = []
        resolution_rows = []
        frame_size_rows = []
        latency_rows = []
        presence_rows = []

        for arch, entries in arch_map.items():
            cpu_results[arch] = []
            psnr_stats[arch] = {}
            for n, run_path in sorted(entries, key=lambda x: x[0]):
                # presence checks for debugging: per client folder, check local/participant files
                client_folders = sorted([p for p in glob.glob(os.path.join(run_path, 'uvgcomm-*')) if os.path.isdir(p)])
                # assign client numbers
                for idx, cf in enumerate(client_folders, start=1):
                    base = os.path.basename(cf)
                    try:
                        # try to extract numeric suffix
                        client_num = int(''.join([c for c in base if c.isdigit()]))
                    except Exception:
                        client_num = idx
                    local_paths = glob.glob(os.path.join(cf, 'local_*.csv'))
                    part_paths = glob.glob(os.path.join(cf, 'participant_*.csv'))
                    local_present = bool(local_paths)
                    part_present = bool(part_paths)
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
                    presence_rows.append({'arch_client': f"{arch}-{n}", 'client': client_num,
                                          'local_present': local_present, 'part_present': part_present,
                                          'local_valid': local_valid, 'part_valid': part_valid})
                metrics = analyze_run(run_path)
                cpu_results[arch].append((n, metrics.get('cpu_avg')))

                # PSNR per run - we have avg_psnr and count. Keep mean and std as single-run values.
                psnr_val = metrics.get('avg_psnr')
                if psnr_val is not None:
                    if n not in psnr_stats[arch]:
                        psnr_stats[arch][n] = []
                    psnr_stats[arch][n].append(psnr_val)

                # missing frames summary appended
                for m in metrics.get('missing_summary', []):
                    row = dict(m)
                    row.update({'arch': arch, 'participants': n, 'run_path': run_path})
                    missing_rows.append(row)

                # resolution/frame size + bitrates
                row_rs = {'arch': arch, 'participants': n, 'avg_width': metrics.get('avg_width'),
                          'avg_height': metrics.get('avg_height'), 'avg_frame_size': metrics.get('avg_frame_size'),
                          'outgoing_bps': metrics.get('outgoing_bps'), 'incoming_bps': metrics.get('incoming_bps')}
                resolution_rows.append(row_rs)
                frame_size_rows.append(row_rs)

                # latency/encode/decode
                latency_rows.append({'arch': arch, 'participants': n,
                                     'avg_latency_ms': metrics.get('avg_latency_ms'),
                                     'avg_encode_ms': metrics.get('avg_encode_ms'),
                                     'avg_decode_ms': metrics.get('avg_decode_ms')})

        # Prepare PSNR mean/std DataFrames: index participants, columns per-architecture
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

        # save CSV summaries
        if missing_rows:
            miss_df = pd.DataFrame(missing_rows)
            miss_csv = os.path.join(ANALYSIS_FOLDER, f'missing_summary.csv')
            miss_df.to_csv(miss_csv, index=False, sep=';')
            print('Wrote missing summary to', miss_csv)

        res_df = pd.DataFrame(resolution_rows)
        if not res_df.empty:
            # convert bitrates to kbps and improve headers
            def _bps_to_kbps(x):
                try:
                    return float(x) / 1000.0 if x is not None else None
                except Exception:
                    return None

            out_rows = []
            for _, r in res_df.iterrows():
                out_rows.append({
                    'Architecture': r.get('arch'),
                    'Participants': int(r.get('participants')) if pd.notna(r.get('participants')) else None,
                    'Avg_Width_px': r.get('avg_width'),
                    'Avg_Height_px': r.get('avg_height'),
                    'Avg_FrameSize_bytes': r.get('avg_frame_size'),
                    'Outgoing_kbps': _bps_to_kbps(r.get('outgoing_bps')),
                    'Incoming_kbps': _bps_to_kbps(r.get('incoming_bps'))
                })
            out_df = pd.DataFrame(out_rows)
            res_csv = os.path.join(ANALYSIS_FOLDER, f'resolution_framesize.csv')
            out_df.to_csv(res_csv, index=False, sep=';')
            print('Wrote resolution/frame-size summary to', res_csv)

        # presence CSVs for debugging (matrix only) and legend
        if presence_rows:
            pres_df = pd.DataFrame(presence_rows)
            try:
                mat = pres_df.copy()
                def code(row):
                    lp = row.get('local_present')
                    pp = row.get('part_present')
                    lv = row.get('local_valid')
                    pv = row.get('part_valid')
                    # Both present and valid
                    if lv and pv:
                        return 'B'
                    # Only local valid
                    if lv and not pp:
                        return 'L'
                    # Only participant valid
                    if pv and not lp:
                        return 'P'
                    # Neither present => Missing
                    if not lp and not pp:
                        return 'M'
                    # Any other case with present but invalid -> mark as invalid '-'
                    return '-'
                mat['code'] = mat.apply(code, axis=1)
                pivot = mat.pivot(index='arch_client', columns='client', values='code')
                pivot_csv = os.path.join(ANALYSIS_FOLDER, 'presence_matrix.csv')
                # write pivot then append legend lines to same CSV for convenience
                pivot.to_csv(pivot_csv, sep=';')
                with open(pivot_csv, 'a') as lf:
                    lf.write('\n')
                    lf.write('Presence matrix legend:\n')
                    lf.write('B = both local and participant files present\n')
                    lf.write('L = local only\n')
                    lf.write('P = participant only\n')
                    lf.write('M = missing (no files)\n')
                    lf.write('- = invalid (file present but unreadable)\n')
                print('Wrote presence (matrix + legend) to', pivot_csv)
            except Exception as e:
                print('Failed to write presence matrix:', e)

        lat_df = pd.DataFrame(latency_rows)
        if not lat_df.empty:
            # pretty column names
            lat_out = lat_df.rename(columns={
                'arch': 'Architecture', 'participants': 'Participants',
                'avg_latency_ms': 'Avg. Latency(ms)', 'avg_encode_ms': 'Avg. Encoding Time (ms)',
                'avg_decode_ms': 'Avg. Decoding Time (ms)'
            })
            lat_csv = os.path.join(ANALYSIS_FOLDER, f'latency_summary.csv')
            lat_out.to_csv(lat_csv, index=False, sep=';')
            print('Wrote latency summary to', lat_csv)

            # also produce bar chart with Encoding / Decoding / Other (other = total - enc - dec if available)
            try:
                # prepare bars per architecture-participants grouping
                fig, ax = plt.subplots(figsize=(8,4))
                labels = []
                enc = []
                dec = []
                oth = []
                for _, r in lat_out.iterrows():
                    labels.append(f"{r['Architecture']}-{int(r['Participants'])}")
                    e = float(r.get('Avg. Encoding Time (ms)') or 0)
                    d = float(r.get('Avg. Decoding Time (ms)') or 0)
                    t = float(r.get('Avg. Latency(ms)') or (e + d))
                    o = max(0.0, t - e - d)
                    enc.append(e)
                    dec.append(d)
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
            except Exception as e:
                print('Failed to create latency breakdown plot:', e)

        # NOTE: debug decode_stats.csv generation removed per user request.

        # CPU plot
        plot_cpu(cpu_results, ANALYSIS_FOLDER, scenario)

        # PSNR plot
        if not psnr_mean.empty:
            plot_psnr(psnr_mean, psnr_std, ANALYSIS_FOLDER, scenario)

    print('Analysis complete. Outputs are in', ANALYSIS_FOLDER)


if __name__ == '__main__':
    main()

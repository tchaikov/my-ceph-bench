#!/usr/bin/env python3
"""
Generate matplotlib charts for REPORT.md §9.6.

Following the visualization style of Ben England's blog post
https://ceph.io/en/news/blog/2025/crimson-seastore-vs-classic/ :
latency-on-X-axis vs IOPS/BW-on-Y-axis scatter, one series per
backend configuration, points = iodepth/numjobs combinations.

Outputs PNGs into the same directory.
"""

import json
import os
import re
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BENCH_ROOT = Path(__file__).resolve().parent.parent  # repo root
RESULTS_ROOT = BENCH_ROOT / "results"
OUT_DIR = BENCH_ROOT / "charts"
OUT_DIR.mkdir(exist_ok=True)


def load_fio_jobs(path):
    """Load fio JSON, tolerating SSH 'Warning: Permanently added' prefix."""
    with open(path) as fp:
        text = fp.read()
    start = text.find("{")
    end = text.rfind("}") + 1
    try:
        return json.loads(text[start:end]).get("jobs", [])
    except Exception:
        return []


JOB_RE = re.compile(r"(randread|randwrite|read|write)-qd(\d+)-nj(\d+)")


def parse_jobs(jobs):
    """Yield (workload, qd, nj, iops, bw_bytes, clat_mean_us, p99_us)."""
    for j in jobs:
        name = j.get("jobname", "")
        m = JOB_RE.match(name)
        if not m:
            # try author/rbm-author variants
            continue
        workload, qd, nj = m.group(1), int(m.group(2)), int(m.group(3))
        # fio mixes 'read'/'write' for sequential and rand* for random
        # determine the side that has nonzero IO
        for side in ("read", "write"):
            d = j.get(side, {})
            if d.get("iops", 0) > 0:
                clat = d.get("clat_ns", {})
                pct = clat.get("percentile", {})
                yield (
                    workload, qd, nj,
                    d["iops"],
                    d.get("bw_bytes", 0),
                    clat.get("mean", 0) / 1000.0,           # us
                    pct.get("99.000000", 0) / 1000.0,        # us
                )
                break


def gather_phase(phase_name, json_name="results-matrix.json"):
    """Return list of tuples (workload, qd, nj, iops, bw, clat_us, p99_us)."""
    path = RESULTS_ROOT / phase_name / json_name
    if not path.exists():
        return []
    return list(parse_jobs(load_fio_jobs(path)))


# --- Configuration: which phases to plot, with style ---
# Only the three matched-budget (12-core CPU total) phases.
PHASES = [
    # (label, dir, json, color, marker, comment)
    ("classic-12share (BlueStore)",   "classic-12share",       "results-matrix.json", "#1f77b4", "o", "12-core shared CPU"),
    ("crimson-12pin (SeaStore)",      "crimson-12pin",         "results-matrix.json", "#2ca02c", "s", "4 cores/OSD pinned"),
    ("crimson-12nopin (SeaStore)",    "crimson-12nopin",       "results-matrix.json", "#9467bd", "^", "4 cores/OSD no pin"),
]


def plot_workload(workload_filter, ylabel, ysel, out_name, title):
    """ysel: lambda(iops,bw) -> y-value. ylabel: y axis text."""
    fig, ax = plt.subplots(figsize=(11, 6.5))

    for label, phase, jname, color, marker, comment in PHASES:
        rows = gather_phase(phase, jname)
        xs, ys = [], []
        for (w, qd, nj, iops, bw, clat, p99) in rows:
            if w != workload_filter:
                continue
            # Use clat MEAN as x; IOPS or BW as y
            xs.append(clat / 1000.0)  # ms
            ys.append(ysel(iops, bw))
        if not xs:
            continue
        # sort by x for line readability
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        xs = [xs[i] for i in order]
        ys = [ys[i] for i in order]
        ax.plot(xs, ys, marker=marker, color=color, label=label,
                linewidth=1.4, markersize=6, alpha=0.85)

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Mean completion latency  (ms, log)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(which="both", linestyle=":", alpha=0.4)
    ax.legend(loc="best", fontsize=9, framealpha=0.85)
    # Annotate the "performance frontier" direction
    ax.annotate("better", xy=(0.04, 0.96), xycoords="axes fraction",
                fontsize=10, color="gray",
                xytext=(0.20, 0.96), textcoords="axes fraction",
                arrowprops=dict(arrowstyle="->", color="gray"))
    fig.tight_layout()
    out = OUT_DIR / out_name
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print(f"  wrote {out}")


# === Generate four blog-style charts ===
plot_workload("randread",  "IOPS (log)",                      lambda i, b: i,         "01-randread-4k.png",  "4K random read — latency vs IOPS")
plot_workload("randwrite", "IOPS (log)",                      lambda i, b: i,         "02-randwrite-4k.png", "4K random write — latency vs IOPS")
plot_workload("read",      "Bandwidth MB/s (log)",            lambda i, b: b/1024/1024, "03-seqread-64k.png", "64K sequential read — latency vs bandwidth")
plot_workload("write",     "Bandwidth MB/s (log)",            lambda i, b: b/1024/1024, "04-seqwrite-64k.png","64K sequential write — latency vs bandwidth")



print(f"\nAll charts written to {OUT_DIR}")

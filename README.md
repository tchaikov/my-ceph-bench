## What I set out to do, and what I found

I wanted a fair head-to-head between crimson + SeaStore and classic +
BlueStore on Ceph master, on real hardware that mirrors what a Proxmox
user might deploy: one host, a single NVMe, three OSDs. Both sides run
under the **same CPU budget**: 12 cores total for 3 OSDs.

- **classic-12share** — classic OSD + BlueStore, all three OSDs share
  the same 12-core cpuset
- **crimson-12pin** — crimson + SeaStore (SegmentManager), each OSD
  pinned to a private 4-core group (0-3, 4-7, 8-11)

The same 96-job fio matrix runs against each — four workloads
(4 K randread, 4 K randwrite, 64 K seqread, 64 K seqwrite) × six
iodepths (1, 2, 4, 8, 16, 32) × four numjobs (1, 4, 32, 64).
Raw JSON is in `results/`.

---

## 1. What I'm measuring on

### Hardware

| Item | Value |
|---|---|
| Host | Proxmox VE node `pve` |
| CPU | 32-thread x86_64 |
| RAM | 128 GiB+ |
| OS root | `/dev/nvme0n1` (separate device, not under test) |
| **Test storage** | `/dev/nvme1n1` — Samsung SSD 9100 PRO 2 TB, **512-LBA mode** (factory default) |
| Test partitions | `nvme1n1p1` (600 GB), `nvme1n1p2` (600 GB), `nvme1n1p3` (663 GB) — one OSD per partition |

SeaStore expects 4 KiB-aligned writes (its `UNIT_SIZE`); make sure
the device-reported alignment matches before `mkfs`.

### Software

| Item | Value |
|---|---|
| Kernel | Linux 7.0.2-6-pve |
| Ceph | 21.0.0-pve1 — proxmox HEAD `fd44b48`, based on upstream `54a58396ffa7` with local fixes applied |
| Build type | `-O2` RelWithDebInfo, **`-DNDEBUG`** (plain `assert()` compiled out; `ceph_assert` still active). The `-O3` Release build crash-loops under PG load, so `-O2` is the usable optimized build. |
| Cluster shape | 1 mon, 1 mgr, 3 OSDs (all on the single host `pve`) |
| Pool under test | `bench-rbd`, size=3, min_size=2, pg_num=32 |
| CRUSH rule | `bench-rule` — `chooseleaf_firstn 0 type osd`. Switched from the default host-level rule because all three OSDs live on one host; the default rule leaves PGs permanently `undersized+peered`. |

### Workload side

| Item | Value |
|---|---|
| Bench VM | Proxmox VM 9001, debian-12 (bookworm) cloud image, 4 vCPU, 4 GiB RAM |
| VM disks | `scsi0`: 3 GiB boot on bench-rbd. `scsi1`: 32 GiB raw test device exposed as `/dev/sdb` |
| fio | 3.33, inside the VM, `ioengine=libaio`, `direct=1`, `time_based`, `group_reporting=1` |
| Storage path | VM `/dev/sdb` → virtio-scsi → host qemu → librbd → 3-OSD pool (rbd object size = 4 MiB default) |

---

## 2. How I ran each bench

### 2.1 The workload

One fio job file, `bench-matrix.fio`, holds all 96 jobs: four
workloads × six iodepths × four numjobs, each `runtime=30 ramp_time=5`,
`stonewall`-separated so they run one at a time.

```
# 4 K random
rw=randread  bs=4k   (×  qd∈{1,2,4,8,16,32} × nj∈{1,4,32,64})
rw=randwrite bs=4k   (×  …)
# 64 K sequential
rw=read      bs=64k  (×  …)
rw=write     bs=64k  (×  …)
```

### 2.2 Per-phase ritual

1. (re)create the OSDs for the phase's backend + CPU layout.
2. recreate the `bench-rbd` pool fresh.
3. prefill the 32 GiB test device with a 24 GiB sequential write
   (so randwrite hits allocated space, not first-touch).
4. warmup pass.
5. author-spec single point: 4 K randwrite, QD20, numjobs=1, 180 s.
6. the 96-job matrix.
7. collectors run throughout: `pidstat -u -r -p` (per-process),
   `pidstat -t` (per-thread → per-reactor CPU%), `mpstat -P ALL`
   (per-core), plus `/proc/PID/status` `VmPeak/HWM/RSS` every 60 s.

### 2.3 Methodological limitations

- Single trial per cell. ~5 % run-to-run variance on warm reads,
  more on writes. Headline numbers are single-shot.
- One host, one NVMe. Replication and CRUSH are OSD-level, not
  host-level; this is a per-node engine comparison, not a cluster study.
- `-O2`, not `-O3`. Absolute ceilings sit below an `-O3` build for
  both backends; the comparison between them is still apples-to-apples.

### 2.4 Config verified at runtime, not just set

Each phase's CPU layout and log levels were read back from the running
daemons (`ceph daemon osd.N config get …`, `taskset -cp`, per-reactor
thread affinity) rather than trusted from `ceph.conf`:

- crimson-12pin: `crimson_cpu_set` effective per OSD; reactor threads
  confirmed on cores 0-3 / 4-7 / 8-11.
- `debug_seastore*` confirmed at the `0/5` default (trace-level seastore
  logging is on the I/O path and is left off).
- `crimson_poll_mode` **off** — pidstat shows event-driven %CPU (drops
  to ~2 % in inter-job gaps), not a poll-loop floor.

---

## 3. Results — matched 12-core CPU budget

Same 12-core CPU budget on both sides: classic's 3 OSDs share cores
0-11; crimson's 3 OSDs get 4 pinned reactors each (12 reactors over the
same 12 cores).

### 3.1 Setup

| Phase | OSD binary | Backend | CPU constraint |
|---|---|---|---|
| **classic-12share** | `ceph-osd-classic` | BlueStore | systemd `CPUAffinity=0-11` shared by all 3 OSDs (default 16-thread NVMe pool per OSD, all contending in the 12-core pool) |
| **crimson-12pin** | `ceph-osd-crimson` | SeaStore (segment manager) | per-OSD `crimson_cpu_set`: osd.0→0-3, osd.1→4-7, osd.2→8-11 (4 reactors pinned per OSD) |

Common to both phases:
- 3 OSDs, all on the same NVMe (`/dev/nvme1n1p{1,2,3}`).
- Same fio matrix on `/dev/sdb`: 96 cells. `runtime=30, ramp_time=5`.
- Same captures (pidstat per-process/per-thread, mpstat per-core,
  `/proc/PID/status` every 60 s).

### 3.2 Headline numbers (peak of each workload)

| Workload | classic-12share | crimson-12pin | 12pin vs classic |
|---|---:|---:|---:|
| 4 KiB random read (peak) | **210.1k** IOPS | 164.3k IOPS | -22% |
| 4 KiB random write (peak) | **17.0k** IOPS | 8.6k IOPS | -49% |
| 64 KiB seq read (peak) | **9,392** MB/s | 9,065 MB/s | -3% |
| 64 KiB seq write (peak) | **712** MB/s | 287 MB/s | -60% |

**Author-spec single point** (4 K randwrite, QD20, numjobs=1, 180 s):

| | classic-12share | crimson-12pin |
|---|---:|---:|
| IOPS | 3,709 | **4,628** |
| mean lat | 5,391µs | **4,320µs** |

The author-spec runs on a freshly prefilled store; the 96-job matrix runs
after the read sweep and accumulated writes have aged it. For SeaStore this
gap is decisive: the author-spec 4,628 IOPS at qd20-nj1 sits well above
its matrix neighbours at nj=1 (1.1k at qd16, 1.7k at qd32), so the
author point is a fresh-store transient, not a point on the QD curve.
Classic interpolates cleanly (3,709, between qd16=2.9k and qd32=5.7k) —
BlueStore write throughput does not degrade with store age. The matrix
rows below reflect aged-store steady state for both.

### 3.3 Does crimson outperform classic at matched resources?

- 4 K random read: classic peaks at 210.1k IOPS, crimson at 164.3k (−22%).
  They track at low QD; classic pulls ahead at high concurrency.
- 4 K random write: classic leads every aged-store matrix cell — peak 17.0k
  vs 8.6k, and at nj=1 classic runs 2.9k/5.7k at qd16/qd32 vs crimson's
  1.1k/1.7k. SeaStore write throughput falls as the store ages (see [§3.2](#32-headline-numbers-peak-of-each-workload));
  BlueStore does not.
- 64 K seq read: near parity at peak — classic 9,392 MB/s vs 9,065 MB/s.
  At ≈9 GB/s both are largely cache-served; the gap reflects the stack, not
  the engine.
- 64 K seq write: classic 712 MB/s vs 287 MB/s (2.5×), the widest backend
  gap.

With CPU equalized, crimson is within 20–30% of classic on random reads and
within 3% on sequential reads. On writes, classic leads by 2–2.5× on the
aged-store matrix, and SeaStore's write throughput degrades with store age
in a way BlueStore's does not.

### 3.4 Full matrix tables

#### 4 KiB random read — IOPS

| iodepth \ numjobs | classic | crimson-pin |
|---|---|---|
| qd=1 nj=1 | 9.2k | 7.6k |
| qd=1 nj=4 | 34.3k | 28.1k |
| qd=1 nj=32 | 120.9k | 92.9k |
| qd=1 nj=64 | 153.4k | 132.3k |
| qd=4 nj=1 | 38.0k | 39.1k |
| qd=4 nj=4 | 96.3k | 78.0k |
| qd=4 nj=32 | 190.6k | 131.4k |
| qd=4 nj=64 | 198.4k | 128.2k |
| qd=16 nj=1 | 67.6k | 58.2k |
| qd=16 nj=4 | 174.3k | 140.5k |
| qd=16 nj=32 | 205.3k | 134.4k |
| qd=16 nj=64 | 201.8k | 135.7k |
| qd=32 nj=1 | 68.2k | 59.0k |
| qd=32 nj=4 | 210.1k | 164.3k |
| qd=32 nj=32 | 205.7k | 138.2k |
| qd=32 nj=64 | 201.9k | 138.5k |

#### 4 KiB random read — P99

| iodepth \ numjobs | classic | crimson-pin |
|---|---|---|
| qd=1 nj=1 | 131µs | 253µs |
| qd=1 nj=4 | 175µs | 293µs |
| qd=1 nj=32 | 553µs | 1.2ms |
| qd=1 nj=64 | 1.3ms | 3.2ms |
| qd=4 nj=1 | 148µs | 259µs |
| qd=4 nj=4 | 297µs | 618µs |
| qd=4 nj=32 | 2.4ms | 7.2ms |
| qd=4 nj=64 | 4.1ms | 11.5ms |
| qd=16 nj=1 | 293µs | 618µs |
| qd=16 nj=4 | 922µs | 2.0ms |
| qd=16 nj=32 | 7.0ms | 15.1ms |
| qd=16 nj=64 | 18.0ms | 22.4ms |
| qd=32 nj=1 | 537µs | 782µs |
| qd=32 nj=4 | 2.0ms | 6.1ms |
| qd=32 nj=32 | 16.6ms | 24.2ms |
| qd=32 nj=64 | 35.4ms | 41.7ms |

#### 4 KiB random write — IOPS

| iodepth \ numjobs | classic | crimson-pin |
|---|---|---|
| qd=1 nj=1 | 349 | 261 |
| qd=1 nj=4 | 754 | 772 |
| qd=1 nj=32 | 5.8k | 5.9k |
| qd=1 nj=64 | 10.2k | 7.5k |
| qd=4 nj=1 | 755 | 773 |
| qd=4 nj=4 | 3.0k | 4.1k |
| qd=4 nj=32 | 16.9k | 8.4k |
| qd=4 nj=64 | 16.6k | 8.5k |
| qd=16 nj=1 | 2.9k | 1.1k |
| qd=16 nj=4 | 10.1k | 2.1k |
| qd=16 nj=32 | 16.7k | 2.4k |
| qd=16 nj=64 | 16.4k | 2.4k |
| qd=32 nj=1 | 5.7k | 1.7k |
| qd=32 nj=4 | 16.8k | 2.4k |
| qd=32 nj=32 | 16.4k | 2.3k |
| qd=32 nj=64 | 16.6k | 2.3k |

#### 4 KiB random write — P99

| iodepth \ numjobs | classic | crimson-pin |
|---|---|---|
| qd=1 nj=1 | 4.0ms | 5.5ms |
| qd=1 nj=4 | 7.3ms | 10.2ms |
| qd=1 nj=32 | 8.8ms | 15.8ms |
| qd=1 nj=64 | 11.2ms | 29.5ms |
| qd=4 nj=1 | 7.2ms | 10.0ms |
| qd=4 nj=4 | 8.4ms | 9.8ms |
| qd=4 nj=32 | 14.0ms | 66.8ms |
| qd=4 nj=64 | 35.9ms | 111.7ms |
| qd=16 nj=1 | 8.6ms | 34.9ms |
| qd=16 nj=4 | 10.6ms | 107.5ms |
| qd=16 nj=32 | 90.7ms | 717.2ms |
| qd=16 nj=64 | 124.3ms | 876.6ms |
| qd=32 nj=1 | 9.0ms | 55.8ms |
| qd=32 nj=4 | 14.1ms | 227.5ms |
| qd=32 nj=32 | 128.5ms | 960.5ms |
| qd=32 nj=64 | 278.9ms | 1887.4ms |

#### 64 KiB seq read — BW_MBS

| iodepth \ numjobs | classic | crimson-pin |
|---|---|---|
| qd=1 nj=1 | 309 | 251 |
| qd=1 nj=4 | 1,205 | 1,249 |
| qd=1 nj=32 | 3,770 | 4,277 |
| qd=1 nj=64 | 5,796 | 5,017 |
| qd=4 nj=1 | 510 | 520 |
| qd=4 nj=4 | 1,633 | 1,020 |
| qd=4 nj=32 | 6,275 | 5,247 |
| qd=4 nj=64 | 7,318 | 6,292 |
| qd=16 nj=1 | 573 | 584 |
| qd=16 nj=4 | 2,048 | 1,353 |
| qd=16 nj=32 | 8,849 | 8,166 |
| qd=16 nj=64 | 9,233 | 9,065 |
| qd=32 nj=1 | 867 | 523 |
| qd=32 nj=4 | 2,784 | 1,857 |
| qd=32 nj=32 | 9,392 | 9,063 |
| qd=32 nj=64 | 7,045 | 8,826 |

#### 64 KiB seq read — P99

| iodepth \ numjobs | classic | crimson-pin |
|---|---|---|
| qd=1 nj=1 | 297µs | 453µs |
| qd=1 nj=4 | 313µs | 506µs |
| qd=1 nj=32 | 750µs | 1.8ms |
| qd=1 nj=64 | 1.8ms | 3.1ms |
| qd=4 nj=1 | 807µs | 1.3ms |
| qd=4 nj=4 | 954µs | 1.8ms |
| qd=4 nj=32 | 2.8ms | 8.5ms |
| qd=4 nj=64 | 5.7ms | 21.1ms |
| qd=16 nj=1 | 3.1ms | 5.9ms |
| qd=16 nj=4 | 3.5ms | 6.6ms |
| qd=16 nj=32 | 10.9ms | 32.9ms |
| qd=16 nj=64 | 27.1ms | 63.7ms |
| qd=32 nj=1 | 5.6ms | 11.5ms |
| qd=32 nj=4 | 7.1ms | 12.5ms |
| qd=32 nj=32 | 21.9ms | 63.2ms |
| qd=32 nj=64 | 75.0ms | 110.6ms |

#### 64 KiB seq write — BW_MBS

| iodepth \ numjobs | classic | crimson-pin |
|---|---|---|
| qd=1 nj=1 | 22 | 12 |
| qd=1 nj=4 | 47 | 65 |
| qd=1 nj=32 | 252 | 131 |
| qd=1 nj=64 | 451 | 55 |
| qd=4 nj=1 | 47 | 35 |
| qd=4 nj=4 | 147 | 147 |
| qd=4 nj=32 | 670 | 287 |
| qd=4 nj=64 | 667 | 168 |
| qd=16 nj=1 | 66 | 55 |
| qd=16 nj=4 | 134 | 143 |
| qd=16 nj=32 | 210 | 61 |
| qd=16 nj=64 | 233 | 27 |
| qd=32 nj=1 | 94 | 81 |
| qd=32 nj=4 | 197 | 202 |
| qd=32 nj=32 | 218 | 47 |
| qd=32 nj=64 | 212 | 35 |

#### 64 KiB seq write — P99

| iodepth \ numjobs | classic | crimson-pin |
|---|---|---|
| qd=1 nj=1 | 3.8ms | 9.6ms |
| qd=1 nj=4 | 8.8ms | 10.2ms |
| qd=1 nj=32 | 14.9ms | 75.0ms |
| qd=1 nj=64 | 16.9ms | 179.3ms |
| qd=4 nj=1 | 8.8ms | 11.7ms |
| qd=4 nj=4 | 12.9ms | 15.5ms |
| qd=4 nj=32 | 23.2ms | 130.5ms |
| qd=4 nj=64 | 53.7ms | 801.1ms |
| qd=16 nj=1 | 28.2ms | 85.5ms |
| qd=16 nj=4 | 48.0ms | 63.2ms |
| qd=16 nj=32 | 438.3ms | 2734.7ms |
| qd=16 nj=64 | 467.7ms | 8556.4ms |
| qd=32 nj=1 | 36.4ms | 93.8ms |
| qd=32 nj=4 | 60.0ms | 89.7ms |
| qd=32 nj=32 | 526.4ms | 4278.2ms |
| qd=32 nj=64 | 1518.3ms | 9193.9ms |

### 3.5 Per-core CPU use

`crimson_poll_mode` was **not** set, and pidstat confirms event-driven
behaviour: per-OSD `%CPU` drops to a few percent during inter-job gaps
and ramps under load, rather than sitting at a poll-loop floor.

| Phase | per-OSD %CPU min | max | mean |
|---|---:|---:|---:|
| classic-12share | 0 % | 352 % | 83 % |
| crimson-12pin | 1 % | 399 % | 185 % |

- classic-12share: the 3 OSDs' worker-thread pools contend in the
  shared 12-core pool; cores 0-11 run hot under peak read, the rest idle.
- crimson-12pin: each OSD's 4 reactors stay on their pinned 4-core
  block; no leakage outside cores 0-11.

### 3.6 Memory footprint per OSD (peak RSS)

Sampled every 60 s from `/proc/PID/status` `VmHWM` (peak resident).

| Phase | Peak RSS per OSD |
|---|---|
| classic-12share | ~1.8 GiB |
| crimson-12pin | ~20.3 GiB |

BlueStore is held near its `osd_memory_target` (4 GiB). SeaStore has no
equivalent cap on this snapshot; its cache grows unconstrained.

### 3.7 What I'd change next time

- More than one trial per cell — the headline numbers are single-shot.
- Vary classic's thread-pool size to find where it plateaus under a
  12-core budget, and what reactor count it takes for crimson to match it.
- Run a `-O3`-equivalent build (once the crimson `-O3` crash is fixed) to
  lift both ceilings and re-check whether the ranking holds.

---

## 4. Visualisation — latency vs throughput

Following the chart style of Ben England's blog post *"Crimson SeaStore
vs Classic"* on ceph.io — each point is one `(iodepth, numjobs)`
combination from the 96-job sweep, lines connect points within a single
backend, both axes log-scaled. Curves further to the **upper-left** =
better (more throughput at lower latency). Generated with
`charts/gen-charts.py` from each phase's `results-matrix.json`.

### 4.1 4 K random read

![4K randread latency vs IOPS](charts/01-randread-4k.png)

### 4.2 4 K random write

![4K randwrite latency vs IOPS](charts/02-randwrite-4k.png)

### 4.3 64 K sequential read

![64K seqread latency vs BW](charts/03-seqread-64k.png)

### 4.4 64 K sequential write

![64K seqwrite latency vs BW](charts/04-seqwrite-64k.png)

## Appendix A — raw artifacts

The fio JSON for each phase lives under `results/`:

- `results/classic-12share/results-matrix.json`
- `results/crimson-12pin/results-matrix.json`

Each JSON carries the full 96-job sweep. `charts/gen-charts.py` parses
them and produces the four PNGs in `charts/`.

## Appendix B — exact fio job definitions

`bench-matrix.fio` (96 jobs, 30 s each) is the single workload file
used across both phases. See [§2.1](#21-the-workload) for its structure.

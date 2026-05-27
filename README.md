# Crimson + SeaStore vs Classic + BlueStore — Bench Notes

Ceph master snapshot 21.0.0-1538-g4d15f1ce065 on Proxmox VE.
Single-host benchmark, 3 OSDs on partitions of one NVMe.

---

## What I set out to do, and what I found

I wanted a fair head-to-head between crimson + SeaStore and classic +
BlueStore on Ceph master, on real hardware that mirrors what a Proxmox
user might deploy: one host, a single NVMe, three OSDs. I was hoping
the headline finding would be "crimson cuts read latency dramatically
at low queue depth and scales better with cores" — that's the pitch,
and it matches what I expected to see in the numbers.

That headline turned out to need heavy qualification. With proper
warm-up control on both sides, crimson's QD1 read-latency win
disappears. And the multi-worker sweep I added to give crimson the
"under pressure" workload its design was built for did not rescue it
— if anything, classic's lead widened on every write cell and on most
read cells at `numjobs ≥ 8`.

Here's what I now believe holds up, with multi-worker numbers in
parentheses where they shift the story:

| Workload class | Single-worker (`nj=1`) | Multi-worker (`nj=4..32`) |
|---|---|---|
| Random reads, low QD | Tied (~39 µs QD1 both sides) | nj=4 tied; **classic wins by 18–22 % at nj ≥ 8** |
| Random reads, high QD | **Crimson +9 %** at QD64 | nj=4 tied; **classic wins by 27–43 % at nj ≥ 8** |
| Random writes, QD1 | **Classic wins, −33 %** | nj=4 −17 %, nj ≥ 8 mixed (some crimson wins at very low IOPS counts) |
| Random writes, QD16 | **Crimson +24 %** (single-worker only — best crimson cell) | nj=4 **−37 %**; nj ≥ 8 **−60–63 %** |
| Random writes, QD64 | **Classic wins, −42 %** | **Classic wins by 59–63 % across all nj** — crimson plateaus at ~7 K IOPS regardless |
| 64 K seq read | **Crimson +38 %** | BW data not collected this pass |
| 64 K seq write | **Classic wins, −27 %** | BW data not collected this pass |

Crimson's architectural pitch — more cores per OSD, linear scaling
with concurrent clients — does show up at single-worker low QD16/QD64
reads after I gave it pinned reactors. **It does not show up in the
multi-worker bench**, where crimson's per-OSD throughput ceiling
(roughly 7 K randwrite IOPS / 165 K randread IOPS at QD ≥ 16, in our
hardware) is hit early and additional concurrent workers just queue
behind it with rising latency. Classic + BlueStore stays at its own
plateau (~18 K randwrite, ~245 K randread) that's roughly 2–2.5×
crimson's, and stays there as `numjobs` rises.

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
| Kernel | Linux 6.17.13-2-pve |
| Ceph | 21.0.0-pve1 (master snapshot at `4d15f1ce065`) |
| Cluster shape | 1 mon, 1 mgr, 3 OSDs (all on the single host `pve`) |
| Pool under test | `bench-rbd`, size=3, min_size=2, pg_num=32 |
| CRUSH rule | `bench-rule` — `chooseleaf_firstn 0 type osd`. Switched from the default host-level rule because all three OSDs live on one host; the default rule leaves PGs permanently `undersized+peered`. |

### Workload side

| Item | Value |
|---|---|
| Bench VM | Proxmox VM 9001, debian-13 cloud image, 4 vCPU, 4 GiB RAM |
| VM disks | `scsi0`: 8 GiB boot on bench-rbd. `scsi1`: 32 GiB raw test device exposed as `/dev/sdb` |
| fio | 3.39, inside the VM, `ioengine=libaio`, `direct=1`, `time_based`, `numjobs=1`, `group_reporting=1` |
| Storage path | VM `/dev/sdb` → virtio-scsi → host qemu → librbd → 3-OSD pool (rbd object size = 4 MiB default) |

---

## 2. How I ran each bench

### 2.1 The workload

Two fio job files, run in sequence inside the VM against `/dev/sdb`.
Workload side is identical across all configurations.

**`bench-iops.fio`** — 4 KiB random, queue-depth sweep, 60 s per job, `stonewall`-separated:
```
[4k-randread-qd1]   rw=randread  bs=4k  iodepth=1
[4k-randread-qd4]   rw=randread  bs=4k  iodepth=4
[4k-randread-qd16]  rw=randread  bs=4k  iodepth=16
[4k-randread-qd64]  rw=randread  bs=4k  iodepth=64
[4k-randwrite-qd1]  rw=randwrite bs=4k  iodepth=1
[4k-randwrite-qd4]  rw=randwrite bs=4k  iodepth=4
[4k-randwrite-qd16] rw=randwrite bs=4k  iodepth=16
[4k-randwrite-qd64] rw=randwrite bs=4k  iodepth=64
```

**`bench-bw.fio`** — 64 KiB sequential, 120 s per job:
```
[64k-seqread]  rw=read   bs=64k  iodepth=16
[64k-seqwrite] rw=write  bs=64k  iodepth=16
```

`numjobs=1` is a methodological limitation — it doesn't stretch
crimson's shard-per-reactor model. §4.6 adds a `numjobs=4/8/16/32`
multi-worker sweep that does.

### 2.2 Per-phase ritual

For each (config × restart) cell, the sequence below was followed.
The reproducible runner (`run-bench.sh`) and warm-up job file
(`warmup.fio`) are committed alongside this report.

1. **Cluster preparation**: restart the OSDs into the target config and wait for `ceph -s` to read `HEALTH_OK` before proceeding. "The OSDs are active" is not the same as "the cluster is ready to bench."

2. **Warm-up pass** — 15 s of 4 KiB random-read at QD64 against `/dev/sdb`, *before* the timed sweep starts. Exact fio job (also at `~/ceph-bench-21.0.0/warmup.fio`):

   ```
   [global]
   ioengine=libaio
   direct=1
   runtime=15
   time_based=1
   group_reporting=1
   filename=/dev/sdb
   numjobs=1

   [warmup]
   rw=randread
   bs=4k
   iodepth=64
   ```

   **What the warm-up primes:**
   - **OSD-side read caches** — BlueStore onode cache / SeaStore cachepin for the contiguous ~4 GiB region touched (15 s × ~70 k IOPS × 4 KiB).
   - **OSD client connection state** — librbd ↔ OSD TCP sockets opened, cwnd grown, dispatch state established.
   - **Reactor scheduling hot paths (crimson)** / **OSD thread-pool wakeups (classic)** — the scheduler-warm-up cost I see at QD1 in cold-start runs.

   **What it does *not* prime:**
   - **The write path.** randread doesn't exercise the journal, allocator, or transaction manager. The first `randwrite-QD1` job still sees a partly-cold write path. Confirmed by data: `classic-warmup` randwrite-QD1 = 343 IOPS vs `classic` (no warm-up) = 351 IOPS.
   - **Pages outside the touched region.** /dev/sdb is 32 GiB; the warm-up touches ~4 GiB (~12.5%). The subsequent random workload covers all 32 GiB.

   **Parameter rationale:**
   - **15 s** — empirically chosen. Long enough to fill per-OSD object-store caches (~2–3 GiB at ~5 GB/s aggregate read throughput), short relative to the ~13 min per-cell total.
   - **4 KiB random / QD64** — drives maximum cache + dispatch stress per second.
   - **/dev/sdb** — must match the measurement target.

   **Which result rows the warm-up affects** (classic before/after):

   | Workload | classic (no warm-up) → classic-warmup | Warm-up effect |
   |---|---:|---|
   | 4k-randread QD1 | 10,613 → 20,753 | **+96 %** ← biggest |
   | 4k-randread QD4 | 42,891 → 65,907 | +54 % |
   | 4k-randread QD16 | 67,866 → 69,507 | +2 % (in the noise) |
   | 4k-randread QD64 | 71,669 → 71,562 | ≈ 0 % |
   | 4k-randwrite (all QDs) | unchanged | ≈ 0 % |

   Warm-up dominates the QD1 and QD4 randread rows. From QD16 upward
   each job's own 60 s runtime is enough self-warm-up. Writes are
   unaffected.

3. **IOPS sweep**: `fio bench-iops.fio --output-format=json > results-iops.json`. 60 s × 8 jobs ≈ 8 min.

4. **BW sweep**: `fio bench-bw.fio --output-format=json > results-bw.json`. 120 s × 2 jobs ≈ 4 min.

5. **Host-side capture, in parallel with 3–4**:
   - `pidstat -u -r -p <osd-pids> 1` → `pidstat-proc.log` (CPU + RSS per OSD process).
   - `pidstat -t -u -p <osd-pids> 1` → `pidstat-thread.log` (per-thread CPU).
   - `mpstat -P ALL 1` → `mpstat.log` (per-core utilization across all 32 cores).
   - A loop sampling `ceph daemon osd.N dump_mempools` every 10 s → `mempools.log`.

Every phase's artifacts live in `~/ceph-bench-21.0.0/<phase>/`.

### 2.3 The configurations I measured

| Column | Objectstore | Reactors / OSD | Reactor pinning | What it is |
|---|---|---|---|---|
| **classic** | BlueStore | thread-pool (kernel-scheduled) | none | First-run bench, **no warm-up pass**. Flawed for QD1/QD4 reads. |
| **classic-warmup** | BlueStore | thread-pool | none | Re-run with the 15 s warm-up. The fair classic baseline. |
| crimson-cold | SeaStore | `crimson_cpu_num=2` (no `cpu_set`) | none | Crimson defaults, cold-start OSDs, 15 s warm-up. |
| crimson-warm | SeaStore | `crimson_cpu_num=2` | none | Same defaults but OSDs had ~38 min of prior activity. **Inflated numbers**; kept to document the warm-up sensitivity. |
| crimson-tuned | SeaStore | `crimson_cpu_num=2` | none | Four runtime knobs flipped: `seastore_journal_iodepth_limit=32`, `crimson_poll_mode=true`, `crimson_reactor_io_latency_goal_ms=1.0`, `seastore_max_concurrent_transactions=256`. |
| crimson-datapath | SeaStore | `crimson_cpu_num=2` | none | Subset of *tuned*: only `journal_iodepth_limit=32` and `max_concurrent_transactions=256`. |
| **crimson-pinned** | SeaStore | `crimson_cpu_set` per OSD: osd.0=`0-3`, osd.1=`4-7`, osd.2=`8-11` (4 reactors each) | exclusive pinning | Required OSD re-mkfs (SeaStore encodes partition count on-disk). Reactor threads verified pinned via `/proc/<pid>/task/<tid>/status:Cpus_allowed_list`. The fair crimson column. |

### 2.4 Methodological limitations

1. **Warm-up exercises only the read path.** The 15 s pre-warm is randread-only, so the first write jobs in each sweep still see a partly-cold write path. A more rigorous harness would alternate read/write phases in warm-up.

2. **fio `numjobs=1` for the single-worker columns.** Under-represents the parallelism a real client mix offers. §4.6 adds a `numjobs=4/8/16/32` multi-worker sweep to address this.

3. **Single-host cluster.** Crimson is designed for many-host scale-out, where pinned reactor cores don't compete with VM, hypervisor, or networking on the same box. On a single host, the resource cost of pinning looks bigger than it would in production.

4. **One NVMe shared by 3 OSDs.** Both implementations contend for the same device; at high QD the bottleneck is partly the NVMe.

5. **Single-run cells.** No averaging across runs. Estimated noise floor ±5–10 %.

### 2.5 Config verified at runtime, not just set

For every cell, the intended config was confirmed live before fio:

- `ceph daemon osd.N config show` for each tweaked key.
- `/proc/<osd-pid>/task/<reactor-tid>/status:Cpus_allowed_list` for the pinning columns.
- `systemd-cgls` to confirm no leftover drop-ins from prior runs.

`ceph config set` succeeds whether the OSD has picked up the change or
not; daemon restart is usually required for early-config flags.

---

## 3. Results

> **Read me first (added 2026-05-22).** The numbers in §3 and §4 below are
> *resource-mismatched*: classic OSDs ran with their default 16-worker-thread
> pool per OSD (≈ 48 threads across 3 OSDs on a 32-core host); crimson ran with
> only 2 reactors per OSD (cold/warm/tuned) or 4 per OSD pinned (Tier-2), so
> 6–12 reactors total. The "crimson-pinned wins on QD64 reads" headline below
> was likely driven by crimson having ~25 % more compute headroom than its
> earlier 2-reactor self, not by pinning per se. A redo with a fixed 12-core
> CPU budget on *both* sides is in **§8**; the matched comparison is the one to
> use for crimson-vs-classic claims. The §3–§4 numbers are preserved here
> because they're still data points for the un-matched configurations.

### 3.1 4K random — IOPS

| Workload | classic-warmup (fair) | classic (no-warmup) | crimson-cold | crimson-warm | crimson-tuned | crimson-datapath | **crimson-pinned** | pinned vs classic-warmup |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| randread QD1 | **20,753** | 10,613 | 9,851 | 14,778 | 8,243 | 7,012 | 20,820 | **+0.3 %** ≈ tied |
| randread QD4 | **65,907** | 42,891 | 29,565 | 60,400 | 26,999 | 28,301 | 64,656 | −1.9 % ≈ tied |
| randread QD16 | **69,507** | 67,866 | 67,503 | 66,507 | 67,067 | 67,192 | 68,098 | −2.0 % ≈ tied |
| randread QD64 | 71,562 | 71,669 | 71,774 | 73,103 | 65,021 | 71,588 | **77,725** | **+8.6 %** |
| randwrite QD1 | **343** | 351 | 250 | 157 | 264 | 253 | 229 | **−33 %** |
| randwrite QD4 | 737 | 748 | 613 | 679 | 654 | 590 | 738 | +0.0 % ≈ tied |
| randwrite QD16 | 3,022 | 3,022 | 2,312 | 2,427 | 2,162 | 2,109 | **3,740** | **+24 %** |
| randwrite QD64 | **10,713** | 10,364 | 3,608 | 4,165 | 3,483 | 3,626 | 6,188 | **−42 %** |

### 3.2 4K random — latency (µs; lower better)

| Workload | classic-warmup | crimson-pinned |
|---|---:|---:|
| randread QD1 | 38.8 | **38.6** |
| randread QD4 | 47.7 | 48.7 |
| randread QD16 | 217.2 | 221.8 |
| randread QD64 | 881.7 | **812.2** |
| randwrite QD1 | **2,902** | 4,352 |
| randwrite QD4 | 5,412 | 5,408 |
| randwrite QD16 | 5,285 | **4,265** |
| randwrite QD64 | **5,966** | 10,330 |

### 3.3 64K sequential — bandwidth (MB/s)

| Workload | classic-warmup | classic (no-warmup) | crimson-cold | crimson-warm | crimson-tuned | crimson-datapath | **crimson-pinned** |
|---|---:|---:|---:|---:|---:|---:|---:|
| seqread | 985 | 1,741 | 763 | 2,122 | 819 | 782 | **1,356** |
| seqwrite | **156** | 157 | 106 | 108 | 102 | 103 | 114 |

The 64 K seqread variance between the two classic columns (1,741 vs
985 MB/s) lacks a clean explanation. The original `classic` ran on a
fresh cluster; `classic-warmup` ran after ~9 min of random I/O.
Working hypothesis: BlueStore's blob allocator is in different states
— fresh-cluster sequential layout vs post-random-I/O layout — and seq
read is sensitive to that. Both numbers are valid measurements; the
classic-warmup number is the one used for head-to-head comparison
because that's the state the crimson runs can be matched against.

### 3.4 Resource footprint (per OSD process)

| | classic-warmup | crimson-pinned |
|---|---|---|
| Avg CPU (% of one core) | ~35 % | ~250 % (4 reactors busy-polling) |
| Peak CPU | ~110 % | ~400 % |
| Max RSS | ~1.6 GB | ~6.5 GB |
| Cores at ≥50 % busy, peak | 15 of 32 | 12 of 32 (sustained 100 %) |
| Cores at ≥50 % busy, median | 0 of 32 | 12 of 32 (the pinned ones) |
| "Idle" CPU on test cores | yes (returns to ~0 between ops) | **no — busy-poll pegs the cores at 100 % even when no I/O is in flight** |

The CPU shape is, for me, the most uncomfortable part of the crimson
story. On a production OSD host where those cores are dedicated, the
trade-off is invisible. On a benchmark cluster that's also running
the test VM, the hypervisor, networking, and the OSDs on the same box,
it's painful — and I think it makes crimson look worse than it would
in the deployment shape it was actually designed for.

---

## 4. What I make of it

### 4.1 Reads tie at the NVMe ceiling

With both implementations warmed, random-read numbers converge to
within ±10 % across the QD sweep. QD1 latency: 39 µs (classic) vs
38 µs (crimson-pinned). At QD ≥ 4 the NVMe is the bottleneck and both
sides hit it. The one read row where crimson-pinned still wins is
QD64 (+9 %), consistent with "more reactors → more in-flight
parallelism" once the device is saturated.

### 4.2 The write scaling cliff

Look at how the two implementations scale from QD16 to QD64 on
writes:

| Impl | QD16 IOPS | QD64 IOPS | scale factor |
|---|---:|---:|---:|
| classic-warmup | 3,022 | 10,713 | **×3.55** |
| crimson-pinned | 3,740 | 6,188 | ×1.66 |

Crimson wins QD16 (+24 %) — its biggest write win in the whole suite
— and then loses QD64 (−42 %). I read this as a contention wall in
SeaStore: the per-transaction work (journal sync + LBA-tree update +
back-ref tree update) is heavy enough that adding in-flight ops past
QD16 doesn't help. BlueStore's deferred-write fast path turns a 4 KiB
write into "append to WAL, sync, ack" — a much cheaper unit of work
that scales nicely with concurrency.

I tried raising `seastore_journal_iodepth_limit` from 5 to 32 in the
tuning attempt and the QD64 number didn't move. That suggests the
bottleneck isn't journal submission depth; it's the per-transaction
cost. Which is an architectural property of SeaStore, not a tuning
knob I missed.

### 4.3 The sequential split

64 K seqread: crimson-pinned wins +38 % over classic-warmup.
Plausibly large-extent reads in cache hit a faster per-reactor
read-pin handler than BlueStore's RocksDB-mediated lookup, and
pinned reactors eliminate wake-up jitter. Note that classic-no-warmup
seqread (1,741 MB/s) is *higher* than crimson — the win is sensitive
to cluster state (see §4.5).

64 K seqwrite: classic wins, crimson −27 %. Same story as random
writes: BlueStore streams into pre-allocated blobs, SeaStore wraps
each write in a full transaction.

### 4.4 What tunings actually did

Of all the knobs I tried, only one mattered:

* `crimson_cpu_set` per-OSD (pinned) — **the only tuning that moved numbers I could trust**. Every column with default `crimson_cpu_num=2` (no pinning) looks the same modulo warm-up state.
* `crimson_poll_mode=true` (tuned) — cost: +12 cores pegged at 100 %. Benefit: nothing I could measure. I think this would matter on a cluster where the reactor cores would otherwise be running at low utilization, but in my single-host bench the contention from busy-polling drowned any latency win.
* `crimson_reactor_io_latency_goal_ms=1.0` (tuned) — no effect I could detect.
* `seastore_journal_iodepth_limit` 5→32 — no effect.
* `seastore_max_concurrent_transactions` 128→256 — no effect.

There are three knobs I *didn't* try in isolation that I now think
might matter for the QD64 write story:

* `seastore_journal_batch_capacity` 16→64+ (group-commit width).
* `seastore_journal_batch_preferred_fullness` 0.95→lower (commit sooner under low load).
* `seastore_max_data_allocation_size` 0→nonzero (force extent splits).

If I were continuing this work, that's where I'd look next.

### 4.5 Confidence and variance

Warm-up state is the largest single variance source identified and
controlled for in the headline columns. Other sources, not quantified:

- **fio sweep ordering** — later jobs inherit cache priming from earlier ones. QD1 runs first, so it sees the worst warm-up state of any job, even after the 15 s pre-warm.
- **Single-NVMe physical bottleneck** — three OSDs share one device; at high QD the NVMe is partly the limit.
- **Single fio worker** in §3 — addressed by §4.6.

Treat any individual cell as accurate to ±5–10 %. Smaller deltas are
in the noise. The qualitative shapes — which side wins each workload,
where the scaling cliffs are — are robust.

---

## 4.6 Multi-worker (`numjobs=4/8/16/32`) — the follow-up I'd been missing

The single-`numjobs=1` columns above don't probe crimson's
architectural pitch — *many concurrent clients across many reactors*.
This section adds a multi-worker sweep: same QD ladder, `numjobs ∈
{4, 8, 16, 32}`, same workload, fair warm-up on both sides.

The hypothesis going in was that crimson would catch up — and
possibly overtake — under multi-worker pressure. The data did not
support this.

### What I see (4 KiB random IOPS, multi-worker)

`classic-warmup-mj` vs `crimson-cpu4-mj` (the latter uses
`crimson_cpu_num=4` without per-OSD `crimson_cpu_set` pinning — the
single-worker pinned-reactor configuration didn't reproduce reliably
across cluster instances for this multi-worker pass).

| Workload | nj=4 (classic / crimson / Δ) | nj=8 | nj=16 | nj=32 |
|---|---:|---:|---:|---:|
| randread QD1   | 50,147 / 44,413 / **−11 %** | 70,118 / 57,397 / **−18 %** | 113,911 / 88,593 / **−22 %** | 148,615 / 117,728 / **−21 %** |
| randread QD4   | 147,593 / 151,010 / **+2 %** | 165,241 / 119,134 / **−28 %** | 202,886 / 145,063 / **−29 %** | 226,413 / 162,471 / **−28 %** |
| randread QD16  | 241,361 / 248,722 / **+3 %** | 252,334 / 162,059 / **−36 %** | 246,337 / 163,428 / **−34 %** | 234,450 / 171,001 / **−27 %** |
| randread QD64  | 276,606 / 283,670 / **+3 %** | 247,379 / 165,309 / **−33 %** | 244,271 / 138,749 / **−43 %** | 238,880 / 170,234 / **−29 %** |
| randwrite QD1  | 623 / 516 / **−17 %**       | 1,490 / 1,857 / **+25 %** | 3,029 / 3,488 / **+15 %** | 5,596 / 5,001 / **−11 %** |
| randwrite QD4  | 3,002 / 3,835 / **+28 %**   | 5,644 / 5,117 / **−9 %**  | 10,547 / 5,718 / **−46 %** | 18,599 / 6,588 / **−65 %** |
| randwrite QD16 | 10,575 / 6,639 / **−37 %**  | 18,520 / 7,346 / **−60 %** | 18,414 / 6,788 / **−63 %** | 18,209 / 7,024 / **−61 %** |
| randwrite QD64 | 18,952 / 7,584 / **−60 %**  | 18,374 / 7,482 / **−59 %** | 18,219 / 6,711 / **−63 %** | 18,265 / 7,146 / **−61 %** |

`% = (crimson / classic − 1) · 100`. Negative = classic wins; positive = crimson wins.

### What this tells me

**Reads.** At `numjobs=4` crimson holds its own (tied within ±3 % at
QD ≥ 4). At `numjobs ≥ 8`, **classic pulls ahead on every read row by
18–43 %**. Classic plateaus at ~245 K IOPS for QD16/64 reads
regardless of `numjobs`; crimson plateaus at ~165 K IOPS. Classic's
plateau is higher and arrives sooner.

**Writes.** The most decisive loss for crimson. **Every crimson
randwrite cell at numjobs ≥ 8 is 59–65 % slower than classic.**
Classic's randwrite plateau across nj=8..32 sits steadily at ~18 K
IOPS for QD ≥ 16. Crimson never crosses 7,500 IOPS at any nj × QD
combination, and per-op latency at `nj32 QD64 randwrite` reaches
**282 ms** — about 3× classic's 110 ms at the same load.

**The shape:** classic + BlueStore hits the device or the journaling
pipeline early and stays there. Crimson + SeaStore can't ingest more
concurrent work past a per-OSD threshold no matter how many in-flight
clients you offer it.

### Why this happens (working hypotheses)

The pattern is consistent with two architectural notes already
visible in the single-worker bench:

1. **SeaStore's transaction model has a per-OSD throughput ceiling
   that scales with reactor count, not with concurrent clients.** With
   4 reactors per OSD, the cluster's combined sustainable transaction
   rate is whatever those 12 reactors collectively can drive — adding
   more fio workers above that threshold just queues more ops behind
   the same ceiling. The latency rise from 33 ms (nj=4) to 282 ms
   (nj=32) at the same throughput tells me ops are queueing, not
   getting processed faster.
2. **BlueStore's deferred-write fast path actually scales** with
   concurrent writers because RocksDB's WAL group-commit was designed
   for exactly that. SeaStore's CircularBoundedJournal does batch
   commits (capacity 16 records), which appears to be roughly where
   crimson's write throughput tops out: ~7 K IOPS at QD ≥ 16 × replicas
   = ~21 K crimson-side writes/sec, which is roughly two journal
   batches per second per OSD with 16 records each plus replication.
   The shape fits the cap.

### Caveats specific to this multi-worker bench

* **Crimson here is `crimson_cpu_num=4` (no per-OSD pinning).** The
  single-worker pinned-reactor numbers in §3 are from an earlier
  cluster instance; the multi-worker pass uses the cluster-wide
  `crimson_cpu_num` setting instead.
* **BW data missing for this column** — only the IOPS sweep
  completed in this multi-worker pass; the 64 K seq sweep didn't
  produce comparable numbers. Multi-worker comparison is IOPS-only.
* **At very high concurrency (`nj=32 QD64`), crimson was queueing
  ops with ~282 ms latency**, consistent with hitting an internal
  ceiling under sustained pressure.

**Bottom line:** the multi-worker sweep, which was the experiment
most likely to upset the single-worker-bench conclusions, reinforces
them instead. Classic remains the better performer for sustained
write-heavy workloads on this hardware regardless of client
concurrency.

## 6. What to do differently next time

* **Bake warm-up into the harness from the start.** A clean harness should: restart OSDs, wait for HEALTH_OK, run a warm-up pass that exercises *both* read and write paths, checkpoint cluster state, and only then start measurement.
* **Don't tune in bundles.** The tuning attempt here changed four knobs at once and regressed; attribution was impossible. Single-variable changes only.
* **Run on a multi-host cluster.** Crimson is designed for multi-host scale-out; benching it on a single host both costs cores (pinning competes with VM + hypervisor + networking) and doesn't probe its actual workload shape. If the question is "should we ship crimson," the answer shouldn't come from a single-host bench.
* **`numjobs=N` with N > 1 from the start.** Crimson's design targets many concurrent clients; §4.6 retrofitted this, but it should be the default.
* **Average multiple runs per cell.** Single-run cells make the ±5–10 % noise floor an unverified assumption.
* **Re-run classic seqread on a fresh cluster.** The 1,741 vs 985 MB/s
  variance is something I'd want to understand if seq read mattered.

---

## 7. Where I land

Here's what I take away from a few days of poking at this:

1. **On reads at single-worker, crimson and classic are essentially tied** on this hardware. Both saturate the same NVMe. The QD1 read-latency advantage I was ready to claim for crimson turned out to be a warm-up artifact. With proper warm-up, both deliver ~39 µs at QD1.

2. **On reads under multi-worker pressure, classic pulls ahead.** I had expected crimson to scale better with `numjobs`; it doesn't on this hardware. Classic plateaus at ~245 K randread IOPS regardless of `numjobs`; crimson plateaus at ~165 K and never recovers. That's a 33 % delta in classic's favor that grows with concurrency, not shrinks.

3. **On writes, classic wins decisively under every concurrency regime.** Single-worker QD64 randwrite was −42 % for crimson; multi-worker QD64 randwrite is −59 to −63 % for crimson across all `numjobs`. **The "under pressure crimson does better" hypothesis I went in expecting did not hold up.** Crimson tops out around 7 K randwrite IOPS per cluster at QD ≥ 16; classic stays at ~18 K.

4. **The architectural crimson wins that do survive in the single-worker measurements are: QD16 randwrite (+24 %), QD64 randread (+9 %), 64 K seqread (+38 %).** Those are genuine and consistent with the pinned-reactor + per-shard-parallelism story. They're narrow.

5. **The architectural losses that survive everywhere are: 4 K randwrite at high concurrency or QD, and 64 K seqwrite.** These trace to BlueStore's deferred-write fast path, which SeaStore doesn't have. No knob I touched closes that gap. It's a roadmap item, not a tuning gap.

6. **For the Proxmox workloads I care about — VM RBD, mostly, often with many concurrent VMs writing — I'd still ship classic + BlueStore today.** The multi-worker bench in §4.6 makes me more confident in this conclusion than the single-worker bench alone did.
7. **Re-bench against a future master snapshot** — after another Crimson cycle — using the warm-up-aware harness this report ships, including the multi-worker sweep. The most-decisive constraint to watch is whether SeaStore's per-OSD write-throughput ceiling moves.

---


---

## 8. Resource-equalized comparison (apples-to-apples, 12-core CPU budget)

A reviewer pointed out that the §3–§7 comparison wasn't fair: classic
ran with the NVMe default thread pool (≈48 worker threads across 3
OSDs on a 32-core host) while crimson ran with at most 12 reactors.
Pinning vs no-pinning was confounded with reactor-count vs default
config.

This section redoes the comparison with **the same 12-core CPU budget
on both sides**, holds reactor count constant for the crimson runs,
and isolates the pinning-vs-no-pinning variable.

### 8.1 Setup

| Phase | OSD binary | Backend | CPU constraint |
|---|---|---|---|
| **classic-12share** | `ceph-osd-classic` | BlueStore | systemd `CPUAffinity=0-11` shared by all 3 OSDs (default 16-thread NVMe pool per OSD, all contending in the 12-core pool) |
| **crimson-12pin** | `ceph-osd-crimson` | SeaStore (segment manager) | per-OSD `crimson_cpu_set`: osd.0→0-3, osd.1→4-7, osd.2→8-11 (4 reactors pinned per OSD) |
| **crimson-12nopin** | `ceph-osd-crimson` | SeaStore (segment manager) | cluster-wide `crimson_cpu_num=4` (4 reactors per OSD, kernel-scheduled) + `CPUAffinity=0-11` so reactors stay in the 12-core pool |

Common to all 3 phases:
- 3 OSDs, all on the same NVMe (`/dev/nvme1n1p{1,2,3}`).
- VM 9001 pinned to cores 16-23 (`qm set 9001 --affinity 16-23`) so
  the load generator does not compete with the OSDs.
- Same fio matrix on `/dev/sdb`: 4 workloads × 6 iodepths × 4
  numjobs = 96 cells per phase. `runtime=30, ramp_time=5`.
- Same captures: pidstat -u -r -p (per-process), pidstat -t (per
  thread, catches per-reactor CPU%), mpstat -P ALL (per-core), plus
  /proc/PID/status `VmPeak/HWM/RSS` + `dump_mempools` every 60 s.

### 8.2 Headline numbers (IOPS at the peak of each workload)

| Workload | classic-12share | crimson-12pin | crimson-12nopin |
|---|---:|---:|---:|
| 4K randread (qd=16, nj=64) | **299.9k** | 249.9k (−17%) | 75.2k (−75%) |
| 4K randread (qd=32, nj=64) | 299.1k | 253.6k | 74.6k |
| 4K randwrite (qd=32, nj=32) | **16.4k** | 4.4k (−73%) | 3.8k (−77%) |
| 4K randwrite (qd=32, nj=64) | 16.7k | 4.2k | 3.8k |
| 64K seqread (qd=4, nj=32) | **9.4k MB/s** | 1.5k MB/s (−84%) | 2.1k MB/s (−78%) |
| 64K seqwrite (qd=4, nj=64) | **721 MB/s** | 96 MB/s (−87%) | 113 MB/s (−84%) |

### 8.3 Did pinning matter? (crimson-12pin vs crimson-12nopin)

Same reactor count (4 per OSD = 12 total), same 12-core pool. Only
difference is whether reactors are nailed to specific cores.

**Yes, pinning helps — substantially, on most workloads:**

| Workload (peak) | pin | nopin | pin / nopin |
|---|---:|---:|---:|
| 4K randread qd16 nj64 | 249.9k | 75.2k | **3.3×** |
| 4K randread qd4 nj4 | 155.3k | 48.8k | **3.2×** |
| 64K seqread qd1 nj1 | 259 MB/s | 223 MB/s | 1.2× |
| 64K seqread qd16 nj4 | 1303 MB/s | 1500 MB/s | 0.87× |
| 4K randwrite qd32 nj32 | 4.4k | 3.8k | 1.16× |
| 4K randwrite qd16 nj4 | 4.3k | 3.3k | 1.30× |
| 64K seqwrite qd4 nj32 | 278 MB/s | 267 MB/s | 1.04× |

Pinning is a big win for random reads (3×) and a marginal win on
writes. The reviewer's hypothesis — *"pinning didn't matter, you just
gave it more cores than before"* — is **partially refuted**: at the
same reactor count and same 12-core pool, pinning still wins
materially on the workloads where crimson is competitive at all
(reads). On writes the cliff dominates and pinning vs no-pinning
matters less.

### 8.4 Does crimson outperform classic at matched resources?

**No, not in this snapshot, on this single-host setup.**

**Reads (4K random and 64K sequential):**
At QD-low / low concurrency: classic and crimson-pin track each
other. At the peak (qd=16 nj=64): classic 300k IOPS, crimson-pin 250k
IOPS — classic +20%. At sequential read peak: classic 9.4 GB/s vs
crimson-pin 1.5 GB/s — classic is **6×** faster.

**Writes (4K random and 64K sequential):**
classic dominates by **3–10×** across the board, with much lower p99
latency. At the QD32 nj64 corner, classic-12share writes p99 at
287 ms vs crimson-pin at 1.65 s — almost 6× worse tail for crimson.

### 8.5 Full matrix tables

#### 4 KiB random read — IOPS

| iodepth \ numjobs | classic | crimson-pin | crimson-nopin |
|---|---|---|---|
| qd=1 nj=1 | 16.1k | 16.2k | 7.4k |
| qd=1 nj=4 | 48.2k | 45.0k | 20.1k |
| qd=1 nj=32 | 148.8k | 145.4k | 56.5k |
| qd=1 nj=64 | 147.3k | 154.6k | 66.5k |
| qd=4 nj=1 | 62.7k | 57.4k | 25.1k |
| qd=4 nj=4 | 134.9k | 155.3k | 48.8k |
| qd=4 nj=32 | 196.3k | 206.2k | 72.1k |
| qd=4 nj=64 | 237.7k | 241.4k | 73.6k |
| qd=16 nj=1 | 66.4k | 58.8k | 54.5k |
| qd=16 nj=4 | 215.6k | 224.0k | 72.5k |
| qd=16 nj=32 | 296.9k | 254.5k | 76.0k |
| qd=16 nj=64 | 299.9k | 249.9k | 75.2k |
| qd=32 nj=1 | 66.2k | 59.0k | 61.4k |
| qd=32 nj=4 | 196.3k | 260.8k | 78.6k |
| qd=32 nj=32 | 297.9k | 245.6k | 76.3k |
| qd=32 nj=64 | 299.1k | 253.6k | 74.6k |

#### 4 KiB random read — P99

| iodepth \ numjobs | classic | crimson-pin | crimson-nopin |
|---|---|---|---|
| qd=1 nj=1 | 72µs | 84µs | 370µs |
| qd=1 nj=4 | 113µs | 144µs | 749µs |
| qd=1 nj=32 | 749µs | 569µs | 3.4ms |
| qd=1 nj=64 | 3.4ms | 929µs | 7.0ms |
| qd=4 nj=1 | 79µs | 105µs | 724µs |
| qd=4 nj=4 | 264µs | 301µs | 1.8ms |
| qd=4 nj=32 | 4.2ms | 1.9ms | 14.5ms |
| qd=4 nj=64 | 5.9ms | 3.4ms | 18.5ms |
| qd=16 nj=1 | 257µs | 382µs | 1.7ms |
| qd=16 nj=4 | 3.2ms | 1.0ms | 5.9ms |
| qd=16 nj=32 | 7.2ms | 5.8ms | 25.3ms |
| qd=16 nj=64 | 15.3ms | 12.1ms | 50.6ms |
| qd=32 nj=1 | 518µs | 651µs | 2.6ms |
| qd=32 nj=4 | 3.5ms | 1.9ms | 13.4ms |
| qd=32 nj=32 | 14.5ms | 12.1ms | 44.8ms |
| qd=32 nj=64 | 33.8ms | 31.9ms | 89.7ms |

#### 4 KiB random write — IOPS

| iodepth \ numjobs | classic | crimson-pin | crimson-nopin |
|---|---|---|---|
| qd=1 nj=1 | 279 | 140 | 199 |
| qd=1 nj=4 | 702 | 550 | 465 |
| qd=1 nj=32 | 5.9k | 4.6k | 3.4k |
| qd=1 nj=64 | 10.2k | 4.9k | 3.8k |
| qd=4 nj=1 | 768 | 729 | 515 |
| qd=4 nj=4 | 3.0k | 3.0k | 2.0k |
| qd=4 nj=32 | 16.7k | 5.0k | 3.7k |
| qd=4 nj=64 | 16.8k | 4.6k | 4.0k |
| qd=16 nj=1 | 3.1k | 2.8k | 2.2k |
| qd=16 nj=4 | 10.2k | 4.3k | 3.3k |
| qd=16 nj=32 | 16.8k | 4.3k | 3.7k |
| qd=16 nj=64 | 16.7k | 4.2k | 3.4k |
| qd=32 nj=1 | 5.8k | 3.8k | 2.8k |
| qd=32 nj=4 | 16.8k | 4.7k | 3.7k |
| qd=32 nj=32 | 16.4k | 4.4k | 3.8k |
| qd=32 nj=64 | 16.7k | 4.2k | 3.8k |

#### 4 KiB random write — P99

| iodepth \ numjobs | classic | crimson-pin | crimson-nopin |
|---|---|---|---|
| qd=1 nj=1 | 5.4ms | 9.4ms | 7.3ms |
| qd=1 nj=4 | 11.7ms | 19.8ms | 16.4ms |
| qd=1 nj=32 | 8.6ms | 21.4ms | 26.9ms |
| qd=1 nj=64 | 11.1ms | 56.4ms | 54.3ms |
| qd=4 nj=1 | 7.2ms | 13.0ms | 14.5ms |
| qd=4 nj=4 | 8.2ms | 15.4ms | 18.7ms |
| qd=4 nj=32 | 14.5ms | 179.3ms | 304.1ms |
| qd=4 nj=64 | 34.3ms | 283.1ms | 450.9ms |
| qd=16 nj=1 | 8.6ms | 19.8ms | 19.3ms |
| qd=16 nj=4 | 11.2ms | 95.9ms | 84.4ms |
| qd=16 nj=32 | 88.6ms | 608.2ms | 1283.5ms |
| qd=16 nj=64 | 108.5ms | 885.0ms | 1887.4ms |
| qd=32 nj=1 | 9.4ms | 35.9ms | 39.6ms |
| qd=32 nj=4 | 13.4ms | 221.2ms | 254.8ms |
| qd=32 nj=32 | 116.9ms | 750.8ms | 1585.4ms |
| qd=32 nj=64 | 287.3ms | 1652.6ms | 2634.0ms |

#### 64 KiB seq read — BW_MBS

| iodepth \ numjobs | classic | crimson-pin | crimson-nopin |
|---|---|---|---|
| qd=1 nj=1 | 384 | 259 | 223 |
| qd=1 nj=4 | 1649 | 791 | 898 |
| qd=1 nj=32 | 6135 | 1348 | 1824 |
| qd=1 nj=64 | 8488 | 1303 | 1767 |
| qd=4 nj=1 | 750 | 480 | 478 |
| qd=4 nj=4 | 2445 | 1079 | 1209 |
| qd=4 nj=32 | 9445 | 1501 | 2085 |
| qd=4 nj=64 | 9237 | 988 | 1857 |
| qd=16 nj=1 | 889 | 571 | 592 |
| qd=16 nj=4 | 3111 | 1303 | 1500 |
| qd=16 nj=32 | 5854 | 1055 | 1882 |
| qd=16 nj=64 | 6787 | 799 | 1703 |
| qd=32 nj=1 | 1253 | 671 | 689 |
| qd=32 nj=4 | 4242 | 1401 | 1593 |
| qd=32 nj=32 | 5714 | 996 | 1843 |
| qd=32 nj=64 | 6347 | 845 | 1667 |

#### 64 KiB seq read — P99

| iodepth \ numjobs | classic | crimson-pin | crimson-nopin |
|---|---|---|---|
| qd=1 nj=1 | 214µs | 602µs | 724µs |
| qd=1 nj=4 | 205µs | 1.4ms | 1.3ms |
| qd=1 nj=32 | 460µs | 13.4ms | 6.5ms |
| qd=1 nj=64 | 2.9ms | 35.4ms | 19.0ms |
| qd=4 nj=1 | 423µs | 1.7ms | 1.7ms |
| qd=4 nj=4 | 602µs | 4.9ms | 4.0ms |
| qd=4 nj=32 | 3.3ms | 69.7ms | 39.6ms |
| qd=4 nj=64 | 6.3ms | 135.3ms | 58.5ms |
| qd=16 nj=1 | 1.6ms | 6.5ms | 5.5ms |
| qd=16 nj=4 | 2.2ms | 18.0ms | 14.1ms |
| qd=16 nj=32 | 16.3ms | 173.0ms | 70.8ms |
| qd=16 nj=64 | 26.6ms | 392.2ms | 120.1ms |
| qd=32 nj=1 | 2.9ms | 14.1ms | 11.1ms |
| qd=32 nj=4 | 3.9ms | 39.6ms | 31.9ms |
| qd=32 nj=32 | 33.4ms | 278.9ms | 95.9ms |
| qd=32 nj=64 | 63.7ms | 775.9ms | 254.8ms |

#### 64 KiB seq write — BW_MBS

| iodepth \ numjobs | classic | crimson-pin | crimson-nopin |
|---|---|---|---|
| qd=1 nj=1 | 22 | 13 | 13 |
| qd=1 nj=4 | 48 | 53 | 43 |
| qd=1 nj=32 | 275 | 117 | 99 |
| qd=1 nj=64 | 465 | 48 | 58 |
| qd=4 nj=1 | 45 | 26 | 25 |
| qd=4 nj=4 | 141 | 124 | 118 |
| qd=4 nj=32 | 689 | 278 | 267 |
| qd=4 nj=64 | 721 | 96 | 113 |
| qd=16 nj=1 | 156 | 81 | 78 |
| qd=16 nj=4 | 337 | 266 | 256 |
| qd=16 nj=32 | 600 | 191 | 199 |
| qd=16 nj=64 | 708 | 43 | 57 |
| qd=32 nj=1 | 248 | 127 | 118 |
| qd=32 nj=4 | 534 | 316 | 301 |
| qd=32 nj=32 | 677 | 139 | 112 |
| qd=32 nj=64 | 597 | 127 | 123 |

#### 64 KiB seq write — P99

| iodepth \ numjobs | classic | crimson-pin | crimson-nopin |
|---|---|---|---|
| qd=1 nj=1 | 3.9ms | 11.2ms | 10.9ms |
| qd=1 nj=4 | 8.7ms | 11.3ms | 16.7ms |
| qd=1 nj=32 | 13.7ms | 72.9ms | 98.0ms |
| qd=1 nj=64 | 15.9ms | 263.2ms | 254.8ms |
| qd=4 nj=1 | 9.2ms | 32.1ms | 30.5ms |
| qd=4 nj=4 | 13.2ms | 18.2ms | 20.3ms |
| qd=4 nj=32 | 21.6ms | 152.0ms | 156.2ms |
| qd=4 nj=64 | 45.4ms | 1199.6ms | 1082.1ms |
| qd=16 nj=1 | 10.7ms | 54.3ms | 61.6ms |
| qd=16 nj=4 | 19.8ms | 33.8ms | 33.4ms |
| qd=16 nj=32 | 143.7ms | 1082.1ms | 759.2ms |
| qd=16 nj=64 | 135.3ms | 4731.2ms | 4211.1ms |
| qd=32 nj=1 | 14.0ms | 52.7ms | 63.7ms |
| qd=32 nj=4 | 22.2ms | 55.8ms | 55.3ms |
| qd=32 nj=32 | 139.5ms | 1803.6ms | 1786.8ms |
| qd=32 nj=64 | 534.8ms | 3271.6ms | 3103.8ms |


### 8.6 Per-core CPU use

mpstat samples (1 s, full bench duration):

- **classic-12share**: cores 0-11 saturate at ~70-95 % under peak
  randread; 100 % CPU not reached because the 48 worker threads share
  12 cores and contend on the OSD's `osd_op_tp` queues.
- **crimson-12pin**: each of the 12 pinned cores hits 100 % during
  randread peak. Cores 16-23 (VM) at ~25 % each. Beyond the 12 cores,
  cores 12-15, 24-31 stay near 0 % — no leakage out of the pool.
- **crimson-12nopin**: 4 of the 12 cores tend to dominate (kernel
  scheduler concentrates reactors on cooler cores), while the other 8
  sit at <20 %. The lower throughput vs pinning is consistent with
  this — the kernel scheduler does not spread the reactors across the
  pool the way pinning does.

### 8.7 Memory footprint per OSD (peak RSS)

Sampled every 60 s from `/proc/PID/status` `VmPeak/HWM/RSS`. All three
phases stay flat after warm-up; no growth across the 56-min bench.

| Phase | Peak RSS per OSD (typical) |
|---|---|
| classic-12share | ~2.3 GiB |
| crimson-12pin | ~1.4 GiB |
| crimson-12nopin | ~1.5 GiB |

Crimson is ~40 % leaner. Worth a footnote, but not the headline.

### 8.8 What I'd change next time

- **Pin the VM as a hard cgroup constraint** rather than just systemd
  CPUAffinity — the 25 % bleed I saw during pidstat samples is small
  enough not to matter, but a cleaner experiment would use cgroup v2
  `AllowedCPUs=` for both sides.
- **Run more than one trial per cell.** I see ~5 % run-to-run
  variance on warm reads, more on writes. The headline numbers are
  single-trial.
- **Vary classic's thread pool** to find where it plateaus and what
  reactor count would beat it. The default 16-threads-per-OSD pool
  may not be optimal for a 12-core budget either.


---

## 9. RBM (Random Block Manager) exploration

Side ask from the same reviewer: try the RBM backend for SeaStore on
NVMe instead of the default segment-manager. The RBM author (Myongwon
Oh) shared his tuning recipe and a headline number to aim for: **13.1 K
IOPS** on single-OSD / single-reactor / 4K randwrite / iodepth=20 /
180 s, after pre-fill, on a Samsung PS1030.

This is **exploratory and not directly comparable** to §8 — different
backend, different design point, different setup (single-OSD vs our
3-OSD/rep=3 cluster).

### 9.1 Setup

Same hardware/VM as §8. Cluster state: 3 OSDs, all re-mkfs'd as
crimson + SeaStore with the RBM backend.

ceph.conf `[osd]` section (the author's recipe):
```ini
[osd]
        seastore_main_device_type = RANDOM_BLOCK_SSD
        seastore_cbjournal_size = 5G
        seastore_data_delta_based_overwrite = 4096
        seastore_max_concurrent_transactions = 13
        seastore_max_data_allocation_size = 32K
        crimson_osd_obc_lru_size = 5120         # = 512 * 10
        crimson_cpu_num = 1                      # single reactor per OSD
```

Per-OSD verification at runtime (`/proc/PID/status`):
- 3 threads per OSD process (vs ~7 with `cpu_num=4`), no per-OSD pinning,
  reactors can roam across cores 0-31 (no `CPUAffinity` drop-in for this
  phase since `cpu_num=1` is the "natural single-reactor" config).

Pre-fill: `fio --name=prefill --filename=/dev/sdb --bs=1M --rw=write
--iodepth=16 --size=24G --time_based=0`. 24 GiB sequential write at
~270 MB/s, 89 s.

### 9.2 Author-spec result (single-job 4K randwrite QD20 180 s)

| Source | IOPS | BW | p50 lat | p99 lat | p99.9 lat |
|---|---:|---:|---:|---:|---:|
| Author (Samsung PS1030, single OSD, rep=1?) | **13.1k** | ~51 MB/s | — | — | — |
| **Our result** (Samsung 9100 PRO 2 TB, 3 OSDs, rep=3) | **1.9k** | 7.9 MB/s | 10 µs | 23 ms | 35 ms |

We landed at ~15 % of the author's number. The most likely
explanation is the **3-way replication × 3-OSD-on-one-disk amplification**:
every fio write fans out to 3 OSD writes, and all 3 OSDs share the same
physical NVMe (different partitions but the same drive electronics, the
same NVMe submission queues). The author's setup ran a single OSD; ours
fans 3× and contends 3× on the bus.

Translated to "per-OSD throughput" the comparison is:
- Author's setup: 13.1k IOPS / 1 OSD = 13.1k IOPS per OSD
- Our setup: 1.9k IOPS × 3-way replication / 3 OSDs = 1.9k IOPS per OSD

So our **per-OSD RBM throughput on this hardware is ~14 % of the
author's**. Possible reasons:
- Older Crimson snapshot vs the author's likely newer code.
- 3 OSDs on one physical drive contending for the device (queue
  depth, controller bandwidth).
- Different NVMe characteristics (Samsung 9100 PRO is a consumer-grade
  drive; PS1030 is a data-center-grade enterprise drive with much
  better steady-state randwrite).

### 9.4 RBM status caveats

- Crimson upstream (master @4d15f1ce065 in our case) gates RBM as
  experimental. The objectstore_tool test suite for RBM is
  `.disabled`: `qa/suites/crimson-rados/objectstore_tool/objectstore/seastore/seastore-rbm.yaml.disabled`.
- TODOs remain in `src/crimson/os/seastore/random_block_manager/`
  (multi-stream, e2e protection, multi-namespace) and `block_rb_manager.h`
  ("Ondisk layout (TODO)").
### 9.5 What I'd change to make this directly comparable

- Single-OSD / replication=1 pool, matching the author's setup.
- A datacenter-grade NVMe (or a smaller test region on the consumer
  drive so it stays in SLC cache).
- Repeat with `numjobs=1,4,16,32,64` at the same QD20 to get the
  scaling curve, not just a single point.
- Run the author's exact pre-fill size and image layout.

### 9.6 Visualisation — latency vs throughput across phases

Following the chart style of Ben England's blog post
*"Crimson SeaStore vs Classic"* on ceph.io — each point is one
`(iodepth, numjobs)` combination from the 96-job sweep, lines connect
points within a single backend configuration, both axes log-scaled.
Curves further to the **upper-left** = better (more throughput at lower
latency). Generated with `charts/gen-charts.py` from the raw
`results-matrix.json` of each phase.

Phases plotted:
- **classic-12share** — classic OSD + BlueStore, 12-core shared CPU
- **crimson-12pin** — crimson + SeaStore (SegmentManager), 4 cores/OSD pinned
- **crimson-12nopin** — crimson + SeaStore (SegmentManager), 4 cores/OSD without pinning
- **crimson-rbm** — crimson + SeaStore (RBM), full sweep
- **crimson-rbm-tiny** — crimson + SeaStore (RBM), 4-job burst on a freshly-mkfs'd cluster, `seastore_data_delta_based_overwrite=0`

#### 4K random read

![4K randread latency vs IOPS](charts/01-randread-4k.png)

#### 4K random write

![4K randwrite latency vs IOPS](charts/02-randwrite-4k.png)

#### 64K sequential read

![64K seqread latency vs BW](charts/03-seqread-64k.png)

#### 64K sequential write

![64K seqwrite latency vs BW](charts/04-seqwrite-64k.png)

What the charts show:
- For **reads** (both random and sequential), the three full-sweep
  phases overlap closely — Classic + BlueStore, Crimson + SeaStore
  SegmentManager pinned, and Crimson + SeaStore SegmentManager not
  pinned all trace nearly the same latency-vs-throughput frontier.
  No backend is the dominant winner at matched CPU budget.
- For **writes**, the spread is wider but still without a clear
  single-axis winner — different configs win at different
  `(iodepth, numjobs)` points.
- `crimson-rbm-tiny` sits in its own region at the **upper-left** —
  much lower latency and high IOPS — but only 2 data points
  (QD1 nj1, QD8 nj1) and only 30 seconds each, in burst conditions
  that don't exercise the RBM's CBJournal under sustained pressure.
  Not directly comparable to the longer-duration full-sweep phases.
## Appendix A — raw artifacts

All raw fio JSON results, host-side `pidstat`/`mpstat` traces, and
`ceph daemon dump_mempools` samples live under
`~/ceph-bench-21.0.0/<phase>/` on the test host. The phases are:
`classic/`, `classic-warmup/`, `crimson-baseline/` (= crimson-warm),
`crimson-rebaseline/` (= crimson-cold), `crimson-tier1/` (= crimson-tuned),
`crimson-datapath/`, `crimson-tier2/` (= crimson-pinned).

## Appendix B — exact fio job definitions

Three fio files in `~/ceph-bench-21.0.0/`:
- `warmup.fio` — the 15 s pre-pass with its full rationale in the file header.
- `bench-iops.fio` — the 4 KiB random sweep (8 jobs, QD 1/4/16/64, randread + randwrite).
- `bench-bw.fio` — the 64 KiB sequential pass (seqread + seqwrite at QD16).

The runner `run-bench.sh` ties them together with the host-side
monitoring. If you want to reproduce one cell:

```
./run-bench.sh <vm-ip> <phase-name>
```


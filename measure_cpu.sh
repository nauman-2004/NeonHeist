#!/bin/bash
# measure_cpu.sh -- sample a process's CPU% every 0.5s into a CSV
# (for the poster graph: capped vs uncapped burn).
# usage: ./measure_cpu.sh <pid> <out.csv> <seconds>
# The CSV has columns t,cpu. t is wall-clock seconds; cpu is percent of
# one core (100 = one full core). Reads utime+stime from /proc/<pid>/stat.
set -euo pipefail

pid=$1; out=$2; seconds=$3
clk=$(getconf CLK_TCK)
echo "t,cpu" > "$out"
prev_ticks=$(awk '{print $14+$15}' "/proc/$pid/stat")
prev_t=$(date +%s.%N)
t_end=$(awk -v t="$prev_t" -v s="$seconds" 'BEGIN{print t+s}')
while true; do
    sleep 0.5
    [ -d "/proc/$pid" ] || { echo "process $pid is gone"; break; }
    cur_ticks=$(awk '{print $14+$15}' "/proc/$pid/stat")
    now=$(date +%s.%N)
    awk -v t="$now" -v cur="$cur_ticks" -v prev="$prev_ticks" -v t0="$prev_t" -v clk="$clk" \
        'BEGIN{printf "%s,%.1f\n", t, (cur-prev)/clk/(t-t0)*100}' >> "$out"
    prev_ticks=$cur_ticks; prev_t=$now
    awk -v now="$now" -v end="$t_end" 'BEGIN{exit (now>=end)}' || break
done
echo "wrote $out"

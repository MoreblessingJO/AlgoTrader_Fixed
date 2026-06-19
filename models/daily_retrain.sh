#!/bin/bash
# models/daily_retrain.sh
# Runs at 02:00 UTC daily via cron.
# Retrains all CB models on accumulated live ticks then restarts the bot.

set -e
cd /root/algotrader_fixed

LOG=logs/retrain_cron.log
echo "" >> $LOG
echo "========================================" >> $LOG
echo "Daily retrain started: $(date -u)" >> $LOG
echo "========================================" >> $LOG

# Abort if disk is over 95% full
DISK_PCT=$(df / | awk 'NR==2 {gsub("%","",$5); print $5}')
if [ "$DISK_PCT" -ge 95 ]; then
    echo "ABORT: Disk ${DISK_PCT}% full — skipping retrain to protect system" >> $LOG
    exit 1
fi
echo "Disk usage: ${DISK_PCT}%" >> $LOG

# Count how many ticks we have per symbol
for f in data/ticks/*_ticks.csv; do
    rows=$(wc -l < "$f" 2>/dev/null || echo 0)
    echo "  $(basename $f): $((rows-1)) ticks" >> $LOG
done

# Retrain on all accumulated CSVs (no new API fetch needed)
python3 models/retrain_real.py --skip-fetch >> $LOG 2>&1

echo "Retrain done: $(date -u)" >> $LOG

# Snapshot performance: archive models + write daily_performance.csv + model_history.csv
python3 models/snapshot_performance.py >> $LOG 2>&1
echo "Performance snapshot saved: $(date -u)" >> $LOG

# Restart bot — kill ALL instances to prevent accumulation of ghost processes
echo "Stopping all bot instances..." >> $LOG
BOT_PID=$(pgrep -f "python3.*bot\.py" | head -1)
if [ -n "$BOT_PID" ]; then kill "$BOT_PID"; sleep 3; fi
pkill -f "python3.*bot\.py" 2>/dev/null || true
sleep 3
rm -f /root/algotrader_fixed/bot.pid

setsid python3 bot.py --paper >> logs/bot.log 2>&1 </dev/null &
disown

echo "Bot restarted with new models: $(date -u)" >> $LOG

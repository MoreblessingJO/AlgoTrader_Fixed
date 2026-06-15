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

# Count how many ticks we have per symbol
for f in data/ticks/*_ticks.csv; do
    rows=$(wc -l < "$f" 2>/dev/null || echo 0)
    echo "  $(basename $f): $((rows-1)) ticks" >> $LOG
done

# Retrain on all accumulated CSVs (no new API fetch needed)
python3 models/retrain_real.py --skip-fetch >> $LOG 2>&1

echo "Retrain done: $(date -u)" >> $LOG

# Restart bot to load new models
pkill -f "python3 bot.py" 2>/dev/null || true
sleep 3
setsid python3 bot.py --paper >> logs/bot.log 2>&1 </dev/null &
disown

echo "Bot restarted with new models: $(date -u)" >> $LOG

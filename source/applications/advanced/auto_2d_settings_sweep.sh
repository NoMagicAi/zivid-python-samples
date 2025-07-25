#!/usr/bin/env bash

set -euo pipefail

SWEEP_START=100
SWEEP_END=200
SWEEP_STEP=10
WHITE_RANGE_TOLERANCE=5
FOCUS_RANGE=500
MAX_GAIN=1
LOG_DIR=/tmp/auto_2d_settings

for (( WHITE_RANGE_START = SWEEP_START; WHITE_RANGE_START <= SWEEP_END; WHITE_RANGE_START += SWEEP_STEP )); do
  WHITE_RANGE_END=$(( WHITE_RANGE_START + WHITE_RANGE_TOLERANCE ))
  CALIBRATION_ID="white_range_${WHITE_RANGE_START}_${WHITE_RANGE_END}_focus_${FOCUS_RANGE}_max_gain_${MAX_GAIN}_by2x2"
  echo "$CALIBRATION_ID"
  python3 auto_2d_settings.py \
      --desired-focus-range  "$FOCUS_RANGE" \
      --desired-white-range  "$WHITE_RANGE_START" "$WHITE_RANGE_END" \
      --max-gain-override    "$MAX_GAIN" \
      --checkerboard-at-end-of-range \
      --use-projector \
      --pixel-sampling by2x2 \
      --log-dir "$LOG_DIR" \
      --calibration-id "$CALIBRATION_ID"
done

echo "Sweep complete, saved logs in $LOG_DIR"

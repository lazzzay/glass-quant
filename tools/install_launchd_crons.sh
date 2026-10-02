#!/usr/bin/env bash
#
# install_launchd_crons.sh: installs the Daily-Invest launchd jobs on macOS
# ==========================================================================================
# V7.10 · 2026-06-01
#
# Generates LaunchAgent plists for the daily pipeline into ~/Library/LaunchAgents
# and loads them with launchctl. Run once from the repository root:
#
#       cd /path/to/daily-invest-oss
#       bash tools/install_launchd_crons.sh
#
#   Afterwards 4 launchd jobs run automatically (label prefix configurable via
#   DAILYINVEST_LAUNCHD_PREFIX, default com.dailyinvest):
#     com.dailyinvest.news       Mon-Fri 06:30   news_sentiment_fetch.py
#     com.dailyinvest.morning    Mon-Fri 08:00   run_morning_pipeline.py (+ catch-up triggers)
#     com.dailyinvest.catalyst   Sun     20:00   catalyst_calendar.py
#     com.dailyinvest.gatekeeper Mon     09:45   observation_gatekeeper.py
#
#   Review first with --dry-run. A dry run writes the plists to tools/_launchd_preview/
#   (gitignored) and prints them. It never writes to ~/Library/LaunchAgents and loads
#   nothing, so no job can start at the next login.
#   A real install writes to ~/Library/LaunchAgents and loads the jobs. launchd then
#   also starts them after every login, until you run --uninstall.
#   Keep DAILYINVEST_LAUNCHD_PREFIX identical for verify_launchd_crons.py.
#
# IDEMPOTENT: a re-run overwrites the plists and re-bootstraps the jobs.
# Local only, no paid service, no network installs.
# NO trades, no writes to trade_journal.
#
# Flags:
#   --dry-run        write the plists to tools/_launchd_preview/ and print them (review only)
#   --python PATH    interpreter for the jobs (default: python3 from the current PATH)
#   --uninstall      unload all 4 jobs and remove their plists from ~/Library/LaunchAgents
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON=""
LA_DIR="$HOME/Library/LaunchAgents"
PREVIEW_DIR="$REPO_DIR/tools/_launchd_preview"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
PREFIX="${DAILYINVEST_LAUNCHD_PREFIX:-com.dailyinvest}"
DRY=0
MODE="install"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)   DRY=1 ;;
    --uninstall) MODE="uninstall" ;;
    --python)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        echo "ERROR: --python needs a path, for example --python /usr/bin/python3" >&2; exit 2
      fi
      PYTHON="$2"; shift ;;
    --python=*)  PYTHON="${1#--python=}" ;;
    *) echo "ERROR: unknown flag: $1 (use --dry-run, --python PATH or --uninstall)" >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$PYTHON" ]; then
  PYTHON="$(command -v python3 || true)"
fi
if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
  echo "ERROR: no usable python3 found (${PYTHON:-not in PATH}). Install python3 or pass --python PATH." >&2
  exit 1
fi

# Where the plists go: a dry run only writes a preview inside the repository.
if [ "$DRY" -eq 1 ]; then
  OUT_DIR="$PREVIEW_DIR"
else
  OUT_DIR="$LA_DIR"
fi

# --- Job-Definitionen: label|hour|minute|weekday(-1=taeglich-ohne)|program-args -----------
# weekday: leer = jeden Tag (kein Weekday-Key); 1..7 = Mo..So (launchd: 1=Mon..7=Sun, 0/7=Sun)
JOBS=(
  "news|6|30|MONFRI|${PYTHON}|${REPO_DIR}/tools/news_sentiment_fetch.py|--tier|A,B|--delay|0.4"
  "morning|8|0|MONFRI_CATCHUP1030|${PYTHON}|${REPO_DIR}/tools/run_morning_pipeline.py"
  "catalyst|20|0|7|${PYTHON}|${REPO_DIR}/tools/catalyst_calendar.py"
  "gatekeeper|9|45|1|${PYTHON}|${REPO_DIR}/tools/observation_gatekeeper.py"
)

emit_plist() {
  # args: label hour minute weekday-spec  then program args via $PROG_ARGS array
  local label="$1" hour="$2" minute="$3" wspec="$4"
  local plist="$OUT_DIR/${PREFIX}.${label}.plist"
  local logf="$REPO_DIR/tools/_logs/launchd_${label}.log"

  # Build StartCalendarInterval entries. MONFRI => 5 dict entries (Weekday 1..5).
  local sci=""
  add_entry_hm() {
    local wd="$1" h="$2" m="$3"
    sci+="    <dict>\n"
    [ -n "$wd" ] && sci+="      <key>Weekday</key><integer>${wd}</integer>\n"
    sci+="      <key>Hour</key><integer>${h}</integer>\n"
    sci+="      <key>Minute</key><integer>${m}</integer>\n"
    sci+="    </dict>\n"
  }
  add_entry() { add_entry_hm "$1" "$hour" "$minute"; }
  case "$wspec" in
    MONFRI) for wd in 1 2 3 4 5; do add_entry "$wd"; done ;;
    # MAC-SLEEP-CATCH-UP: the morning job also fires at 08:15, 08:45, 09:15 and
    # 10:30 Mon to Fri. A missed 08:00 run (the Mac was asleep; a scheduled wake
    # is not enough for GUI LaunchAgents with the lid closed) is made up at the
    # first trigger while the Mac is awake. catch_up_skip() turns every later
    # trigger into a no-op once a valid scan_<today>.json exists.
    MONFRI_CATCHUP1030)
      for wd in 1 2 3 4 5; do add_entry "$wd"; done
      for wd in 1 2 3 4 5; do add_entry_hm "$wd" 8 15; done
      for wd in 1 2 3 4 5; do add_entry_hm "$wd" 8 45; done
      for wd in 1 2 3 4 5; do add_entry_hm "$wd" 9 15; done
      for wd in 1 2 3 4 5; do add_entry_hm "$wd" 10 30; done ;;
    "")     add_entry "" ;;
    *)      add_entry "$wspec" ;;
  esac

  # ProgramArguments come from global PROG_ARGS array
  local pa=""
  for a in "${PROG_ARGS[@]}"; do
    pa+="    <string>${a}</string>\n"
  done

  printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' > "$plist"
  printf '%s\n' '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' >> "$plist"
  printf '%s\n' '<plist version="1.0">' >> "$plist"
  printf '%s\n' '<dict>' >> "$plist"
  printf '  <key>Label</key><string>%s.%s</string>\n' "$PREFIX" "$label" >> "$plist"
  printf '  <key>ProgramArguments</key>\n  <array>\n' >> "$plist"
  printf "$pa" >> "$plist"
  printf '  </array>\n' >> "$plist"
  printf '  <key>WorkingDirectory</key><string>%s</string>\n' "$REPO_DIR" >> "$plist"
  printf '  <key>EnvironmentVariables</key>\n  <dict>\n' >> "$plist"
  printf '    <key>DAILYINVEST_REPO</key><string>%s</string>\n' "$REPO_DIR" >> "$plist"
  printf '    <key>DAILYINVEST_PYTHON</key><string>%s</string>\n' "$PYTHON" >> "$plist"
  printf '    <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>\n' >> "$plist"
  printf '  </dict>\n' >> "$plist"
  printf '  <key>StartCalendarInterval</key>\n  <array>\n' >> "$plist"
  printf "$sci" >> "$plist"
  printf '  </array>\n' >> "$plist"
  printf '  <key>StandardOutPath</key><string>%s</string>\n' "$logf" >> "$plist"
  printf '  <key>StandardErrorPath</key><string>%s</string>\n' "$logf" >> "$plist"
  printf '  <key>RunAtLoad</key><false/>\n' >> "$plist"
  printf '%s\n' '</dict>' >> "$plist"
  printf '%s\n' '</plist>' >> "$plist"

  echo "$plist"
}

bootstrap_job() {
  local label="$1"
  local plist="$LA_DIR/${PREFIX}.${label}.plist"
  # bootout if already loaded (ignore error if not loaded)
  launchctl bootout "${DOMAIN}/${PREFIX}.${label}" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$plist"
  launchctl enable "${DOMAIN}/${PREFIX}.${label}"
}

uninstall_job() {
  local label="$1"
  local plist="$LA_DIR/${PREFIX}.${label}.plist"
  launchctl bootout "${DOMAIN}/${PREFIX}.${label}" 2>/dev/null || true
  rm -f "$plist"
  echo "removed: $plist"
}

echo "Daily-Invest launchd installer (V7.10)"
echo "  REPO_DIR = $REPO_DIR"
echo "  PYTHON   = $PYTHON"
echo "  DOMAIN   = $DOMAIN"
echo "  MODE     = $MODE  DRY=$DRY"
echo "  OUT_DIR  = $OUT_DIR"
echo ""

if [ "$MODE" = "uninstall" ]; then
  if [ "$DRY" -eq 1 ]; then
    echo "ERROR: --uninstall and --dry-run cannot be combined." >&2; exit 2
  fi
  for spec in "${JOBS[@]}"; do
    IFS='|' read -r label _ _ _ _rest <<< "$spec"
    uninstall_job "$label"
  done
  echo "Uninstall done."
  exit 0
fi

mkdir -p "$OUT_DIR"
if [ "$DRY" -eq 0 ]; then
  mkdir -p "$REPO_DIR/tools/_logs"
fi

for spec in "${JOBS[@]}"; do
  IFS='|' read -r label hour minute wspec rest <<< "$spec"
  # Reconstruct program args (everything after the 4 fixed fields)
  IFS='|' read -r -a allfields <<< "$spec"
  PROG_ARGS=("${allfields[@]:4}")
  plist="$(emit_plist "$label" "$hour" "$minute" "$wspec")"
  echo "written: $plist"
  if [ "$DRY" -eq 1 ]; then
    echo "---- $plist ----"
    cat "$plist"
    echo "----------------"
  else
    bootstrap_job "$label"
    echo "  -> bootstrapped and enabled: ${PREFIX}.${label}"
  fi
done

if [ "$DRY" -eq 1 ]; then
  echo ""
  echo "Dry run: the plists above are in $OUT_DIR for review."
  echo "Nothing was written to $LA_DIR and nothing was loaded."
  echo "To install for real, run: bash tools/install_launchd_crons.sh"
else
  echo ""
  echo "Done. The jobs now also start after every login, until you run --uninstall."
  echo "Check the status with:"
  echo "  launchctl list | grep ${PREFIX}"
  echo "  $PYTHON tools/verify_launchd_crons.py"
fi

#!/bin/bash
# Stock Scanner Scheduler — Mac launchd helper
# Run this script to install, uninstall, or test the daily scheduler

PLIST_NAME="com.stockscanner.daily"
PLIST_SRC="$(dirname "$0")/$PLIST_NAME.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

case "$1" in

  install)
    echo "📦 Installing daily stock scanner..."

    # Check the plist file has been configured
    if grep -q "YOURUSERNAME" "$PLIST_SRC"; then
      echo "❌ You haven't edited the plist yet!"
      echo "   Open com.stockscanner.daily.plist and replace:"
      echo "   - YOURUSERNAME with your actual Mac username (run: whoami)"
      echo "   - PASTE_YOUR_YOUTUBE_API_KEY_HERE with your YouTube API key"
      echo "   - PASTE_YOUR_ANTHROPIC_API_KEY_HERE with your Anthropic API key"
      exit 1
    fi

    cp "$PLIST_SRC" "$PLIST_DEST"
    launchctl load "$PLIST_DEST"
    echo "✅ Installed! Scanner will run daily at 8:00 AM."
    echo "   Logs will appear in ~/StocksScraper/launchd_out.log"
    ;;

  uninstall)
    echo "🗑  Uninstalling daily stock scanner..."
    launchctl unload "$PLIST_DEST" 2>/dev/null
    rm -f "$PLIST_DEST"
    echo "✅ Uninstalled."
    ;;

  test)
    echo "🧪 Running scanner once right now (not waiting for 8am)..."
    cd "$(dirname "$0")"
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 scanner.py
    echo "📤 Pushing updated stocks.json to GitHub..."
    git add stocks.json seen_content.json channel_id_cache.json
    git diff --cached --quiet || git commit -m "📈 Auto-update stocks.json $(date +'%Y-%m-%d')"
    git stash
    git pull --rebase origin main
    git stash pop
    git push
    echo "✅ Done."
    ;;

  status)
    echo "📊 Scheduler status:"
    launchctl list | grep stockscanner || echo "Not currently loaded."
    echo ""
    echo "Last run output (last 20 lines):"
    tail -20 "$(dirname "$0")/launchd_out.log" 2>/dev/null || echo "(no log yet)"
    ;;

  *)
    echo "Usage: $0 {install|uninstall|test|status}"
    echo ""
    echo "  install    — set up daily 8am run"
    echo "  uninstall  — remove the scheduler"
    echo "  test       — run scanner right now"
    echo "  status     — check if scheduler is running + show last log"
    ;;
esac

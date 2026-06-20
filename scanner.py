name: Daily Stock List Update

on:
  schedule:
    - cron: '0 13 * * *'   # 8 AM Eastern daily
  workflow_dispatch:         # manual trigger anytime

# Allow the workflow to commit back to the repo
permissions:
  contents: write

jobs:
  update-stocks:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run scanner
        env:
          YOUTUBE_API_KEY:   ${{ secrets.YOUTUBE_API_KEY }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: python scanner.py

      # This is the key step — commits the updated stocks.json back to the repo
      # so your trading script always reads the latest version
      - name: Commit updated stocks.json
        run: |
          git config user.name  "Stock Scanner Bot"
          git config user.email "bot@github-actions"
          git add stocks.json seen_content.json
          git diff --cached --quiet || git commit -m "📈 Auto-update stocks.json $(date +'%Y-%m-%d')"
          git pull --rebase origin main
          git push

      - name: Save daily summary as artifact
        uses: actions/upload-artifact@v4
        with:
          name: summary-${{ github.run_number }}
          path: summary_*.json
          retention-days: 30

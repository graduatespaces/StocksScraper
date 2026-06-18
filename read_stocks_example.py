# ─────────────────────────────────────────────────────────────────
# HOW TO READ stocks.json IN YOUR EXISTING TRADING SCRIPT
#
# Replace your hardcoded stock list with this snippet.
# stocks.json lives in the same GitHub repo and is updated daily.
# ─────────────────────────────────────────────────────────────────

import json
from pathlib import Path

# Option A — reading locally (if trading script is in the same repo)
def get_stock_list() -> list[str]:
    stocks_file = Path(__file__).parent / "stocks.json"
    if not stocks_file.exists():
        return []
    data = json.loads(stocks_file.read_text())
    return sorted(data.keys())


# Option B — reading directly from GitHub (if trading script is elsewhere)
def get_stock_list_remote(github_user: str, repo: str, branch: str = "main") -> list[str]:
    import urllib.request
    url  = f"https://raw.githubusercontent.com/{github_user}/{repo}/{branch}/stocks.json"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    return sorted(data.keys())


# ── Usage example ─────────────────────────────────────────────────

# BEFORE (hardcoded — what you have now):
# STOCKS = ["AAPL", "NVDA", "TSLA"]

# AFTER (dynamic — reads from stocks.json):
STOCKS = get_stock_list()

# Or if your trading script is in a different location:
# STOCKS = get_stock_list_remote("your-github-username", "your-repo-name")

print(f"Trading with {len(STOCKS)} stocks: {STOCKS}")

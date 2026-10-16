# Price Tracker with Email Alerts

A Selenium-based price tracker developed against [Books to Scrape](https://books.toscrape.com/). Site-specific CSS selectors are kept in `config.json`; the tracking, storage, logging, and email logic is separate.

## Requirements

- Python 3.14 (or a compatible modern Python version)
- Google Chrome
- Internet access
- SMTP credentials for real email alerts

## Setup in Windows PowerShell

Run these commands from the folder containing `main.py`:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` with your SMTP details. For Gmail, create an App Password if your account supports it; do not use your normal account password. Keep `.env` private and do not commit it.

## Run

```powershell
.\.venv\Scripts\python.exe main.py
```

Enter a product URL, target price, and polling interval in minutes. The loop waits using `time.sleep()` between checks. Use `Ctrl+C` to stop.

## Files

- `config.json`: site-specific CSS selectors, currency label, and Selenium timeouts.
- `price_history.json`: last known price, last check timestamp, and alert state; created at runtime.
- `price_log.csv`: timestamped successful checks, errors, and email alert outcomes; created at runtime.
- `price_tracker.log`: operational logs; created at runtime.
- `.env`: SMTP settings and password; never share this file.

## Changing to another website

Update `title_selector`, `price_selector`, and `currency` in `config.json`. The target website may have additional access restrictions or page behavior, so verify its terms and selectors before tracking it.

## Alert behavior

An email is sent when the current price is strictly below the target and an alert is not already active. It will not send the same alert on every polling cycle. When the price returns to the target or higher, the alert is re-armed for a future drop. If email delivery fails, the tracker logs the failure and retries on the next check.

## Security and usage notes

- Never commit or share `.env`.
- Use an SMTP app password or dedicated SMTP credential.
- Choose a reasonable polling interval and follow the target site's terms and access policies.
- The JSON state is for one tracked URL at a time; tracking a different URL starts a fresh state.

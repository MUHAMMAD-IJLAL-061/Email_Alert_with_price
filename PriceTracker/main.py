from __future__ import annotations

import csv
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "price_history.json"
CSV_LOG_PATH = BASE_DIR / "price_log.csv"
APP_LOG_PATH = BASE_DIR / "price_tracker.log"
ENV_PATH = BASE_DIR / ".env"


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(APP_LOG_PATH, encoding="utf-8"),
        ],
        force=True,
    )


def load_config() -> dict[str, Any]:
    """Load site-specific selectors; all tracker logic stays site-independent."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Missing {CONFIG_PATH.name}. Keep it beside main.py and restore it from the project files."
        )
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{CONFIG_PATH.name} is not valid JSON: {exc}") from exc

    required = ("title_selector", "price_selector", "currency")
    missing = [key for key in required if not str(config.get(key, "")).strip()]
    if missing:
        raise ValueError(f"Missing required config values: {', '.join(missing)}")
    return config


def validate_product_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a complete product URL beginning with http:// or https://.")
    return url


def parse_price(price_text: str) -> Decimal:
    """Extract a decimal number from a displayed price such as '£51.77'."""
    cleaned = price_text.replace(",", "")
    match = re.search(r"\d+(?:\.\d{1,2})?", cleaned)
    if not match:
        raise ValueError(f"Could not find a numeric price in: {price_text!r}")
    try:
        value = Decimal(match.group(0)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid price value: {price_text!r}") from exc
    if value < 0:
        raise ValueError("A product price cannot be negative.")
    return value


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write JSON through a temporary file to reduce the risk of a partial save."""
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.write("\n")
    temporary_path.replace(path)


class EmailNotifier:
    """Send price-drop alerts using SMTP SSL or STARTTLS."""

    def __init__(self) -> None:
        load_dotenv(ENV_PATH)
        self.host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
        self.port = int(os.getenv("SMTP_PORT", "465").strip())
        self.username = os.getenv("SMTP_USERNAME", "").strip()
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.sender = os.getenv("EMAIL_FROM", self.username).strip()
        self.recipient = os.getenv("EMAIL_TO", "").strip()

    @property
    def is_configured(self) -> bool:
        return all((self.host, self.port, self.username, self.password, self.sender, self.recipient))

    def send_price_drop(self, product: dict[str, Any], target_price: Decimal) -> None:
        if not self.is_configured:
            raise RuntimeError(
                "Email is not configured. Copy .env.example to .env and fill in SMTP_USERNAME, "
                "SMTP_PASSWORD, EMAIL_FROM, and EMAIL_TO."
            )

        currency = product["currency"]
        current_price = Decimal(str(product["price"]))
        message = EmailMessage()
        message["Subject"] = f"Price drop alert: {product['title']}"
        message["From"] = self.sender
        message["To"] = self.recipient
        message.set_content(
            "Your tracked product is now below your target price.\n\n"
            f"Product: {product['title']}\n"
            f"Current price: {currency} {current_price:.2f}\n"
            f"Target price: {currency} {target_price:.2f}\n"
            f"Product URL: {product['url']}\n"
            f"Checked at: {product['checked_at']}\n"
        )

        context = ssl.create_default_context()
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=30) as server:
                server.login(self.username, self.password)
                server.send_message(message)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(self.username, self.password)
                server.send_message(message)


@dataclass
class PriceTracker:
    product_url: str
    target_price: Decimal
    interval_minutes: int
    config: dict[str, Any]
    notifier: EmailNotifier

    def __post_init__(self) -> None:
        self.product_url = validate_product_url(self.product_url)
        if self.target_price <= 0:
            raise ValueError("Target price must be greater than zero.")
        if self.interval_minutes < 1:
            raise ValueError("Check interval must be at least 1 minute.")
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not STATE_PATH.exists():
            return self._empty_state()
        try:
            with STATE_PATH.open("r", encoding="utf-8") as file:
                stored = json.load(file)
            if stored.get("product_url") != self.product_url:
                logging.info("The tracked URL changed; starting fresh price state for this product.")
                return self._empty_state()
            return {
                "product_url": self.product_url,
                "last_known_price": stored.get("last_known_price"),
                "last_checked_at": stored.get("last_checked_at"),
                "alert_active": bool(stored.get("alert_active", False)),
            }
        except (json.JSONDecodeError, OSError, AttributeError, TypeError) as exc:
            logging.warning("Could not read saved price state (%s). Starting with fresh state.", exc)
            return self._empty_state()

    def _empty_state(self) -> dict[str, Any]:
        return {
            "product_url": self.product_url,
            "last_known_price": None,
            "last_checked_at": None,
            "alert_active": False,
        }

    def _save_state(self) -> None:
        write_json_atomic(STATE_PATH, self.state)

    def _append_csv_log(
        self,
        *,
        checked_at: str,
        status: str,
        title: str = "",
        price: str = "",
        details: str = "",
    ) -> None:
        file_exists = CSV_LOG_PATH.exists() and CSV_LOG_PATH.stat().st_size > 0
        with CSV_LOG_PATH.open("a", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow(["timestamp", "status", "title", "url", "current_price", "target_price", "details"])
            writer.writerow([
                checked_at,
                status,
                title,
                self.product_url,
                price,
                f"{self.target_price:.2f}",
                details,
            ])

    def get_product_details(self) -> dict[str, Any]:
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        driver = None
        try:
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(int(self.config.get("page_load_timeout_seconds", 30)))
            driver.get(self.product_url)
            wait = WebDriverWait(driver, int(self.config.get("element_timeout_seconds", 15)))
            title_element = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self.config["title_selector"]))
            )
            price_element = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self.config["price_selector"]))
            )
            title = title_element.text.strip()
            raw_price = price_element.text.strip()
            if not title:
                raise ValueError("The product title element was empty.")
            price = parse_price(raw_price)
            return {
                "title": title,
                "price": str(price),
                "currency": str(self.config["currency"]),
                "url": self.product_url,
            }
        except TimeoutException as exc:
            raise RuntimeError(
                "Timed out waiting for the product title/price. Check the URL and CSS selectors in config.json."
            ) from exc
        except WebDriverException as exc:
            raise RuntimeError(f"Selenium/browser error: {exc.msg}") from exc
        finally:
            if driver is not None:
                driver.quit()

    def check_once(self) -> None:
        checked_at = utc_timestamp()
        try:
            product = self.get_product_details()
            current_price = Decimal(product["price"])
            product["checked_at"] = checked_at
            below_target = current_price < self.target_price

            logging.info(
                "Price check | product=%s | current=%s %s | target=%s %s",
                product["title"], product["currency"], current_price,
                product["currency"], self.target_price,
            )
            self._append_csv_log(
                checked_at=checked_at,
                status="success",
                title=product["title"],
                price=f"{current_price:.2f}",
                details="Below target" if below_target else "Target not reached",
            )

            previous_price = self.state.get("last_known_price")
            if previous_price is not None:
                try:
                    previous_decimal = Decimal(str(previous_price))
                    if current_price < previous_decimal:
                        logging.info("Price decreased from %s to %s.", previous_decimal, current_price)
                except InvalidOperation:
                    logging.warning("Saved previous price was invalid; replacing it with the current price.")

            self.state["last_known_price"] = f"{current_price:.2f}"
            self.state["last_checked_at"] = checked_at

            if below_target and not self.state.get("alert_active", False):
                try:
                    self.notifier.send_price_drop(product, self.target_price)
                    self.state["alert_active"] = True
                    self._append_csv_log(
                        checked_at=checked_at,
                        status="alert_sent",
                        title=product["title"],
                        price=f"{current_price:.2f}",
                        details=f"Email sent to {self.notifier.recipient}",
                    )
                    logging.info("Price-drop email sent successfully.")
                except Exception as exc:
                    self._append_csv_log(
                        checked_at=checked_at,
                        status="alert_failed",
                        title=product["title"],
                        price=f"{current_price:.2f}",
                        details=str(exc),
                    )
                    logging.error("Price is below target, but email alert failed: %s", exc)
            elif not below_target and self.state.get("alert_active", False):
                self.state["alert_active"] = False
                logging.info("Price is back at or above target; a future drop can trigger a new alert.")

            self._save_state()
        except Exception as exc:
            self._append_csv_log(
                checked_at=checked_at,
                status="error",
                details=f"{type(exc).__name__}: {exc}",
            )
            logging.exception("Price check failed; the tracker will retry at the next interval.")

    def run_forever(self) -> None:
        logging.info("Tracker started for %s", self.product_url)
        logging.info("Target: %s %s | Check interval: %s minute(s)", self.config["currency"], self.target_price, self.interval_minutes)
        logging.info("Press Ctrl+C to stop the tracker.")
        try:
            while True:
                self.check_once()
                time.sleep(self.interval_minutes * 60)
        except KeyboardInterrupt:
            logging.info("Tracker stopped by user.")


def prompt_for_settings() -> tuple[str, Decimal, int]:
    product_url = input("Enter the product URL: ").strip()
    validate_product_url(product_url)

    try:
        target_price = Decimal(input("Enter your target price (in the site's currency): ").strip())
    except InvalidOperation as exc:
        raise ValueError("Target price must be a valid number, such as 25.99.") from exc
    if target_price <= 0:
        raise ValueError("Target price must be greater than zero.")

    try:
        interval = int(input("Check interval in minutes (minimum 1): ").strip())
    except ValueError as exc:
        raise ValueError("Check interval must be a whole number of minutes.") from exc
    if interval < 1:
        raise ValueError("Check interval must be at least 1 minute.")
    return product_url, target_price.quantize(Decimal("0.01")), interval


def main() -> None:
    setup_logging()
    try:
        config = load_config()
        product_url, target_price, interval = prompt_for_settings()
        notifier = EmailNotifier()
        if not notifier.is_configured:
            logging.warning(
                "Email settings are incomplete. Tracking will run, but a price-drop email cannot be sent until .env is configured."
            )
        tracker = PriceTracker(
            product_url=product_url,
            target_price=target_price,
            interval_minutes=interval,
            config=config,
            notifier=notifier,
        )
        tracker.run_forever()
    except (ValueError, FileNotFoundError, OSError) as exc:
        logging.error("Cannot start tracker: %s", exc)
    except Exception:
        logging.exception("Unexpected application error.")


if __name__ == "__main__":
    main()

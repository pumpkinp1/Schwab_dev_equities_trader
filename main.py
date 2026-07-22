# Note: to run, enter "python ./main.py" into terminal or "python C:\Users\jerem\Desktop\MarketManager\main.py"
# Core architecture and logic designed by Jeremiah Patlan, with use of Schwabdev API
# Code implementation augmented with AI assistance

# 1. IMPORTS & DEPENDENCIES
import csv
import datetime
import json
import logging
import os
import time
from collections import deque

try:
    import schwabdev
except ImportError:  # pragma: no cover - fallback for local unit tests
    schwabdev = None

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - fallback for local unit tests
    def load_dotenv():
        return False

try:
    import requests
except ImportError:  # pragma: no cover - fallback when requests is unavailable directly
    requests = None

# 2. GLOBAL CONSTANTS
load_dotenv()

logging.basicConfig(level=logging.INFO)

SYMBOL = "SPY"
ASSET_TYPE = "EQUITY"
ORDER_SESSION = "NORMAL"
ORDER_DURATION = "DAY"
ORDER_STRATEGY = "SINGLE"
MARKET_OPEN_HOUR = 6
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 13
MARKET_CLOSE_MINUTE = 0
SLEEP_INTERVAL_SECONDS = 0.2
SMA_WINDOW_SIZES = [4, 8, 30]
WHIP_CHECK_MINUTES = 5
MIN_AVG_ABS_SMA_SLOPE = 0.03
ORDER_RETRY_ATTEMPTS = 3
ORDER_RETRY_DELAY_SECONDS = 2.0

# 3. CLASSES & DATA MODELS
class MarketData:
    def __init__(self):
        self.latest_price = None
        self.last_quote_time = None
        self.closes4 = deque(maxlen=4)
        self.closes8 = deque(maxlen=8)
        self.closes30 = deque(maxlen=30)
        self.sma4 = None
        self.previous_sma4 = None
        self.sma8 = None
        self.previous_sma8 = None
        self.sma30 = None
        self.previous_sma30 = None
        self.position = "FLAT"
        self.active_sma_window = 4
        self.waiting_after_loss = False
        self.resume_time = None
        self.waiting_for_reversal = False
        self.locked_trend = None
        self.last_loss_sma_window = None
        self.pending_sma_window = None
        self.pending_loss_stage = 0
        self.whip_check_started_at = None
        self.sma_slopes = {
            4: deque(maxlen=WHIP_CHECK_MINUTES),
            8: deque(maxlen=WHIP_CHECK_MINUTES),
            30: deque(maxlen=WHIP_CHECK_MINUTES),
        }


class Trade:
    def __init__(self):
        self.side = None
        self.entry_time = None
        self.entry_price = None
        self.quantity = 0
        self.entry_sma_window = None
        self.entry_avg_abs_slope = None
        self.exit_time = None
        self.exit_price = None
        self.exit_quantity = 0
        self.exit_sma_window = None
        self.exit_avg_abs_slope = None
        self.pnl = 0.0
        self.closed = False


class SessionStats:
    def __init__(self):
        self.current_trade = None
        self.trade_history = []
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.orders = []
        self.trades_by_window = {4: 0, 8: 0, 30: 0}
        self.wins_by_window = {4: 0, 8: 0, 30: 0}
        self.losses_by_window = {4: 0, 8: 0, 30: 0}
        self.pnl_by_window = {4: 0.0, 8: 0.0, 30: 0.0}


class TelegramBotFramework:
    def __init__(self, status_ref, stats_ref, market_ref):
        self.status = status_ref
        self.stats = stats_ref
        self.market = market_ref

    def handle_command(self, command):
        text = (command or "").strip()
        if not text:
            return "No command received."

        if text in {"/help", "/start"}:
            return self._help_text()
        if text == "/status":
            return self._format_status()
        if text == "/pnl":
            return f"Session P/L: ${self.stats.total_pnl:.2f}"
        if text == "/cash":
            return f"Cash: ${self.status['cash']:.2f}"
        if text == "/positions":
            return json.dumps(self.status["positions"], indent=2)
        if text == "/orders":
            return json.dumps(self.stats.orders[-10:], indent=2)
        if text == "/pause":
            self.status["paused"] = True
            self.status["running"] = False
            return "Trading paused."
        if text == "/resume":
            self.status["paused"] = False
            self.status["running"] = True
            return "Trading resumed."
        if text == "/shutdown":
            self.status["shutdown_requested"] = True
            return "Shutdown requested."
        if text == "/log":
            return "Log stream ready."
        if text == "/errors":
            return "No errors reported."
        if text == "/restart":
            self.status["shutdown_requested"] = True
            return "Restart requested."
        if text == "/ping":
            return "pong"
        return "Unknown command. Use /help."

    def _help_text(self):
        return "\n".join([
            "Available commands:",
            "/help - show this help text",
            "/status - show shared bot status",
            "/pnl - show session pnl",
            "/cash - show current cash",
            "/positions - show current positions",
            "/orders - show recent orders",
            "/pause - pause the trading loop",
            "/resume - resume the trading loop",
            "/shutdown - request shutdown",
            "/log - show log placeholder",
            "/errors - show error placeholder",
            "/restart - request restart",
            "/ping - health check",
        ])

    def _format_status(self):
        return json.dumps(self.status, indent=2)


# 4. HELPER / UTILITY FUNCTIONS
client = None
account_hash = None
market = MarketData()
stats = SessionStats()
streamer = None
position_size = 1
max_loss_dollars = None
target_gain_dollars = None
status = {
    "running": True,
    "paused": False,
    "daily_pnl": 0.0,
    "unrealized": 0.0,
    "cash": 0.0,
    "positions": [],
    "shutdown_requested": False,
    "target_gain_reached": False,
}
telegram_bot = None


def now_hms():
    return datetime.datetime.now().strftime("%H:%M:%S")


def order_log(message):
    print(f"[{now_hms()}] {message}")


def initialize_client():
    global client, account_hash

    if schwabdev is None:
        raise RuntimeError("schwabdev is required to connect to the broker.")

    client = schwabdev.Client(
        os.getenv("appkey"),
        os.getenv("appsecret"),
        os.getenv("callback_url")
    )

    linked_accounts = client.linked_accounts().json()
    account_hash = linked_accounts[0].get("hashValue")


def get_user_config():
    global position_size, max_loss_dollars, target_gain_dollars

    raw_position = os.getenv("POSITION_SIZE")
    if raw_position is None:
        raw_position = input("Enter position size (shares): ").strip()
    if not raw_position:
        position_size = 1
    else:
        position_size = int(raw_position)

    raw_loss = os.getenv("MAX_ALLOWABLE_LOSS")
    if raw_loss is None:
        raw_loss = input("Enter max allowable loss per session in dollars: ").strip()
    if not raw_loss:
        max_loss_dollars = 100.0
    else:
        max_loss_dollars = float(raw_loss)

    raw_target = os.getenv("TARGET_GAIN")
    if raw_target is None:
        raw_target = input("Enter target gain per session in dollars: ").strip()
    if not raw_target:
        target_gain_dollars = 100.0
    else:
        target_gain_dollars = float(raw_target)

    print(f"Trading size set to {position_size} share(s).")
    print(f"Max allowable loss set to ${max_loss_dollars:.2f}.")
    print(f"Target gain set to ${target_gain_dollars:.2f}.")


def update_status():
    status["daily_pnl"] = round(stats.total_pnl, 2)
    status["unrealized"] = 0.0
    if market.position != "FLAT":
        current_size = position_size
        if stats.current_trade is not None and stats.current_trade.quantity > 0:
            current_size = stats.current_trade.quantity
        status["positions"] = [{
            "symbol": SYMBOL,
            "side": market.position,
            "size": current_size,
        }]
    else:
        status["positions"] = []
    status["cash"] = 0.0
    status["running"] = not status["paused"] and not status["shutdown_requested"]


def build_order_payload(action, quantity):
    instruction = {
        "BUY": "BUY",
        "SELL": "SELL",
        "SHORT": "SELL_SHORT",
        "COVER": "BUY_TO_COVER",
    }[action]

    return {
        "orderType": "MARKET",
        "session": ORDER_SESSION,
        "duration": ORDER_DURATION,
        "orderStrategyType": ORDER_STRATEGY,
        "orderLegCollection": [
            {
                "instruction": instruction,
                "quantity": quantity,
                "instrument": {
                    "symbol": SYMBOL,
                    "assetType": ASSET_TYPE,
                },
            }
        ],
    }


def place_order(action, quantity=None):
    order_quantity = position_size if quantity is None else quantity
    order = build_order_payload(action, order_quantity)
    response = None

    for attempt in range(1, ORDER_RETRY_ATTEMPTS + 1):
        try:
            response = client.place_order(account_hash, order)
            break
        except Exception as exc:
            is_network_error = isinstance(exc, OSError)
            if requests is not None:
                is_network_error = is_network_error or isinstance(exc, requests.exceptions.ConnectionError)

            if not is_network_error:
                raise

            order_log(f"Network error placing order ({action}) attempt {attempt}/{ORDER_RETRY_ATTEMPTS}: {exc}")
            if attempt < ORDER_RETRY_ATTEMPTS:
                time.sleep(ORDER_RETRY_DELAY_SECONDS)

    if response is None:
        order_log(f"Unable to place {action} order after {ORDER_RETRY_ATTEMPTS} attempts.")
        return None

    order_log(f"{action} {order_quantity} {SYMBOL} | status={response.status_code}")

    location = response.headers.get("location")
    order_id = location.split("/")[-1] if location else None
    stats.orders.append({
        "action": action,
        "quantity": order_quantity,
        "timestamp": datetime.datetime.now().isoformat(),
        "status_code": response.status_code,
        "order_id": order_id,
    })
    update_status()
    return response


def buy(quantity=None):
    return place_order("BUY", quantity)


def sell(quantity=None):
    return place_order("SELL", quantity)


def short(quantity=None):
    return place_order("SHORT", quantity)


def cover(quantity=None):
    return place_order("COVER", quantity)


def open_trade(side, fill_price=None, quantity=None):
    if fill_price is None and market.latest_price is None:
        return None

    stats.current_trade = Trade()
    stats.current_trade.side = side
    stats.current_trade.entry_price = fill_price if fill_price is not None else market.latest_price
    stats.current_trade.quantity = position_size if quantity is None else int(quantity)
    stats.current_trade.entry_time = datetime.datetime.now()
    stats.current_trade.entry_sma_window = market.active_sma_window
    stats.current_trade.entry_avg_abs_slope = get_average_abs_slope(market.active_sma_window)
    return stats.current_trade


def get_fill_details(order_id, timeout_seconds=5.0, poll_interval=0.1):
    if not order_id:
        return None

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            response = client.order_details(account_hash, order_id)
            order = response.json()
        except Exception as exc:
            logging.warning("Unable to read order details for %s: %s", order_id, exc)
            break

        status_code = str(order.get("status", "")).upper()
        if status_code == "FILLED":
            activity_collection = order.get("orderActivityCollection") or []
            total_quantity = 0.0
            total_notional = 0.0
            for activity in activity_collection:
                execution_legs = activity.get("executionLegs") or []
                for leg in execution_legs:
                    price = leg.get("price")
                    quantity = leg.get("quantity")
                    if price is None or quantity is None:
                        continue
                    leg_price = float(price)
                    leg_quantity = float(quantity)
                    total_quantity += leg_quantity
                    total_notional += leg_price * leg_quantity

            if total_quantity > 0:
                return {
                    "price": total_notional / total_quantity,
                    "quantity": int(round(total_quantity)),
                }

        time.sleep(poll_interval)

    return None


def get_fill_price(order_id, timeout_seconds=5.0, poll_interval=0.1):
    details = get_fill_details(order_id, timeout_seconds, poll_interval)
    if details is None:
        return None
    return details["price"]


def save_trade_history_csv(trade_record):
    output_path = os.path.join(os.path.dirname(__file__), "trade_history.csv")
    fieldnames = [
        "side",
        "quantity",
        "exit_quantity",
        "entry_time",
        "exit_time",
        "entry_price",
        "exit_price",
        "entry_sma_window",
        "exit_sma_window",
        "entry_avg_abs_slope",
        "exit_avg_abs_slope",
        "pnl",
        "closed",
    ]

    with open(output_path, "a", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            writer.writeheader()

        writer.writerow({
            "side": trade_record.get("side"),
            "quantity": trade_record.get("quantity"),
            "exit_quantity": trade_record.get("exit_quantity"),
            "entry_time": trade_record.get("entry_time"),
            "exit_time": trade_record.get("exit_time"),
            "entry_price": trade_record.get("entry_price"),
            "exit_price": trade_record.get("exit_price"),
            "entry_sma_window": trade_record.get("entry_sma_window"),
            "exit_sma_window": trade_record.get("exit_sma_window"),
            "entry_avg_abs_slope": trade_record.get("entry_avg_abs_slope"),
            "exit_avg_abs_slope": trade_record.get("exit_avg_abs_slope"),
            "pnl": trade_record.get("pnl"),
            "closed": trade_record.get("closed"),
        })


def abort_if_loss_limit_reached():
    if max_loss_dollars is None:
        return False

    if stats.total_pnl <= -max_loss_dollars:
        status["shutdown_requested"] = True
        print(f"Max allowable loss reached. Session P/L ${stats.total_pnl:.2f} is below the limit of ${max_loss_dollars:.2f}.")
        raise SystemExit(0)

    return False


def close_all_positions():
    if market.position == "LONG":
        response = sell(position_size)
        if response is None:
            order_log("Unable to close LONG position due to order placement failure.")
            return False
        if response.status_code in (200, 201):
            location = response.headers.get("location")
            order_id = location.split("/")[-1] if location else None
            fill_details = get_fill_details(order_id)
            fill_price = fill_details["price"] if fill_details is not None else None
            if stats.current_trade is not None:
                stats.current_trade.exit_quantity = (
                    fill_details["quantity"] if fill_details is not None else stats.current_trade.quantity
                )
            close_trade(fill_price)
            market.position = "FLAT"
            return True
        order_log("SELL failed while closing position")
        print(response.status_code)
        print(response.text)
        return False

    if market.position == "SHORT":
        response = cover(position_size)
        if response is None:
            order_log("Unable to close SHORT position due to order placement failure.")
            return False
        if response.status_code in (200, 201):
            location = response.headers.get("location")
            order_id = location.split("/")[-1] if location else None
            fill_details = get_fill_details(order_id)
            fill_price = fill_details["price"] if fill_details is not None else None
            if stats.current_trade is not None:
                stats.current_trade.exit_quantity = (
                    fill_details["quantity"] if fill_details is not None else stats.current_trade.quantity
                )
            close_trade(fill_price)
            market.position = "FLAT"
            return True
        order_log("COVER failed while closing position")
        print(response.status_code)
        print(response.text)
        return False

    return True


def pause_for_target_gain():
    global position_size, max_loss_dollars

    if not close_all_positions():
        return

    status["paused"] = True
    status["running"] = False
    status["target_gain_reached"] = True
    print(f"Target gain reached. Session P/L is ${stats.total_pnl:.2f}.")

    while True:
        answer = input("Target gain reached. Continue trading? (y/n): ").strip().lower()
        if answer in {"y", "yes"}:
            print(f"Current position size: {position_size} share(s).")
            print(f"Current max loss limit: ${max_loss_dollars:.2f}.")
            adjust = input("Adjust these values before continuing? (y/n): ").strip().lower()
            if adjust in {"y", "yes"}:
                new_position = input(f"Enter new position size or press Enter to keep {position_size}: ").strip()
                if new_position:
                    position_size = int(new_position)
                new_loss = input(f"Enter new max loss or press Enter to keep {max_loss_dollars:.2f}: ").strip()
                if new_loss:
                    max_loss_dollars = float(new_loss)
                print(f"Updated position size: {position_size} share(s).")
                print(f"Updated max loss limit: ${max_loss_dollars:.2f}.")

            status["paused"] = False
            status["running"] = True
            status["target_gain_reached"] = False
            update_status()
            return

        if answer in {"n", "no"}:
            status["shutdown_requested"] = True
            return

        print("Please answer y or n.")


def check_target_gain_reached():
    if target_gain_dollars is None:
        return False

    if stats.total_pnl >= target_gain_dollars and not status["target_gain_reached"]:
        pause_for_target_gain()
        return True

    return False


def close_trade(exit_price=None):
    if stats.current_trade is None:
        return None

    trade = stats.current_trade
    trade.exit_price = exit_price if exit_price is not None else market.latest_price
    trade.exit_time = datetime.datetime.now()
    trade.exit_sma_window = market.active_sma_window
    trade.exit_avg_abs_slope = get_average_abs_slope(market.active_sma_window)
    if trade.exit_quantity <= 0:
        trade.exit_quantity = trade.quantity

    effective_quantity = min(trade.quantity, trade.exit_quantity)

    if trade.side == "LONG":
        trade.pnl = (trade.exit_price - trade.entry_price) * effective_quantity
    else:
        trade.pnl = (trade.entry_price - trade.exit_price) * effective_quantity

    trade.closed = True
    stats.total_trades += 1
    stats.total_pnl += trade.pnl
    trade_window = trade.entry_sma_window if trade.entry_sma_window in stats.trades_by_window else market.active_sma_window
    stats.trades_by_window[trade_window] += 1
    stats.pnl_by_window[trade_window] += trade.pnl

    if trade.pnl > 0:
        stats.wins += 1
        stats.wins_by_window[trade_window] += 1
        market.pending_loss_stage = 0
        market.pending_sma_window = None
        market.last_loss_sma_window = None
        market.whip_check_started_at = None
        market.waiting_after_loss = False
        market.waiting_for_reversal = False
        market.locked_trend = None
        market.resume_time = None
    else:
        stats.losses += 1
        stats.losses_by_window[trade_window] += 1
        print("Loss detected.")

    trade_record = {
        "side": trade.side,
        "quantity": trade.quantity,
        "exit_quantity": trade.exit_quantity,
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat(),
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "entry_sma_window": trade.entry_sma_window,
        "exit_sma_window": trade.exit_sma_window,
        "entry_avg_abs_slope": trade.entry_avg_abs_slope,
        "exit_avg_abs_slope": trade.exit_avg_abs_slope,
        "pnl": trade.pnl,
        "closed": trade.closed,
    }
    stats.trade_history.append(trade_record)
    save_trade_history_csv(trade_record)

    print()
    print("========== SESSION ==========")
    print(f"Position     : {market.position}")
    print(f"Trades       : {stats.total_trades}")
    print(f"Wins         : {stats.wins}")
    print(f"Losses       : {stats.losses}")
    win_rate = 100 * stats.wins / stats.total_trades if stats.total_trades else 0.0
    print(f"Win Rate     : {win_rate:.1f}%")
    print(f"Session P/L  : ${stats.total_pnl:.2f}")
    for window in SMA_WINDOW_SIZES:
        trades_window = stats.trades_by_window[window]
        wins_window = stats.wins_by_window[window]
        losses_window = stats.losses_by_window[window]
        pnl_window = stats.pnl_by_window[window]
        win_rate_window = (100 * wins_window / trades_window) if trades_window else 0.0
        print(
            f"SMA{window} Stats : trades={trades_window} wins={wins_window} losses={losses_window} "
            f"win_rate={win_rate_window:.1f}% pnl=${pnl_window:.2f}"
        )
    print("=============================")
    print()

    update_status()
    abort_if_loss_limit_reached()
    check_target_gain_reached()

    stats.current_trade = None
    return trade


def market_is_open():
    now = datetime.datetime.now()

    if now.weekday() >= 5:
        return False

    market_open = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
    market_close = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0)

    return market_open <= now < market_close


def my_handler(message):
    msg = json.loads(message)

    if "data" not in msg:
        return

    for service in msg["data"]:
        for quote in service["content"]:
            if "3" in quote:
                market.latest_price = float(quote["3"])
                market.last_quote_time = time.time()


def is_market_data_stale(market_data, max_age_seconds=10):
    if market_data.last_quote_time is None:
        return True
    return time.time() - market_data.last_quote_time > max_age_seconds


def reconnect_stream():
    global streamer

    if streamer is not None:
        try:
            streamer.stop()
        except Exception as exc:
            logging.warning("Unable to stop existing stream cleanly: %s", exc)

    if schwabdev is None:
        logging.warning("schwabdev is not installed; skipping stream reconnect.")
        return

    streamer = schwabdev.Stream(client)
    streamer.start(my_handler)
    streamer.send(streamer.level_one_equities(SYMBOL, "0,3"))
    print("Stream reconnected.")


# 5. MAIN BUSINESS LOGIC FUNCTIONS

def get_current_sma(window_size):
    if window_size == 4:
        return market.sma4
    if window_size == 8:
        return market.sma8
    return market.sma30


def get_previous_sma(window_size):
    if window_size == 4:
        return market.previous_sma4
    if window_size == 8:
        return market.previous_sma8
    return market.previous_sma30


def get_sma_closes(window_size):
    if window_size == 4:
        return market.closes4
    if window_size == 8:
        return market.closes8
    return market.closes30


def update_sma_slope(window_size, current_sma, previous_sma):
    if current_sma is None or previous_sma is None:
        return

    slope = current_sma - previous_sma
    market.sma_slopes[window_size].append(slope)
    avg_abs_slope = get_average_abs_slope(window_size)

    print(
        f"SMA{window_size}: {current_sma:.2f} | Slope: {slope:.4f} | AvgAbsSlope(5): {avg_abs_slope:.4f}"
    )


def get_trend_from_sma(previous_sma, current_sma):
    if current_sma > previous_sma:
        return "Increasing"
    if current_sma < previous_sma:
        return "Decreasing"
    return "Equal"


def get_next_sma_window(window_size):
    current_index = SMA_WINDOW_SIZES.index(window_size)
    return SMA_WINDOW_SIZES[(current_index + 1) % len(SMA_WINDOW_SIZES)]


def is_sma_ready(window_size):
    return len(get_sma_closes(window_size)) >= window_size


def whip_check_ready(window_size):
    return len(market.sma_slopes[window_size]) >= WHIP_CHECK_MINUTES


def get_average_abs_slope(window_size):
    slopes = market.sma_slopes[window_size]
    if not slopes:
        return 0.0
    return sum(abs(slope) for slope in slopes) / len(slopes)


def sma_is_whipping(window_size):
    return get_average_abs_slope(window_size) < MIN_AVG_ABS_SMA_SLOPE


def start_wait_for_reversal(trend, resume_time=None):
    market.waiting_after_loss = resume_time is not None
    market.resume_time = resume_time
    market.waiting_for_reversal = True
    market.locked_trend = trend


def prepare_loss_recovery(trend):
    current_window = market.active_sma_window
    next_window = get_next_sma_window(current_window)
    market.last_loss_sma_window = current_window
    market.pending_sma_window = next_window

    if market.pending_loss_stage == 0:
        market.pending_loss_stage = 1

        if not whip_check_ready(current_window):
            market.whip_check_started_at = datetime.datetime.now()
            print(
                f"Collecting slope data on SMA {current_window} before deciding whether to switch. "
                f"Need {WHIP_CHECK_MINUTES} points."
            )
            market.waiting_after_loss = False
            market.resume_time = None
            market.waiting_for_reversal = False
            market.locked_trend = trend
            return

        if sma_is_whipping(current_window):
            market.whip_check_started_at = datetime.datetime.now()
            avg_abs_slope = get_average_abs_slope(current_window)
            print(
                f"Current SMA {current_window} is whipping (avg |slope|={avg_abs_slope:.4f}). "
                "Waiting instead of switching."
            )
            market.waiting_after_loss = False
            market.resume_time = None
            market.waiting_for_reversal = False
            market.locked_trend = trend
            return

        if is_sma_ready(next_window):
            market.active_sma_window = next_window
            print(f"Switching to SMA {next_window} after a loss.")
            start_wait_for_reversal(trend)
            return

        market.whip_check_started_at = datetime.datetime.now()
        print(
            f"SMA {next_window} is not ready. Starting whip check on SMA {current_window} for at least {WHIP_CHECK_MINUTES} minutes."
        )
        market.waiting_after_loss = False
        market.resume_time = None
        market.waiting_for_reversal = False
        market.locked_trend = trend
        return

    market.pending_loss_stage = 2
    market.active_sma_window = next_window
    market.whip_check_started_at = None
    print(f"Second loss detected. Switching to SMA {next_window}.")
    print("Entering 3-minute observation mode before waiting for reversal.")
    start_wait_for_reversal(trend, datetime.datetime.now() + datetime.timedelta(minutes=3))


def evaluate_wait_state(trend, sma_label):
    now = datetime.datetime.now()

    if market.waiting_after_loss:
        print(f"{now.strftime('%H:%M')} Observation: SMA{sma_label}: {get_current_sma(market.active_sma_window):.2f} Trend: {trend}")
        if now < market.resume_time:
            return True

        market.waiting_after_loss = False
        print("Observation complete.")
        print(f"Waiting for reversal from {trend}.")
        return True

    if market.waiting_for_reversal:
        if trend == market.locked_trend:
            print(f"Waiting for reversal from {market.locked_trend}...")
            print(f"SMA Trend: {trend}")
            print()
            return True

        print(f"Trend changed from {market.locked_trend} to {trend}")
        print("Trading resumed.")
        print("Executing trade logic immediately on resumed trend.")
        market.waiting_for_reversal = False
        market.locked_trend = None
        print(f"SMA Trend: {trend}")
        print()
        return False

    if market.whip_check_started_at is not None:
        if not whip_check_ready(market.last_loss_sma_window):
            print(
                f"Collecting whip-check data for SMA {market.last_loss_sma_window}: "
                f"{len(market.sma_slopes[market.last_loss_sma_window])}/{WHIP_CHECK_MINUTES}"
            )
            return True

        if sma_is_whipping(market.last_loss_sma_window):
            avg_abs_slope = get_average_abs_slope(market.last_loss_sma_window)
            print(
                f"Whipping detected on SMA {market.last_loss_sma_window} "
                f"(avg |slope|={avg_abs_slope:.4f} < {MIN_AVG_ABS_SMA_SLOPE:.4f}). Waiting for the pattern to break."
            )
            return True

        next_window = market.pending_sma_window
        if next_window is not None and is_sma_ready(next_window):
            market.active_sma_window = next_window
            market.whip_check_started_at = None
            print(f"Whip check cleared. Switching to SMA {next_window}.")
            start_wait_for_reversal(trend)
            return True

        print(f"Whip check cleared, but SMA {next_window} is still not ready. Continuing to wait.")
        return True

    return False

def switch_to_next_sma_window(trend):
    prepare_loss_recovery(trend)


def evaluate_trade_signal():
    if market.active_sma_window == 4:
        previous_sma = market.previous_sma4
        current_sma = market.sma4
        sma_label = "4"
    elif market.active_sma_window == 8:
        previous_sma = market.previous_sma8
        current_sma = market.sma8
        sma_label = "8"
    else:
        previous_sma = market.previous_sma30
        current_sma = market.sma30
        sma_label = "30"

    if previous_sma is None:
        return

    if check_target_gain_reached():
        return

    print(f"Previous SMA {sma_label}: {previous_sma:.2f}")
    print(f"Current SMA {sma_label} : {current_sma:.2f}")

    if not market_is_open():
        print("Market closed - no trading")
        return

    if current_sma > previous_sma:
        trend = "Increasing"
    elif current_sma < previous_sma:
        trend = "Decreasing"
    else:
        trend = "Equal"

    if evaluate_wait_state(trend, sma_label):
        return

    if current_sma > previous_sma:
        if market.position == "FLAT":
            response = buy(position_size)
            if response is None:
                order_log("Order not sent due to network problem.")
                return
            if response.status_code in (200, 201):
                order_log("BUY successful")
                location = response.headers.get("location")
                order_id = location.split("/")[-1] if location else None
                order_log(f"Order ID: {order_id}")

                fill_details = get_fill_details(order_id)
                fill_price = fill_details["price"] if fill_details is not None else None
                market.position = "LONG"
                open_trade("LONG", fill_price, fill_details["quantity"] if fill_details is not None else position_size)
            else:
                order_log("BUY failed")
                print(response.status_code)
                print(response.text)

        if market.position == "SHORT":
            response = cover(position_size)
            if response is None:
                order_log("Order not sent due to network problem.")
                return
            if response.status_code in (200, 201):
                order_log("SHORT COVERED successful")
                location = response.headers.get("location")
                order_id = location.split("/")[-1] if location else None
                order_log(f"Order ID: {order_id}")

                fill_details = get_fill_details(order_id)
                fill_price = fill_details["price"] if fill_details is not None else None
                if stats.current_trade is not None:
                    stats.current_trade.exit_quantity = (
                        fill_details["quantity"] if fill_details is not None else stats.current_trade.quantity
                    )
                trade = close_trade(fill_price)
                market.position = "FLAT"

                if trade is not None and trade.pnl < 0:
                    switch_to_next_sma_window(trend)
                    return

                response = buy(position_size)
                if response is None:
                    order_log("Order not sent due to network problem.")
                    return
                if response.status_code in (200, 201):
                    order_log("ENTRY TO LONG successful")
                    location = response.headers.get("location")
                    order_id = location.split("/")[-1] if location else None
                    order_log(f"Order ID: {order_id}")

                    fill_details = get_fill_details(order_id)
                    fill_price = fill_details["price"] if fill_details is not None else None
                    market.position = "LONG"
                    open_trade("LONG", fill_price, fill_details["quantity"] if fill_details is not None else position_size)
                else:
                    order_log("BUY failed")
                    print(response.status_code)
                    print(response.text)
            else:
                order_log("COVER SHORT failed")
                print(response.status_code)
                print(response.text)

    elif current_sma < previous_sma:
        if market.position == "FLAT":
            response = short(position_size)
            if response is None:
                order_log("Order not sent due to network problem.")
                return
            if response.status_code in (200, 201):
                order_log("SHORT successful")
                location = response.headers.get("location")
                order_id = location.split("/")[-1] if location else None
                order_log(f"Order ID: {order_id}")

                fill_details = get_fill_details(order_id)
                fill_price = fill_details["price"] if fill_details is not None else None
                market.position = "SHORT"
                open_trade("SHORT", fill_price, fill_details["quantity"] if fill_details is not None else position_size)
            else:
                order_log("SHORT failed")
                print(response.status_code)
                print(response.text)

        if market.position == "LONG":
            response = sell(position_size)
            if response is None:
                order_log("Order not sent due to network problem.")
                return
            if response.status_code in (200, 201):
                order_log("SELL successful")
                location = response.headers.get("location")
                order_id = location.split("/")[-1] if location else None
                order_log(f"Order ID: {order_id}")

                fill_details = get_fill_details(order_id)
                fill_price = fill_details["price"] if fill_details is not None else None
                if stats.current_trade is not None:
                    stats.current_trade.exit_quantity = (
                        fill_details["quantity"] if fill_details is not None else stats.current_trade.quantity
                    )
                trade = close_trade(fill_price)
                market.position = "FLAT"

                if trade is not None and trade.pnl < 0:
                    switch_to_next_sma_window(trend)
                    return

                response = short(position_size)
                if response is None:
                    order_log("Order not sent due to network problem.")
                    return
                if response.status_code in (200, 201):
                    order_log("SHORT successful")
                    location = response.headers.get("location")
                    order_id = location.split("/")[-1] if location else None
                    order_log(f"Order ID: {order_id}")

                    fill_details = get_fill_details(order_id)
                    fill_price = fill_details["price"] if fill_details is not None else None
                    market.position = "SHORT"
                    open_trade("SHORT", fill_price, fill_details["quantity"] if fill_details is not None else position_size)
                else:
                    order_log("SHORT failed")
                    print(response.status_code)
                    print(response.text)
            else:
                order_log("SELL failed")
                print(response.status_code)
                print(response.text)

    else:
        print("EQUAL, NO ACTION")

    print(f"SMA Trend: {trend}")
    print()


def run_trading_loop():
    current_minute = datetime.datetime.now().minute

    while True:
        if status["shutdown_requested"]:
            print("Shutdown requested. Exiting trading loop.")
            break

        if status["paused"]:
            time.sleep(SLEEP_INTERVAL_SECONDS)
            continue

        now = datetime.datetime.now()

        if now.minute != current_minute:
            current_minute = now.minute
            if market.latest_price is not None:
                market.closes4.append(market.latest_price)
                market.closes8.append(market.latest_price)
                market.closes30.append(market.latest_price)

                if is_market_data_stale(market, 10):
                    print("No market data received for 10 seconds. Reconnecting stream...")
                    reconnect_stream()
                    continue

                if len(market.closes4) == 4:
                    market.previous_sma4 = market.sma4
                    market.sma4 = sum(market.closes4) / 4
                    update_sma_slope(4, market.sma4, market.previous_sma4)

                if len(market.closes8) == 8:
                    market.previous_sma8 = market.sma8
                    market.sma8 = sum(market.closes8) / 8
                    update_sma_slope(8, market.sma8, market.previous_sma8)

                if len(market.closes30) == 30:
                    market.previous_sma30 = market.sma30
                    market.sma30 = sum(market.closes30) / 30
                    update_sma_slope(30, market.sma30, market.previous_sma30)

                if market.active_sma_window == 4 and len(market.closes4) >= 4:
                    evaluate_trade_signal()
                elif market.active_sma_window == 8 and len(market.closes8) >= 8:
                    evaluate_trade_signal()
                elif market.active_sma_window == 30 and len(market.closes30) >= 30:
                    evaluate_trade_signal()
                else:
                    print(f"Collected {len(market.closes4)}/4, {len(market.closes8)}/8, {len(market.closes30)}/30 closes...")

        time.sleep(SLEEP_INTERVAL_SECONDS)


# 6. THE ENTRY POINT (Execution)
def main():
    global market, stats, telegram_bot

    get_user_config()
    initialize_client()
    market = MarketData()
    stats = SessionStats()
    update_status()

    telegram_bot = TelegramBotFramework(status, stats, market)

    global streamer
    reconnect_stream()

    print("Waiting for first quote...")
    while market.latest_price is None:
        time.sleep(0.1)

    print("First quote received.")
    run_trading_loop()


if __name__ == "__main__":
    main()


# language: Python 3.10+, file: bot.py
import asyncio
import logging
import time
import ccxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from friday import Friday, Mode
from chart import build_chart
from exchanges import EXCHANGES, DEFAULT_QUOTES, load_all_pairs
from config import TELEGRAM_TOKEN, DEFAULT_SYMBOL, PROFILES
import portfolio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("friday")

STATE = {}
PAGE_SIZE = 20
CACHE = {}

INTERVAL_OPTIONS = {
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "1h":  3600,
    "4h":  14400,
}


def get_state(chat_id: int) -> dict:
    return STATE.setdefault(chat_id, {
        "exchange": "bybit",
        "symbol": DEFAULT_SYMBOL,
        "quote": "USDT",
        "mode": "scalp",
        "profile": "high",
        "interval": 60,
        "live": False,
        "task": None,
        "last_sent": {},
        "page": 0,
        "search_mode": False,
    })


def kb_start() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Начать", callback_data="menu:exchanges")],
        [InlineKeyboardButton("Статистика", callback_data="menu:stats")],
        [InlineKeyboardButton("Текущая настройка", callback_data="menu:current")],
    ])


def kb_exchanges() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for key, label in EXCHANGES.items():
        row.append(InlineKeyboardButton(label, callback_data=f"ex:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("назад", callback_data="menu:start")])
    return InlineKeyboardMarkup(rows)


def kb_quotes(exchange_key: str) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for q in DEFAULT_QUOTES:
        row.append(InlineKeyboardButton(q, callback_data=f"quote:{exchange_key}:{q}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("назад", callback_data="menu:exchanges")])
    return InlineKeyboardMarkup(rows)


def kb_pairs(exchange_key: str, quote: str, pairs: list, page: int = 0) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(pairs) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = pairs[start:start + PAGE_SIZE]

    rows = []
    row = []
    for sym in chunk:
        base = sym.split("/")[0]
        row.append(InlineKeyboardButton(base, callback_data=f"pair:{exchange_key}:{quote}:{base}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("<", callback_data=f"page:{exchange_key}:{quote}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(">", callback_data=f"page:{exchange_key}:{quote}:{page+1}"))
    if len(nav) > 1:
        rows.append(nav)

    rows.append([InlineKeyboardButton("дешевые", callback_data=f"sort:{exchange_key}:{quote}:asc")])
    rows.append([InlineKeyboardButton("дорогие", callback_data=f"sort:{exchange_key}:{quote}:desc")])
    rows.append([InlineKeyboardButton("поиск", callback_data=f"search:{exchange_key}:{quote}")])
    rows.append([InlineKeyboardButton("назад", callback_data=f"back:quotes:{exchange_key}")])
    return InlineKeyboardMarkup(rows)


def kb_profile_select(exchange_key: str, symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("большая прибыль (редко, 4R)", callback_data="profile:high")],
        [InlineKeyboardButton("малая прибыль (часто, 1.5R)", callback_data="profile:low")],
        [InlineKeyboardButton("назад", callback_data=f"back:pairs:{exchange_key}")],
    ])


def kb_mode_select(exchange_key: str, symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("скальп 1m", callback_data=f"lmode:scalp:{exchange_key}:{symbol}")],
        [InlineKeyboardButton("интрадей 15m", callback_data=f"lmode:intraday:{exchange_key}:{symbol}")],
        [InlineKeyboardButton("свинг 4h", callback_data=f"lmode:swing:{exchange_key}:{symbol}")],
        [InlineKeyboardButton("назад", callback_data=f"pair:{exchange_key}:USDT:{symbol.split('/')[0]}")],
    ])


def kb_confirm_start(exchange_key: str, symbol: str, mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ЗАПУСТИТЬ", callback_data=f"start_live:{exchange_key}:{symbol}:{mode}")],
    ])


def kb_running(exchange_key: str, symbol: str, mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("СТОП", callback_data=f"stop:{exchange_key}:{symbol}")],
    ])


def kb_signal_buttons(symbol: str, action: str, price: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("купил", callback_data=f"bought:{symbol}:{action}:{price}")],
        [InlineKeyboardButton("пропустил", callback_data=f"skip:{symbol}")],
    ])


def kb_after_stop(exchange_key: str, symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1 минута", callback_data=f"go:{exchange_key}:{symbol}:60")],
        [InlineKeyboardButton("5 минут",  callback_data=f"go:{exchange_key}:{symbol}:300")],
        [InlineKeyboardButton("15 минут", callback_data=f"go:{exchange_key}:{symbol}:900")],
        [InlineKeyboardButton("1 час",    callback_data=f"go:{exchange_key}:{symbol}:3600")],
        [InlineKeyboardButton("4 часа",   callback_data=f"go:{exchange_key}:{symbol}:14400")],
        [InlineKeyboardButton("сменить профиль", callback_data=f"pair:{exchange_key}:USDT:{symbol.split('/')[0]}")],
        [InlineKeyboardButton("сменить пару", callback_data=f"back:pairs:{exchange_key}")],
        [InlineKeyboardButton("сменить биржу", callback_data="menu:exchanges")],
        [InlineKeyboardButton("в меню", callback_data="menu:start")],
    ])


def get_cached_pairs(exchange_key: str, quote: str) -> list:
    key = (exchange_key, quote)
    if key not in CACHE:
        CACHE[key] = load_all_pairs(exchange_key, quote)
    return CACHE[key]


def get_market_price(exchange_key: str, symbol: str) -> float:
    try:
        ex = getattr(ccxt, exchange_key)({"enableRateLimit": True})
        t = ex.fetch_ticker(symbol)
        return float(t["last"])
    except Exception:
        return 0.0


def sort_pairs_by_price(exchange_key: str, quote: str, order: str, pairs: list) -> list:
    limit = min(len(pairs), 60)
    rows = []
    for sym in pairs[:limit]:
        price = get_market_price(exchange_key, sym)
        if price > 0:
            rows.append((sym.split("/")[0], sym, price))
    rows.sort(key=lambda x: x[2], reverse=(order == "desc"))
    return rows


def kb_pairs_sorted(exchange_key: str, quote: str, sorted_rows: list, order: str) -> InlineKeyboardMarkup:
    rows = []
    for base, _sym, price in sorted_rows[:PAGE_SIZE]:
        label = base + " " + str(round(price, 4))
        rows.append([InlineKeyboardButton(label, callback_data=f"pair:{exchange_key}:{quote}:{base}")])
    rows.append([InlineKeyboardButton("дешевые", callback_data=f"sort:{exchange_key}:{quote}:asc")])
    rows.append([InlineKeyboardButton("дорогие", callback_data=f"sort:{exchange_key}:{quote}:desc")])
    rows.append([InlineKeyboardButton("назад", callback_data=f"back:quotes:{exchange_key}")])
    return InlineKeyboardMarkup(rows)


def stats_text() -> str:
    s = portfolio.stats()
    if s["total"] == 0 and not s["open"]:
        return "сделок пока нет.\nкогда бот пришлёт сигнал — жми «купил». когда продашь — «продал»."
    lines = ["статистика сделок", ""]
    if s["open"]:
        o = s["open"]
        lines.append("открыта: " + o["symbol"] + " " + o["side"] + " @ " + str(o["entry"]))
        lines.append("")
    lines.append("всего: " + str(s["total"]))
    lines.append("прибыльных: " + str(s["wins"]))
    lines.append("убыточных: " + str(s["losses"]))
    if s["total"] > 0:
        lines.append("winrate: " + str(round(s["winrate"], 1)) + "%")
    lines.append("суммарный PnL: " + str(round(s["total_pnl"], 2)) + "%")
    if s["wins"]:
        lines.append("средний выигрыш: +" + str(round(s["avg_win"], 2)) + "%")
    if s["losses"]:
        lines.append("средний проигрыш: " + str(round(s["avg_loss"], 2)) + "%")
    if s["pf"]:
        lines.append("профит-фактор: " + str(round(s["pf"], 2)))
    return "\n".join(lines)


async def _live_loop(chat_id: int, ctx, st: dict):
    interval = st["interval"]
    ex = st["exchange"]
    sym = st["symbol"]
    mode = st["mode"]
    profile_key = st.get("profile", "high")
    profile = PROFILES[profile_key]
    min_conf = profile["min_confidence"]
    cooldown = profile["cooldown"]
    tp_mult = profile["tp_mult"]

    await ctx.bot.send_message(
        chat_id,
        "запущена. " + sym + " на " + EXCHANGES[ex] + "\n"
        + "профиль: " + profile["label"] + "\n"
        + "порог " + str(int(min_conf * 100)) + "% · тейк " + str(tp_mult) + "R\n"
        + "проверка каждые " + str(interval) + " сек.",
        reply_markup=kb_running(ex, sym, mode),
    )

    st["last_sent"] = {}
    while st["live"]:
        try:
            friday = await asyncio.to_thread(Friday, sym, ex)
            sig = await asyncio.to_thread(friday.analyze_filtered, Mode(mode), tp_mult)

            if sig.action in ("BUY", "SELL") and sig.confidence >= min_conf:
                now = time.time()
                last_time = st["last_sent"].get(sig.action, 0)
                if now - last_time >= cooldown:
                    st["last_sent"][sig.action] = now

                    pos = portfolio.load().get("open")
                    header = "СИГНАЛ"
                    if pos:
                        cp = portfolio.current_pnl(sig.price)
                        if cp:
                            pnl_str = str(round(cp["pnl_pct"], 2))
                            header = ("твой PnL: " + pnl_str + "% · вход " + str(cp["entry"])
                                      + "\n\nСИГНАЛ")

                    await ctx.bot.send_message(
                        chat_id,
                        header + "\n" + sig.pretty(),
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb_signal_buttons(sym, sig.action, sig.price),
                    )
                    try:
                        path = await asyncio.to_thread(build_chart, sym, Mode(mode), sig, "chart_" + mode + ".png")
                        with open(path, "rb") as f:
                            await ctx.bot.send_photo(chat_id, photo=f,
                                                     caption=sig.action + " " + mode + " " + sym,
                                                     reply_markup=kb_running(ex, sym, mode))
                    except Exception as e:
                        log.warning("chart error: " + str(e))
        except Exception as e:
            log.warning("live error: " + str(e))

        for _ in range(interval):
            if not st["live"]:
                return
            await asyncio.sleep(1)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Пятница на связи.", reply_markup=kb_start())


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats_text(), reply_markup=kb_start())


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_chat.id)
    st["live"] = False
    if st.get("task"):
        st["task"].cancel()
        st["task"] = None
    await update.message.reply_text("остановлено. что дальше?", reply_markup=kb_after_stop(st["exchange"], st["symbol"]))


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    st = get_state(chat_id)
    data = q.data

    if data == "noop":
        return

    if data == "menu:start":
        await q.message.reply_text("меню:", reply_markup=kb_start())
        return

    if data == "menu:stats":
        await q.message.reply_text(stats_text(), reply_markup=kb_start())
        return

    if data == "menu:current":
        profile = PROFILES.get(st.get("profile", "high"), {})
        pos = portfolio.load().get("open")
        pos_str = ""
        if pos:
            pos_str = "\nоткрыта: " + pos["symbol"] + " " + pos["side"] + " @ " + str(pos["entry"])
        await q.message.reply_text(
            "биржа " + st["exchange"] + "\nпара " + st["symbol"] + "\nрежим " + str(st["mode"])
            + "\nпрофиль " + profile.get("label", "—") + pos_str,
            reply_markup=kb_start(),
        )
        return

    if data == "menu:exchanges":
        await q.message.reply_text("шаг 1: биржа", reply_markup=kb_exchanges())
        return

    if data.startswith("ex:"):
        ex = data.split(":", 1)[1]
        st["exchange"] = ex
        await q.message.reply_text("биржа " + EXCHANGES[ex] + "\nшаг 2: котировка", reply_markup=kb_quotes(ex))
        return

    if data.startswith("quote:"):
        _, ex, quote = data.split(":")
        st["quote"] = quote
        st["page"] = 0
        await q.message.reply_text("загружаю пары...")
        try:
            pairs = await asyncio.to_thread(get_cached_pairs, ex, quote)
        except Exception as e:
            await q.message.reply_text("ошибка: " + str(e))
            return
        await q.message.reply_text("найдено " + str(len(pairs)) + " пар. шаг 3: выбери", reply_markup=kb_pairs(ex, quote, pairs, 0))
        return

    if data.startswith("page:"):
        _, ex, quote, page_str = data.split(":")
        page = int(page_str)
        st["page"] = page
        pairs = get_cached_pairs(ex, quote)
        await q.message.reply_text("страница " + str(page + 1), reply_markup=kb_pairs(ex, quote, pairs, page))
        return

    if data.startswith("back:quotes:"):
        ex = data.split(":")[2]
        await q.message.reply_text("котировка:", reply_markup=kb_quotes(ex))
        return

    if data.startswith("pair:"):
        _, ex, quote, base = data.split(":")
        symbol = base + "/" + quote
        st["symbol"] = symbol
        st["quote"] = quote
        await q.message.reply_text(
            "пара " + symbol + "\n\nшаг 4: выбери профиль прибыли",
            reply_markup=kb_profile_select(ex, symbol),
        )
        return

    if data.startswith("profile:"):
        profile_key = data.split(":")[1]
        st["profile"] = profile_key
        profile = PROFILES[profile_key]
        await q.message.reply_text(
            "профиль: " + profile["label"] + "\n"
            + "порог " + str(int(profile["min_confidence"] * 100)) + "%\n"
            + "тейк " + str(profile["tp_mult"]) + "R\n"
            + "кулдаун " + str(profile["cooldown"]) + " сек\n\n"
            + "шаг 5: выбери режим",
            reply_markup=kb_mode_select(st["exchange"], st["symbol"]),
        )
        return

    if data.startswith("back:pairs:"):
        ex = data.split(":")[2]
        quote = st.get("quote", "USDT")
        pairs = get_cached_pairs(ex, quote)
        await q.message.reply_text("выбери пару", reply_markup=kb_pairs(ex, quote, pairs, st.get("page", 0)))
        return

    if data.startswith("sort:"):
        _, ex, quote, order = data.split(":")
        await q.message.reply_text("считаю цены...")
        pairs = get_cached_pairs(ex, quote)
        rows = await asyncio.to_thread(sort_pairs_by_price, ex, quote, order, pairs)
        if not rows:
            await q.message.reply_text("не удалось получить цены")
            return
        await q.message.reply_text("сортировка:", reply_markup=kb_pairs_sorted(ex, quote, rows, order))
        return

    if data.startswith("search:"):
        _, ex, quote = data.split(":")
        st["search_mode"] = True
        await q.message.reply_text("напиши код монеты, например SOL")
        return

    if data.startswith("lmode:"):
        _, mode_name, ex, symbol = data.split(":")
        st["exchange"] = ex
        st["symbol"] = symbol
        st["mode"] = mode_name
        profile = PROFILES.get(st.get("profile", "high"), {})
        await q.message.reply_text(
            "режим " + mode_name + "\n"
            + "профиль " + profile.get("label", "—") + "\n"
            + "биржа " + EXCHANGES[ex] + "\n"
            + "пара " + symbol + "\n\n"
            + "жми ЗАПУСТИТЬ",
            reply_markup=kb_confirm_start(ex, symbol, mode_name),
        )
        return

    if data.startswith("start_live:"):
        _, ex, symbol, mode = data.split(":")
        st["exchange"] = ex
        st["symbol"] = symbol
        st["mode"] = mode
        if mode == "scalp":
            default_interval = 60
        elif mode == "intraday":
            default_interval = 300
        else:
            default_interval = 900
        st["interval"] = default_interval
        st["live"] = True
        if st.get("task"):
            st["task"].cancel()
        st["task"] = asyncio.create_task(_live_loop(chat_id, ctx, st))
        return

    if data.startswith("go:"):
        _, ex, symbol, interval_str = data.split(":")
        st["exchange"] = ex
        st["symbol"] = symbol
        st["interval"] = int(interval_str)
        st["live"] = True
        if st.get("task"):
            st["task"].cancel()
        st["task"] = asyncio.create_task(_live_loop(chat_id, ctx, st))
        return

    if data.startswith("stop:"):
        st["live"] = False
        if st.get("task"):
            st["task"].cancel()
            st["task"] = None
        await q.message.reply_text("остановлено. что дальше?", reply_markup=kb_after_stop(st["exchange"], st["symbol"]))
        return

    if data.startswith("bought:"):
        _, symbol, action, price_str = data.split(":")
        price = float(price_str)
        portfolio.open_position(symbol, action, price)
        await q.message.reply_text(
            "позиция открыта\n" + action + " " + symbol + " @ " + str(price)
            + "\n\nпри следующем сигнале увидишь свой PnL.",
        )
        return

    if data.startswith("skip:"):
        await q.message.reply_text("пропущено. ждём следующий сигнал.")
        return

    if data.startswith("sold:"):
        _, symbol, price_str = data.split(":")
        price = float(price_str)
        trade = portfolio.close_position(price)
        if "error" in trade:
            await q.message.reply_text("нет открытой позиции")
            return
        s = portfolio.stats()
        await q.message.reply_text(
            "сделка закрыта\n"
            + trade["side"] + " " + trade["symbol"] + "\n"
            + "вход " + str(trade["entry"]) + " · выход " + str(trade["exit"]) + "\n"
            + "PnL: " + str(trade["pnl_pct"]) + "%\n"
            + "держишь " + str(trade["held_min"]) + " мин\n\n"
            + "всего сделок: " + str(s["total"]) + " · winrate " + str(round(s["winrate"], 1)) + "%"
        )
        return

    if data.startswith("hold:"):
        await q.message.reply_text("держим позицию дальше.")
        return


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_chat.id)
    if not st.get("search_mode"):
        return
    st["search_mode"] = False
    query = update.message.text.strip().upper()
    if not query:
        return
    ex = st["exchange"]
    quote = st.get("quote", "USDT")
    try:
        pairs = await asyncio.to_thread(get_cached_pairs, ex, quote)
        matches = [p for p in pairs if query in p.split("/")[0]]
        if not matches:
            await update.message.reply_text("пара с " + query + " не найдена")
            return
        rows = []
        for m in matches[:15]:
            rows.append([InlineKeyboardButton(m, callback_data=f"pair:{ex}:{quote}:{m.split('/')[0]}")])
        await update.message.reply_text("нашёл " + str(len(matches)) + ":", reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        await update.message.reply_text("ошибка: " + str(e))


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("нет TELEGRAM_TOKEN")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Пятница запущена. polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

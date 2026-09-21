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
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}


def get_state(chat_id):
    return STATE.setdefault(chat_id, {
        "exchange": "bybit",
        "symbol": DEFAULT_SYMBOL,
        "quote": "USDT",
        "mode": "scalp",
        "profile": "high",
        "signal_type": "signals",
        "interval": 60,
        "live": False,
        "task": None,
        "last_sent": {},
        "page": 0,
        "search_mode": False,
        "pending_buy": None,
    })


def kb_start():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Начать", callback_data="menu:exchanges")],
        [InlineKeyboardButton("Статистика", callback_data="menu:stats")],
        [InlineKeyboardButton("Текущая настройка", callback_data="menu:current")],
    ])


def kb_exchanges():
    rows = []
    row = []
    for key, label in EXCHANGES.items():
        row.append(InlineKeyboardButton(label, callback_data="ex:" + key))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("назад", callback_data="menu:start")])
    return InlineKeyboardMarkup(rows)


def kb_quotes(ex):
    rows = []
    row = []
    for q in DEFAULT_QUOTES:
        row.append(InlineKeyboardButton(q, callback_data="quote:" + ex + ":" + q))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("назад", callback_data="menu:exchanges")])
    return InlineKeyboardMarkup(rows)


def kb_pairs(ex, quote, pairs, page=0):
    total = max(1, (len(pairs) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total - 1))
    start = page * PAGE_SIZE
    chunk = pairs[start:start + PAGE_SIZE]
    rows = []
    row = []
    for sym in chunk:
        base = sym.split("/")[0]
        rows.append([InlineKeyboardButton(base, callback_data="pair:" + ex + ":" + quote + ":" + base)])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("<", callback_data="page:" + ex + ":" + quote + ":" + str(page - 1)))
    nav.append(InlineKeyboardButton(str(page + 1) + "/" + str(total), callback_data="noop"))
    if page < total - 1:
        nav.append(InlineKeyboardButton(">", callback_data="page:" + ex + ":" + quote + ":" + str(page + 1)))
    if len(nav) > 1:
        rows.append(nav)
    rows.append([InlineKeyboardButton("дешевые", callback_data="sort:" + ex + ":" + quote + ":asc")])
    rows.append([InlineKeyboardButton("дорогие", callback_data="sort:" + ex + ":" + quote + ":desc")])
    rows.append([InlineKeyboardButton("поиск", callback_data="search:" + ex + ":" + quote)])
    rows.append([InlineKeyboardButton("назад", callback_data="back:quotes:" + ex)])
    return InlineKeyboardMarkup(rows)


def kb_profile_select(ex, symbol):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("большая прибыль", callback_data="profile:high")],
        [InlineKeyboardButton("малая прибыль", callback_data="profile:low")],
        [InlineKeyboardButton("назад", callback_data="back:pairs:" + ex)],
    ])


def kb_type_select(ex, symbol):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("сигналы (BUY/SELL + кнопки)", callback_data="stype:signals")],
        [InlineKeyboardButton("прогноз (вверх / вниз)", callback_data="stype:forecast")],
        [InlineKeyboardButton("назад", callback_data="pair:" + ex + ":USDT:" + symbol.split("/")[0])],
    ])


def kb_mode_select(ex, symbol):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("скальп 1m", callback_data="lmode:scalp:" + ex + ":" + symbol)],
        [InlineKeyboardButton("интрадей 15m", callback_data="lmode:intraday:" + ex + ":" + symbol)],
        [InlineKeyboardButton("свинг 4h", callback_data="lmode:swing:" + ex + ":" + symbol)],
        [InlineKeyboardButton("назад", callback_data="stype:back")],
    ])


def kb_confirm_start(ex, symbol, mode):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ЗАПУСТИТЬ", callback_data="start_live:" + ex + ":" + symbol + ":" + mode)],
    ])


def kb_running(ex, symbol):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("СТОП", callback_data="stop:" + ex + ":" + symbol)],
    ])


def kb_signal_buttons(symbol, action, price):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("купил", callback_data="bought:" + symbol + ":" + action + ":" + str(price))],
        [InlineKeyboardButton("пропустил", callback_data="skip:" + symbol)],
    ])


def kb_after_stop(ex, symbol):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1 минута", callback_data="go:" + ex + ":" + symbol + ":60")],
        [InlineKeyboardButton("5 минут", callback_data="go:" + ex + ":" + symbol + ":300")],
        [InlineKeyboardButton("15 минут", callback_data="go:" + ex + ":" + symbol + ":900")],
        [InlineKeyboardButton("1 час", callback_data="go:" + ex + ":" + symbol + ":3600")],
        [InlineKeyboardButton("4 часа", callback_data="go:" + ex + ":" + symbol + ":14400")],
        [InlineKeyboardButton("сменить тип", callback_data="stype:back")],
        [InlineKeyboardButton("сменить пару", callback_data="back:pairs:" + ex)],
        [InlineKeyboardButton("сменить биржу", callback_data="menu:exchanges")],
        [InlineKeyboardButton("в меню", callback_data="menu:start")],
    ])


def get_cached_pairs(ex, quote):
    key = (ex, quote)
    if key not in CACHE:
        CACHE[key] = load_all_pairs(ex, quote)
    return CACHE[key]


def get_market_price(ex, symbol):
    try:
        e = getattr(ccxt, ex)({"enableRateLimit": True})
        t = e.fetch_ticker(symbol)
        return float(t["last"])
    except Exception:
        return 0.0


def sort_pairs_by_price(ex, quote, order, pairs):
    limit = min(len(pairs), 60)
    rows = []
    for sym in pairs[:limit]:
        price = get_market_price(ex, sym)
        if price > 0:
            rows.append((sym.split("/")[0], sym, price))
    rows.sort(key=lambda x: x[2], reverse=(order == "desc"))
    return rows


def kb_pairs_sorted(ex, quote, sorted_rows, order):
    rows = []
    for base, _sym, price in sorted_rows[:PAGE_SIZE]:
        label = base + " " + str(round(price, 4))
        rows.append([InlineKeyboardButton(label, callback_data="pair:" + ex + ":" + quote + ":" + base)])
    rows.append([InlineKeyboardButton("дешевые", callback_data="sort:" + ex + ":" + quote + ":asc")])
    rows.append([InlineKeyboardButton("дорогие", callback_data="sort:" + ex + ":" + quote + ":desc")])
    rows.append([InlineKeyboardButton("назад", callback_data="back:quotes:" + ex)])
    return InlineKeyboardMarkup(rows)


def stats_text():
    s = portfolio.stats()
    if s["total"] == 0 and not s["open"]:
        return "сделок пока нет.\nкогда бот пришлёт сигнал — жми «купил», введи количество."
    lines = ["статистика сделок", ""]
    if s["open"]:
        o = s["open"]
        lines.append("открыта: " + o["symbol"] + " " + o["side"])
        lines.append("вход " + str(o["entry"]) + " · кол-во " + str(o["qty"]))
        lines.append("")
    lines.append("всего: " + str(s["total"]))
    lines.append("прибыльных: " + str(s["wins"]))
    lines.append("убыточных: " + str(s["losses"]))
    if s["total"] > 0:
        lines.append("winrate: " + str(round(s["winrate"], 1)) + "%")
    lines.append("суммарный PnL: " + str(round(s["total_pnl"], 2)) + "%")
    if s["total_abs"]:
        lines.append("в USDT: " + str(round(s["total_abs"], 4)))
    if s["wins"]:
        lines.append("средний выигрыш: +" + str(round(s["avg_win"], 2)) + "%")
    if s["losses"]:
        lines.append("средний проигрыш: " + str(round(s["avg_loss"], 2)) + "%")
    if s["pf"]:
        lines.append("профит-фактор: " + str(round(s["pf"], 2)))
    return "\n".join(lines)


async def _live_loop(chat_id, ctx, st):
    interval = st["interval"]
    ex = st["exchange"]
    sym = st["symbol"]
    mode = st["mode"]
    profile = PROFILES[st.get("profile", "high")]
    min_conf = profile["min_confidence"]
    cooldown = profile["cooldown"]
    tp_mult = profile["tp_mult"]

    await ctx.bot.send_message(
        chat_id,
        "сигналы запущены. " + sym + " на " + EXCHANGES[ex] + "\n"
        + "профиль: " + profile["label"] + "\n"
        + "порог " + str(int(min_conf * 100)) + "% · тейк " + str(tp_mult) + "R\n"
        + "проверка каждые " + str(interval) + " сек.",
        reply_markup=kb_running(ex, sym),
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
                    header = "СИГНАЛ"
                    pos = portfolio.load().get("open")
                    if pos:
                        cp = portfolio.current_pnl(sig.price)
                        if cp:
                            header = ("твой PnL: " + str(round(cp["pnl_pct"], 2)) + "% ("
                                      + str(round(cp["pnl_abs"], 4)) + ")\n"
                                      + "вход " + str(cp["entry"]) + " · кол-во " + str(cp["qty"])
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
                                                     reply_markup=kb_running(ex, sym))
                    except Exception as e:
                        log.warning("chart error: " + str(e))
        except Exception as e:
            log.warning("live error: " + str(e))
        for _ in range(interval):
            if not st["live"]:
                return
            await asyncio.sleep(1)


async def _forecast_loop(chat_id, ctx, st):
    interval = st["interval"]
    ex = st["exchange"]
    sym = st["symbol"]
    mode = st["mode"]
    tp_mult = PROFILES[st.get("profile", "high")]["tp_mult"]

    await ctx.bot.send_message(
        chat_id,
        "прогноз запущен. " + sym + " на " + EXCHANGES[ex] + "\n"
        + "буду говорить куда идёт рынок и на сколько.\n"
        + "проверка каждые " + str(interval) + " сек.",
        reply_markup=kb_running(ex, sym),
    )

    last_dir = None
    last_sent_at = 0
    while st["live"]:
        try:
            friday = await asyncio.to_thread(Friday, sym, ex)
            sig = await asyncio.to_thread(friday.analyze_filtered, Mode(mode), tp_mult)
            price = sig.price

            if sig.action == "BUY":
                direction = "up"
                arrow = "↑"
                label = "ВВЕРХ"
            elif sig.action == "SELL":
                direction = "down"
                arrow = "↓"
                label = "ВНИЗ"
            else:
                direction = "flat"
                arrow = "→"
                label = "БОКОВИК"

            if sig.action == "HOLD":
                pot_pct = abs(sig.take_profit - price) / price * 100 if price else 0.0
            else:
                pot_pct = abs(sig.take_profit - price) / price * 100

            now = time.time()
            changed = (direction != last_dir)
            heartbeat = (now - last_sent_at >= 300)

            if changed or heartbeat:
                last_dir = direction
                last_sent_at = now
                if direction == "flat":
                    msg = arrow + " " + label + "\nцена: " + str(round(price, 4))
                else:
                    sign = "+" if direction == "up" else "-"
                    msg = (arrow + " " + label + "\n"
                           + "потенциал: " + sign + str(round(pot_pct, 2)) + "%\n"
                           + "уверенность: " + str(int(sig.confidence * 100)) + "%\n"
                           + "цена: " + str(round(price, 4)))
                await ctx.bot.send_message(chat_id, msg, reply_markup=kb_running(ex, sym))
        except Exception as e:
            log.warning("forecast error: " + str(e))
        for _ in range(interval):
            if not st["live"]:
                return
            await asyncio.sleep(1)


async def cmd_start(update, ctx):
    await update.message.reply_text("Пятница на связи.", reply_markup=kb_start())


async def cmd_stats(update, ctx):
    await update.message.reply_text(stats_text(), reply_markup=kb_start())


async def cmd_stop(update, ctx):
    st = get_state(update.effective_chat.id)
    st["live"] = False
    if st.get("task"):
        st["task"].cancel()
        st["task"] = None
    await update.message.reply_text("остановлено. что дальше?",
                                    reply_markup=kb_after_stop(st["exchange"], st["symbol"]))


async def on_button(update, ctx):
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
        extra = ""
        pos = portfolio.load().get("open")
        if pos:
            extra = ("\nоткрыта: " + pos["symbol"] + " " + pos["side"]
                     + " @ " + str(pos["entry"]) + " · " + str(pos.get("qty", 1)))
        await q.message.reply_text(
            "биржа " + st["exchange"] + "\nпара " + st["symbol"]
            + "\nтип " + st.get("signal_type", "signals")
            + "\nрежим " + str(st["mode"])
            + "\nпрофиль " + profile.get("label", "—") + extra,
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
        await q.message.reply_text("найдено " + str(len(pairs)) + " пар",
                                   reply_markup=kb_pairs(ex, quote, pairs, 0))
        return

    if data.startswith("page:"):
        _, ex, quote, page_str = data.split(":")
        page = int(page_str)
        st["page"] = page
        pairs = get_cached_pairs(ex, quote)
        await q.message.reply_text("страница " + str(page + 1),
                                   reply_markup=kb_pairs(ex, quote, pairs, page))
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
        await q.message.reply_text("пара " + symbol + "\nшаг 4: профиль",
                                   reply_markup=kb_profile_select(ex, symbol))
        return

    if data.startswith("profile:"):
        profile_key = data.split(":")[1]
        st["profile"] = profile_key
        profile = PROFILES[profile_key]
        await q.message.reply_text(
            "профиль: " + profile["label"] + "\n"
            + "порог " + str(int(profile["min_confidence"] * 100)) + "%\n"
            + "тейк " + str(profile["tp_mult"]) + "R\n\n"
            + "шаг 5: тип сигнала",
            reply_markup=kb_type_select(st["exchange"], st["symbol"]),
        )
        return

    if data.startswith("stype:"):
        arg = data.split(":")[1]
        if arg == "back":
            await q.message.reply_text("выбери тип:",
                                       reply_markup=kb_type_select(st["exchange"], st["symbol"]))
            return
        st["signal_type"] = arg
        label = "сигналы (BUY/SELL)" if arg == "signals" else "прогноз (вверх/вниз)"
        await q.message.reply_text(
            "тип: " + label + "\n\nшаг 6: режим",
            reply_markup=kb_mode_select(st["exchange"], st["symbol"]),
        )
        return

    if data.startswith("back:pairs:"):
        ex = data.split(":")[2]
        quote = st.get("quote", "USDT")
        pairs = get_cached_pairs(ex, quote)
        await q.message.reply_text("выбери пару",
                                   reply_markup=kb_pairs(ex, quote, pairs, st.get("page", 0)))
        return

    if data.startswith("sort:"):
        _, ex, quote, order = data.split(":")
        await q.message.reply_text("считаю цены...")
        pairs = get_cached_pairs(ex, quote)
        rows = await asyncio.to_thread(sort_pairs_by_price, ex, quote, order, pairs)
        if not rows:
            await q.message.reply_text("не удалось получить цены")
            return
        await q.message.reply_text("сортировка:",
                                   reply_markup=kb_pairs_sorted(ex, quote, rows, order))
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
        label = "сигналы" if st.get("signal_type") == "signals" else "прогноз"
        await q.message.reply_text(
            "тип " + label + "\n"
            + "режим " + mode_name + "\n"
            + "профиль " + profile.get("label", "—") + "\n"
            + "биржа " + EXCHANGES[ex] + "\n"
            + "пара " + symbol + "\n\nжми ЗАПУСТИТЬ",
            reply_markup=kb_confirm_start(ex, symbol, mode_name),
        )
        return

    if data.startswith("start_live:"):
        _, ex, symbol, mode = data.split(":")
        st["exchange"] = ex
        st["symbol"] = symbol
        st["mode"] = mode
        if mode == "scalp":
            st["interval"] = 60
        elif mode == "intraday":
            st["interval"] = 300
        else:
            st["interval"] = 900
        st["live"] = True
        if st.get("task"):
            st["task"].cancel()
        if st.get("signal_type") == "forecast":
            st["task"] = asyncio.create_task(_forecast_loop(chat_id, ctx, st))
        else:
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
        if st.get("signal_type") == "forecast":
            st["task"] = asyncio.create_task(_forecast_loop(chat_id, ctx, st))
        else:
            st["task"] = asyncio.create_task(_live_loop(chat_id, ctx, st))
        return

    if data.startswith("stop:"):
        st["live"] = False
        if st.get("task"):
            st["task"].cancel()
            st["task"] = None
        await q.message.reply_text("остановлено. что дальше?",
                                   reply_markup=kb_after_stop(st["exchange"], st["symbol"]))
        return

    if data.startswith("bought:"):
        _, symbol, action, price_str = data.split(":")
        price = float(price_str)
        st["pending_buy"] = {"symbol": symbol, "action": action, "price": price}
        await q.message.reply_text(
            "сколько купил?\nнапиши число, например 100 или 0.5\nцена входа: " + str(price))
        return

    if data.startswith("skip:"):
        st["pending_buy"] = None
        await q.message.reply_text("пропущено.")
        return

    if data.startswith("sold:"):
        _, symbol, price_str = data.split(":")
        price = float(price_str)
        trade = portfolio.close_position(price)
        if "error" in trade:
            await q.message.reply_text("нет открытой позиции")
            return
        s = portfolio.stats()
        sign = "+" if trade["pnl_pct"] > 0 else ""
        abs_sign = "+" if trade["pnl_abs"] > 0 else ""
        await q.message.reply_text(
            "сделка закрыта\n"
            + trade["side"] + " " + trade["symbol"] + "\n"
            + "вход " + str(trade["entry"]) + " · выход " + str(trade["exit"]) + "\n"
            + "кол-во " + str(trade["qty"]) + "\n"
            + "PnL: " + sign + str(trade["pnl_pct"]) + "%  (" + abs_sign + str(trade["pnl_abs"]) + ")\n"
            + "держишь " + str(trade["held_min"]) + " мин\n\n"
            + "всего сделок: " + str(s["total"]) + " · winrate " + str(round(s["winrate"], 1)) + "%"
        )
        return

    if data.startswith("hold:"):
        await q.message.reply_text("держим позицию дальше.")
        return


async def on_text(update, ctx):
    st = get_state(update.effective_chat.id)

    if st.get("pending_buy"):
        text = update.message.text.strip().replace(",", ".")
        try:
            qty = float(text)
            if qty <= 0:
                raise ValueError
        except Exception:
            await update.message.reply_text("нужно положительное число, например 100 или 0.5")
            return
        pb = st["pending_buy"]
        st["pending_buy"] = None
        portfolio.open_position(pb["symbol"], pb["action"], pb["price"], qty)
        await update.message.reply_text(
            "позиция открыта\n"
            + pb["action"] + " " + pb["symbol"] + "\n"
            + "вход " + str(pb["price"]) + " · кол-во " + str(qty) + "\n\n"
            + "при следующем сигнале увидишь PnL."
        )
        return

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
            rows.append([InlineKeyboardButton(m, callback_data="pair:" + ex + ":" + quote + ":" + m.split("/")[0])])
        await update.message.reply_text("нашёл " + str(len(matches)) + ":",
                                        reply_markup=InlineKeyboardMarkup(rows))
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

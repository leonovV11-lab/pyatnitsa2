# language: Python 3.10+, file: bot.py
import asyncio
import logging
import time
import ccxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from friday import Friday, Mode, MODE_CONFIG, fmt_price
from chart import build_chart
from exchanges import EXCHANGES, DEFAULT_QUOTES, load_all_pairs
from config import TELEGRAM_TOKEN, DEFAULT_SYMBOL, PROFILES
import portfolio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("friday")

STATE = {}
PAGE_SIZE = 20
CACHE = {}

# Монеты, среди которых бот ищет лучшую для автопилота
AUTO_PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", 
    "DOGE/USDT", "PEPE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT"
]

INTERVAL_OPTIONS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}


def get_state(chat_id):
    return STATE.setdefault(chat_id, {
        "exchange": "okx",
        "symbol": "BTC/USDT",
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
        "pending_sell": None,
        "demo_pos": None,
        "auto_best": "",
    })


def kb_start():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Начать вручную", callback_data="menu:exchanges")],
        [InlineKeyboardButton("🚀 АВТОПИЛОТ (поиск лучшей)", callback_data="auto:start")],
        [InlineKeyboardButton("📁 История сделок", callback_data="hist:menu")],
        [InlineKeyboardButton("Статистика", callback_data="menu:stats")],
        [InlineKeyboardButton("Демо-тест", callback_data="demo:start")],
    ])


def kb_exchanges_auto():
    rows = []
    row = []
    for key, label in EXCHANGES.items():
        row.append(InlineKeyboardButton(label, callback_data="autoex:" + key))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("назад", callback_data="menu:start")])
    return InlineKeyboardMarkup(rows)


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
        [InlineKeyboardButton("большая прибыль (7R)", callback_data="profile:high")],
        [InlineKeyboardButton("малая прибыль (4R)", callback_data="profile:low")],
        [InlineKeyboardButton("назад", callback_data="back:pairs:" + ex)],
    ])


def kb_type_select(ex, symbol):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("сигналы (умный анализ)", callback_data="stype:signals")],
        [InlineKeyboardButton("микро-профит (75%)", callback_data="stype:rapid")],
        [InlineKeyboardButton("прогноз (вверх / вниз)", callback_data="stype:forecast")],
        [InlineKeyboardButton("микро-шаг (свечи)", callback_data="stype:micro")],
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


def kb_signal_buttons(symbol, action, price, has_position):
    if has_position:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Закрыл", callback_data="sold:" + symbol + ":" + str(price))],
            [InlineKeyboardButton("Не закрыл", callback_data="hold:" + symbol)],
        ])
    if action == "SELL":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Открыл шорт (SELL)", callback_data="bought:" + symbol + ":" + action + ":" + str(price))],
            [InlineKeyboardButton("Пропустил", callback_data="skip:" + symbol)],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Купил (BUY)", callback_data="bought:" + symbol + ":" + action + ":" + str(price))],
        [InlineKeyboardButton("Пропустил", callback_data="skip:" + symbol)],
    ])


def kb_after_stop(ex, symbol):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1 минута", callback_data="go:" + ex + ":" + symbol + ":60")],
        [InlineKeyboardButton("5 минут", callback_data="go:" + ex + ":" + symbol + ":300")],
        [InlineKeyboardButton("15 минут", callback_data="go:" + ex + ":" + symbol + ":900")],
        [InlineKeyboardButton("1 час", callback_data="go:" + ex + ":" + symbol + ":3600")],
        [InlineKeyboardButton("4 часа", callback_data="go:" + ex + ":" + symbol + ":14400")],
        [InlineKeyboardButton("📁 История сделок", callback_data="hist:menu")],
        [InlineKeyboardButton("сменить тип", callback_data="stype:back")],
        [InlineKeyboardButton("сменить пару", callback_data="back:pairs:" + ex)],
        [InlineKeyboardButton("сменить биржу", callback_data="menu:exchanges")],
        [InlineKeyboardButton("в меню", callback_data="menu:start")],
    ])


def kb_history_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Все сделки", callback_data="hist:view:all")],
        [InlineKeyboardButton("Только лонги (BUY)", callback_data="hist:view:BUY")],
        [InlineKeyboardButton("Только шорты (SELL)", callback_data="hist:view:SELL")],
        [InlineKeyboardButton("в меню", callback_data="menu:start")],
    ])


def get_cached_pairs(ex, quote):
    key = (ex, quote)
    if key not in CACHE:
        CACHE[key] = load_all_pairs(ex, quote)
    return CACHE[key]


def find_best_pair(ex):
    """Сканирует топ-монеты и ищет ту, у которой самый большой импульс на 1m"""
    best_pair = None
    best_move = 0.0
    
    try:
        exchange = getattr(ccxt, ex)({"enableRateLimit": True})
        tickers = exchange.fetch_tickers(AUTO_PAIRS)
        
        for sym in AUTO_PAIRS:
            if sym in tickers and tickers[sym]['percentage'] is not None:
                move = abs(tickers[sym]['percentage'])
                if move > best_move:
                    best_move = move
                    best_pair = sym
    except Exception:
        pass
        
    if not best_pair:
        best_pair = "BTC/USDT"
    return best_pair, best_move


def stats_text():
    s = portfolio.stats()
    if s["total"] == 0 and not s["open"]:
        return "сделок пока нет.\nкогда бот пришлёт сигнал — жми «купил»/«продал», введи количество."
    lines = ["статистика сделок", ""]
    if s["open"]:
        o = s["open"]
        lines.append("открыта: " + o["symbol"] + " " + o["side"])
        lines.append("вход " + fmt_price(o["entry"]) + " · кол-во " + str(o["qty"]))
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


def history_text(filter_type):
    data = portfolio.load()
    history = data.get("history", [])
    
    if filter_type != "all":
        history = [t for t in history if t["side"] == filter_type]
        
    if not history:
        return "В этой категории сделок пока нет."
    
    lines = [f"<b>История ({filter_type}):</b>", ""]
    for t in history[-15:][::-1]:
        sign = "+" if t["pnl_pct"] > 0 else ""
        abs_sign = "+" if t["pnl_abs"] > 0 else ""
        icon = "🟢" if t["side"] == "BUY" else "🔴"
        
        lines.append(f"{icon} <b>{t['symbol']}</b> · {t['qty']} шт.")
        lines.append(f"Вход: {fmt_price(t['entry'])} → Выход: {fmt_price(t['exit'])}")
        lines.append(f"PnL: {sign}{t['pnl_pct']}% ({abs_sign}{t['pnl_abs']} USDT)")
        lines.append("──────────────")
        
    return "\n".join(lines)


async def _live_loop(chat_id, ctx, st):
    interval = st["interval"]
    ex = st["exchange"]
    sym = st["symbol"]
    mode = st["mode"]
    
    # Если выбран "быстрый", принудительно берем профиль rapid
    profile_key = "rapid" if st.get("signal_type") == "rapid" else st.get("profile", "high")
    profile = PROFILES[profile_key]
    
    min_conf = profile["min_confidence"]
    cooldown = profile["cooldown"]
    tp_mult = profile["tp_mult"]

    await ctx.bot.send_message(
        chat_id,
        "режим " + profile["label"] + " запущен. " + sym + " на " + EXCHANGES[ex] + "\n"
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
                    header = "СИГНАЛ (" + profile["label"] + ")"
                    pos = portfolio.load().get("open")
                    if pos:
                        cp = portfolio.current_pnl(sig.price)
                        if cp:
                            header = ("твой PnL: " + str(round(cp["pnl_pct"], 2)) + "% ("
                                      + str(round(cp["pnl_abs"], 4)) + ")\n"
                                      + "вход " + fmt_price(cp["entry"]) + " · " + cp["side"]
                                      + " · кол-во " + str(cp["qty"])
                                      + "\n\n" + header)
                    has_pos = pos is not None
                    await ctx.bot.send_message(
                        chat_id,
                        header + "\n" + sig.pretty(),
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb_signal_buttons(sym, sig.action, sig.price, has_pos),
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


async def _auto_loop(chat_id, ctx, st):
    interval = st["interval"]
    ex = st["exchange"]
    
    await ctx.bot.send_message(
        chat_id,
        "🤖 <b>АВТОПИЛОТ ЗАПУЩЕН</b>\n"
        "Биржа: " + EXCHANGES[ex] + "\n"
        "Ищу лучшую монету на сегодня...\n"
        "Проверка каждые " + str(interval) + " сек.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_running(ex, "AUTO")
    )

    # Выбираем лучшую пару
    best_sym, best_move = await asyncio.to_thread(find_best_pair, ex)
    st["symbol"] = best_sym
    st["auto_best"] = best_sym
    
    await ctx.bot.send_message(
        chat_id,
        "🎯 <b>Выбрана монета:</b> " + best_sym + "\n"
        + "Импульс за 24ч: " + str(round(best_move, 2)) + "%\n"
        + "Работаю в режиме Микро-профит (1:1, 75% уверенности).",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_running(ex, "AUTO")
    )

    profile = PROFILES["rapid"]
    tp_mult = profile["tp_mult"]
    min_conf = profile["min_confidence"]
    cooldown = 60 # Кулдаун для автопилота 1 минута

    st["last_sent"] = {}
    while st["live"]:
        try:
            friday = await asyncio.to_thread(Friday, st["symbol"], ex)
            sig = await asyncio.to_thread(friday.analyze_filtered, Mode("scalp"), tp_mult)
            
            if sig.action in ("BUY", "SELL") and sig.confidence >= min_conf:
                now = time.time()
                last_time = st["last_sent"].get(sig.action, 0)
                if now - last_time >= cooldown:
                    st["last_sent"][sig.action] = now
                    header = "🤖 АВТО-СИГНАЛ (" + st["symbol"] + ")"
                    pos = portfolio.load().get("open")
                    if pos:
                        cp = portfolio.current_pnl(sig.price)
                        if cp:
                            header = ("твой PnL: " + str(round(cp["pnl_pct"], 2)) + "% ("
                                      + str(round(cp["pnl_abs"], 4)) + ")\n"
                                      + "вход " + fmt_price(cp["entry"]) + " · " + cp["side"]
                                      + " · кол-во " + str(cp["qty"])
                                      + "\n\n" + header)
                    has_pos = pos is not None
                    await ctx.bot.send_message(
                        chat_id,
                        header + "\n" + sig.pretty(),
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb_signal_buttons(st["symbol"], sig.action, sig.price, has_pos),
                    )
                    try:
                        path = await asyncio.to_thread(build_chart, st["symbol"], Mode("scalp"), sig, "chart_auto.png")
                        with open(path, "rb") as f:
                            await ctx.bot.send_photo(chat_id, photo=f,
                                                     caption=sig.action + " scalp " + st["symbol"],
                                                     reply_markup=kb_running(ex, "AUTO"))
                    except Exception as e:
                        log.warning("chart error: " + str(e))
        except Exception as e:
            log.warning("auto error: " + str(e))
            
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
                    msg = arrow + " " + label + "\nцена: " + fmt_price(price)
                else:
                    sign = "+" if direction == "up" else "-"
                    msg = (arrow + " " + label + "\n"
                           + "потенциал: " + sign + str(round(pot_pct, 2)) + "%\n"
                           + "уверенность: " + str(int(sig.confidence * 100)) + "%\n"
                           + "цена: " + fmt_price(price))
                await ctx.bot.send_message(chat_id, msg, reply_markup=kb_running(ex, sym))
        except Exception as e:
            log.warning("forecast error: " + str(e))
        for _ in range(interval):
            if not st["live"]:
                return
            await asyncio.sleep(1)


async def _micro_loop(chat_id, ctx, st):
    ex = st["exchange"]
    sym = st["symbol"]
    mode = st["mode"]
    interval = st["interval"]
    tf = MODE_CONFIG[Mode(mode)]["tf"]

    await ctx.bot.send_message(
        chat_id,
        "МИКРО-ШАГ запущен. " + sym + " на " + EXCHANGES[ex] + "\n"
        + "таймфрейм: " + tf + " · проверка каждые " + str(interval) + " сек.\n"
        + "жми кнопки для входа/выхода.",
        reply_markup=kb_running(ex, sym),
    )

    while st["live"]:
        try:
            friday = await asyncio.to_thread(Friday, sym, ex)
            sig = await asyncio.to_thread(friday.micro_predict, Mode(mode))
            
            if sig.action != "HOLD":
                arrow = "↑ ВВЕРХ" if sig.action == "BUY" else "↓ ВНИЗ"
                msg = ("ПРОГНОЗ НА СЛЕДУЮЩУЮ СВЕЧУ (" + tf + ")\n"
                       + arrow + " (" + sig.action + ")\n"
                       + "цена: " + fmt_price(sig.price) + "\n\n"
                       + "жми кнопку чтобы зафиксировать сделку.")
                
                pos = portfolio.load().get("open")
                has_pos = pos is not None
                await ctx.bot.send_message(chat_id, msg, reply_markup=kb_signal_buttons(sym, sig.action, sig.price, has_pos))
        except Exception as e:
            log.warning("micro error: " + str(e))
            
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
    st["pending_buy"] = None
    st["pending_sell"] = None
    if st.get("task"):
        st["task"].cancel()
        st["task"] = None
    await update.message.reply_text("остановлено. состояние сброшено. что дальше?",
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

    # АВТОПИЛОТ
    if data == "auto:start":
        await q.message.reply_text("🤖 <b>АВТОПИЛОТ</b>\nВыбери биржу для автопоиска лучшей монеты:", parse_mode=ParseMode.HTML, reply_markup=kb_exchanges_auto())
        return
        
    if data.startswith("autoex:"):
        ex = data.split(":")[1]
        st["exchange"] = ex
        st["signal_type"] = "rapid"
        st["live"] = True
        st["interval"] = 60
        
        if st.get("task"):
            st["task"].cancel()
        st["task"] = asyncio.create_task(_auto_loop(chat_id, ctx, st))
        return

    if data == "menu:stats":
        await q.message.reply_text(stats_text(), reply_markup=kb_start())
        return

    if data == "hist:menu":
        await q.message.reply_text("📁 <b>Папка с историей.</b>\nВыбери категорию:", 
                                   parse_mode=ParseMode.HTML, reply_markup=kb_history_menu())
        return

    if data.startswith("hist:view:"):
        filter_type = data.split(":")[2]
        text = history_text(filter_type)
        await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_history_menu())
        return

    if data == "demo:start":
        st["demo_pos"] = None
        text = (
            "СИГНАЛ (ДЕМО)\n"
            "BUY · SCALP\n"
            "пара: DEMO/USDT\n"
            "цена: 100.00\n"
            "уверенность: 85%\n"
            "стоп: 95.00\n"
            "тейк: 115.00\n\n"
            "причины:\n"
            "  • демо тренд\n"
            "  • демо MACD"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("купил", callback_data="demo:bought")],
            [InlineKeyboardButton("пропустил", callback_data="demo:skip")]
        ])
        await q.message.reply_text(text, reply_markup=markup)
        return

    if data == "demo:bought":
        st["pending_buy"] = {"symbol": "DEMO/USDT", "action": "BUY", "price": 100.0, "is_demo": True}
        await q.message.reply_text("сколько купил?\nнапиши число, например 5")
        return

    if data == "demo:skip":
        await q.message.reply_text("демо пропущено.", reply_markup=kb_start())
        return

    if data == "demo:sold":
        pos = st.get("demo_pos")
        if not pos:
            await q.message.reply_text("нет демо-позиции", reply_markup=kb_start())
            return
        st["pending_sell"] = {"price": 110.0, "is_demo": True, "available": pos["qty"]}
        await q.message.reply_text("сколько продал?\nв позиции: " + str(pos["qty"]))
        return

    if data == "menu:current":
        profile = PROFILES.get(st.get("profile", "high"), {})
        extra = ""
        pos = portfolio.load().get("open")
        if pos:
            extra = ("\nоткрыта: " + pos["symbol"] + " " + pos["side"]
                     + " @ " + fmt_price(pos["entry"]) + " · " + str(pos.get("qty", 1)))
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
        
        if arg == "signals": label = "сигналы (умный анализ)"
        elif arg == "rapid": label = "быстрый (микро-профит)"
        elif arg == "forecast": label = "прогноз (
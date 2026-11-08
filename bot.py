# language: Python 3.10+, file: bot.py
import asyncio
import logging
import time
import ccxt
import datetime
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
        "pending_hours": False,
        "auto_hours": 12,
        "auto_end_time": 0,
        "change_suggested": False,
        "new_sym": None,
        "new_mode": None,
        "new_profile": None,
    })


def kb_start():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Начать вручную", callback_data="menu:exchanges")],
        [InlineKeyboardButton("АВТОПИЛОТ", callback_data="auto:start")],
        [InlineKeyboardButton("📁 История сделок", callback_data="hist:menu")],
        [InlineKeyboardButton("Статистика", callback_data="menu:stats")],
        [InlineKeyboardButton("Демо-тест", callback_data="demo:start")],
    ])


def kb_auto_time():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Весь день (12ч+)", callback_data="autotime:12")],
        [InlineKeyboardButton("Указать свое время (часы)", callback_data="autotime:custom")],
        [InlineKeyboardButton("назад", callback_data="menu:start")],
    ])


def kb_auto_continue():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Да, продолжить", callback_data="autocont:yes")],
        [InlineKeyboardButton("Нет, в меню", callback_data="autocont:no")],
    ])

def kb_change_symbol():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Да, меняем", callback_data="autochange:yes")],
        [InlineKeyboardButton("Нет, оставляем эту", callback_data="autochange:no")],
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


def find_best_asset(ex, hours):
    try:
        exchange = getattr(ccxt, ex)({"enableRateLimit": True})
        markets = exchange.load_markets()
        usdt_pairs = [s for s in markets if s.endswith("/USDT") and markets[s]["active"] and markets[s]["spot"]]
        usdt_pairs = usdt_pairs[:300] 
        
        tickers = exchange.fetch_tickers(usdt_pairs)
        
        best_sym = "BTC/USDT"
        best_mode = "scalp"
        best_volatility = 0.0
        best_profile = "rapid"

        for sym, data in tickers.items():
            if not data['percentage'] or not data['quoteVolume']: continue
            if data['quoteVolume'] < 100000: continue
            
            volatility = abs(data['percentage'])
            
            if volatility > best_volatility:
                best_volatility = volatility
                best_sym = sym
                
                if hours <= 4:
                    best_mode = "scalp"
                    best_profile = "rapid" if volatility > 8.0 else "low"
                elif hours <= 12:
                    best_mode = "intraday"
                    best_profile = "low" if volatility > 6.0 else "high"
                else:
                    best_mode = "swing"
                    best_profile = "high"

        return best_sym, best_mode, best_profile, best_volatility
    except Exception as e:
        log.warning("scan error: " + str(e))
        return "BTC/USDT", "scalp", "rapid", 0.0


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


async def _auto_loop(chat_id, ctx, st, hours):
    interval = 60 # Проверка смены монеты раз в минуту
    ex = st["exchange"]
    
    await ctx.bot.send_message(
        chat_id,
        "🤖 <b>АВТОПИЛОТ ЗАПУЩЕН</b>\n"
        "Биржа: " + EXCHANGES[ex] + "\n"
        "Время работы: " + str(hours) + " ч.\n"
        "Сканирую ВЕСЬ рынок...",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_running(ex, "AUTO")
    )

    best_sym, best_mode, best_profile, best_vol = await asyncio.to_thread(find_best_asset, ex, hours)
    st["symbol"] = best_sym
    st["mode"] = best_mode
    st["profile"] = best_profile
    
    profile = PROFILES[best_profile]
    tf_label = MODE_CONFIG[Mode(best_mode)]["tf"]
    
    await ctx.bot.send_message(
        chat_id,
        "🎯 <b>Лучший сетап найден:</b>\n"
        + "Монета: <b>" + best_sym + "</b>\n"
        + "Волатильность 24ч: <b>" + str(round(best_vol, 2)) + "%</b>\n"
        + "Режим: <b>" + best_mode + " (" + tf_label + ")</b>\n"
        + "Профиль: <b>" + profile["label"] + "</b>\n\n"
        + "Следим и ждем идеального входа.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_running(ex, "AUTO")
    )

    tp_mult = profile["tp_mult"]
    min_conf = profile["min_confidence"]
    
    st["last_sent"] = {}
    last_scan_time = time.time()
    st["change_suggested"] = False

    while st["live"]:
        try:
            now = time.time()
            
            # Если мы ждем ответа от пользователя, не спамим сигналами
            if st["change_suggested"]:
                await asyncio.sleep(5)
                continue

            # Проверяем, не вышло ли время
            if now >= st["auto_end_time"]:
                st["live"] = False
                await ctx.bot.send_message(
                    chat_id,
                    "⏰ <b>Время вышло!</b>\nТвоя сессия автопилота (" + str(hours) + " ч.) завершена.\nПродолжаем работу?",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb_auto_continue()
                )
                return

            # Раз в минуту проверяем смену лидера
            if now - last_scan_time >= 60:
                last_scan_time = now
                new_sym, new_mode, new_profile, new_vol = await asyncio.to_thread(find_best_asset, ex, hours)
                
                if new_sym != st["symbol"]:
                    # Нашли лидера, спрашиваем
                    st["change_suggested"] = True
                    st["new_sym"] = new_sym
                    st["new_mode"] = new_mode
                    st["new_profile"] = new_profile
                    st["new_vol"] = new_vol
                    
                    await ctx.bot.send_message(
                        chat_id,
                        "🔄 <b>Смена лидера!</b>\n"
                        + "Было: " + st["symbol"] + "\n"
                        + "Стало: " + new_sym + " (волатильность " + str(round(new_vol, 2)) + "%)\n"
                        + "Режим: " + new_mode + "\n"
                        + "Меняем?",
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb_change_symbol()
                    )
                    continue # Ждем ответа, цикл пропускаем

            friday = await asyncio.to_thread(Friday, st["symbol"], ex)
            sig = await asyncio.to_thread(friday.analyze_filtered, Mode(st["mode"]), tp_mult)
            
            if sig.action in ("BUY", "SELL") and sig.confidence >= min_conf:
                now = time.time()
                last_time = st["last_sent"].get(sig.action, 0)
                if now - last_time >= interval:
                    st["last_sent"][sig.action] = now
                    header = "🤖 АВТО-СИГНАЛ (" + st["symbol"] + " · " + tf_label + ")"
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
                        path = await asyncio.to_thread(build_chart, st["symbol"], Mode(st["mode"]), sig, "chart_auto.png")
                        with open(path, "rb") as f:
                            await ctx.bot.send_photo(chat_id, photo=f,
                                                     caption=sig.action + " " + st["mode"] + " " + st["symbol"],
                                                     reply_markup=kb_running(ex, "AUTO"))
                    except Exception as e:
                        log.warning("chart error: " + str(e))
        except Exception as e:
            log.warning("auto error: " + str(e))
            
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

    if data == "auto:start":
        await q.message.reply_text("🤖 <b>АВТОПИЛОТ</b>\nСначала выбери время работы:", parse_mode=ParseMode.HTML, reply_markup=kb_auto_time())
        return

    if data.startswith("autotime:"):
        arg = data.split(":")[1]
        if arg == "custom":
            st["pending_hours"] = True
            await q.message.reply_text("Сколько часов планируешь работать?\nНапиши число, например: 3 или 0.5")
            return
        else:
            hours = float(arg)
            st["pending_hours"] = False
            st["auto_hours"] = hours
            await q.message.reply_text("Принято. Теперь выбери биржу для сканирования:", reply_markup=kb_exchanges_auto())
        return
        
    if data.startswith("autoex:"):
        ex = data.split(":")[1]
        st["exchange"] = ex
        st["signal_type"] = "rapid"
        st["live"] = True
        st["interval"] = 60
        
        hours = st.get("auto_hours", 12)
        st["auto_end_time"] = time.time() + (hours * 3600)
        
        if st.get("task"):
            st["task"].cancel()
        st["task"] = asyncio.create_task(_auto_loop(chat_id, ctx, st, hours))
        return

    if data.startswith("autochange:"):
        arg = data.split(":")[1]
        st["change_suggested"] = False
        if arg == "yes":
            st["symbol"] = st["new_sym"]
            st["mode"] = st["new_mode"]
            st["profile"] = st["new_profile"]
            await q.message.reply_text(
                "✅ <b>Отлично, переключился!</b>\n"
                + "Теперь работаем с: " + st["symbol"] + "\n"
                + "Режим: " + st["mode"] + "\n\nПродолжаем поиск сигналов.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_running(st["exchange"], "AUTO")
            )
        else:
            await q.message.reply_text(
                "✅ <b>Ок, продолжаем смотреть за старой.</b>\n"
                + "Монета: " + st["symbol"] + "\n"
                + "Продолжаю искать лучшие варианты...",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_running(st["exchange"], "AUTO")
            )
        return

    if data.startswith("autocont:"):
        arg = data.split(":")[1]
        if arg == "no":
            await q.message.reply_text("Ок, возвращаемся в меню.", reply_markup=kb_start())
            return
        else:
            await q.message.reply_text("Сколько еще часов будем торговать?\nНапиши число, например: 1 или 0.5")
            st["pending_hours"] = True
            st["is_continuing"] = True
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
        elif arg == "forecast": label = "прогноз (вверх/вниз)"
        else: label = "микро-шаг (свечи)"
        
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
        
        if st.get("signal_type") == "signals": label = "сигналы"
        elif st.get("signal_type") == "rapid": label = "микро-профит"
        elif st.get("signal_type") == "forecast": label = "прогноз"
        else: label = "микро-шаг"
        
        await q.message.reply_text(
            "тип " + label + "\n"
            + "режим " + mode_name + "\n"
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
        elif st.get("signal_type") == "micro":
            st["task"] = asyncio.create_task(_micro_loop(chat_id, ctx, st))
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
        elif st.get("signal_type") == "micro":
            st["task"] = asyncio.create_task(_micro_loop(chat_id, ctx, st))
        else:
            st["task"] = asyncio.create_task(_live_loop(chat_id, ctx, st))
        return

    if data.startswith("stop:"):
        st["live"] = False
        st["pending_buy"] = None
        st["pending_sell"] = None
        if st.get("task"):
            st["task"].cancel()
            st["task"] = None
        await q.message.reply_text("остановлено. состояние сброшено. что дальше?",
                                   reply_markup=kb_after_stop(st["exchange"], st["symbol"]))
        return

    if data.startswith("bought:"):
        _, symbol, action, price_str = data.split(":")
        price = float(price_str)
        st["pending_buy"] = {"symbol": symbol, "action": action, "price": price, "is_demo": False}
        word = "купил" if action == "BUY" else "открыл шорт"
        await q.message.reply_text(
            word + "\nсколько?\nнапиши число, например 100 или 0.5\nцена входа: " + fmt_price(price))
        return

    if data.startswith("skip:"):
        st["pending_buy"] = None
        st["pending_sell"] = None
        await q.message.reply_text("пропущено. ждём следующий сигнал.")
        return

    if data.startswith("sold:"):
        _, symbol, price_str = data.split(":")
        price = float(price_str)
        pos = portfolio.load().get("open")
        if not pos:
            await q.message.reply_text("нет открытой позиции")
            return
        st["pending_sell"] = {"symbol": symbol, "price": price, "is_demo": False, "available": pos["qty"]}
        await q.message.reply_text("сколько продал?\nв позиции: " + str(pos["qty"]))
        return

    if data.startswith("hold:"):
        await q.message.reply_text("держим позицию дальше.")
        return


async def on_text(update, ctx):
    st = get_state(update.effective_chat.id)

    if st.get("pending_hours"):
        text = update.message.text.strip().replace(",", ".")
        try:
            hours = float(text)
            if hours <= 0:
                raise ValueError
        except Exception:
            await update.message.reply_text("нужно положительное число, например 3 или 0.5")
            return
        st["pending_hours"] = False
        st["auto_hours"] = hours
        st["auto_end_time"] = time.time() + (hours * 3600)
        st["live"] = True
        
        if st.get("is_continuing"):
            st["is_continuing"] = False
            if st.get("task"):
                st["task"].cancel()
            st["task"] = asyncio.create_task(_auto_loop(update.effective_chat.id, ctx, st, hours))
            return
            
        await update.message.reply_text(
            "Принято: " + str(hours) + " ч.\nТеперь выбери биржу для сканирования:",
            reply_markup=kb_exchanges_auto()
        )
        return

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

        if pb.get("is_demo"):
            st["demo_pos"] = {"entry": pb["price"], "qty": qty, "side": pb["action"]}
            msg = (
                "СИГНАЛ (ДЕМО)\n"
                "SELL · SCALP\n"
                "пара: DEMO/USDT\n"
                "цена: 110.00\n"
                "уверенность: 80%\n"
                "стоп: 115.00\n"
                "тейк: 95.00\n\n"
                "причины:\n"
                "  • демо тренд вниз\n"
                "  • демо MACD"
            )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("продал", callback_data="demo:sold")],
                [InlineKeyboardButton("пропустил", callback_data="demo:skip")]
            ])
            await update.message.reply_text(msg, reply_markup=markup)
            return

        portfolio.open_position(pb["symbol"], pb["action"], pb["price"], qty)
        side_word = "лонг" if pb["action"] == "BUY" else "шорт"
        await update.message.reply_text(
            "позиция открыта\n"
            + side_word + " " + pb["symbol"] + "\n"
            + "вход " + fmt_price(pb["price"]) + " · кол-во " + str(qty) + "\n\n"
            + "при следующем сигнале увидишь PnL."
        )
        return

    if st.get("pending_sell"):
        text = update.message.text.strip().replace(",", ".")
        try:
            qty = float(text)
            if qty <= 0:
                raise ValueError
        except Exception:
            await update.message.reply_text("нужно положительное число, например 3 или 0.5")
            return
        ps = st["pending_sell"]
        st["pending_sell"] = None

        if ps.get("is_demo"):
            pos = st.get("demo_pos")
            if not pos:
                await update.message.reply_text("нет демо позиции")
                return
            if qty > pos["qty"] + 1e-8:
                await update.message.reply_text("у тебя только " + str(pos["qty"]))
                return
            pnl = (ps["price"] - pos["entry"]) * qty
            pnl_pct = (ps["price"] - pos["entry"]) / pos["entry"] * 100
            pos["qty"] -= qty
            rem = pos["qty"] if pos["qty"] > 1e-8 else 0
            st["demo_pos"] = pos if rem > 0 else None
            
            rem_str = "\nосталось в позиции: " + str(rem) if rem > 0 else "\nпозиция полностью закрыта"
            await update.message.reply_text(
                "Сделка закрыта (ДЕМО) и занесена в историю\n"
                "вход " + fmt_price(pos['entry']) + " · выход " + fmt_price(ps['price']) + "\n"
                "кол-во " + str(qty) + "\n"
                "PnL: " + str(round(pnl_pct,2)) + "% (" + str(round(pnl,2)) + " USDT)" + rem_str,
                reply_markup=kb_running(st["exchange"], st["symbol"])
            )
            return

        trade = portfolio.close_position(ps["price"], qty)
        if "error" in trade:
            await update.message.reply_text(trade["error"])
            return
        s = portfolio.stats()
        sign = "+" if trade["pnl_pct"] > 0 else ""
        abs_sign = "+" if trade["pnl_abs"] > 0 else ""
        
        rem = trade["remaining"]
        rem_str = "\nосталось в позиции: " + str(rem) if rem > 0 else "\nпозиция полностью закрыта"

        await update.message.reply_text(
            "сделка закрыта и занесена в историю\n"
            + trade["side"] + " " + trade["symbol"] + "\n"
            + "вход " + fmt_price(trade["entry"]) + " · выход " + fmt_price(trade["exit"]) + "\n"
            + "продано " + str(trade["qty"]) + "\n"
            + "PnL: " + sign + str(trade["pnl_pct"]) + "%  (" + abs_sign + str(trade["pnl_abs"]) + ")" + rem_str + "\n\n"
            + "всего сделок: " + str(s["total"]) + " · winrate " + str(round(s["winrate"], 1)) + "%",
            reply_markup=kb_running(st["exchange"], st["symbol"])
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
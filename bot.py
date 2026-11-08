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
    })


def kb_start():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Начать вручную", callback_data="menu:exchanges")],
        [InlineKeyboardButton("🚀 УМНЫЙ АВТОПИЛОТ", callback_data="auto:start")],
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


def find_best_asset(ex):
    """Сканирует ВЕСЬ рынок и ищет лучшую монету + режим"""
    try:
        exchange = getattr(ccxt, ex)({"enableRateLimit": True})
        markets = exchange.load_markets()
        
        # Берем абсолютно все пары в USDT
        usdt_pairs = [s for s in markets if s.endswith("/USDT") and markets[s]["active"] and markets[s]["spot"]]
        
        # Ограничим до 300, чтобы не привысить лимиты биржи, но это уже весь цвет рынка
        usdt_pairs = usdt_pairs[:300] 
        
        tickers = exchange.fetch_tickers(usdt_pairs)
        
        best_sym = "BTC/USDT"
        best_mode = "scalp"
        best_volatility = 0.0
        best_profile = "rapid"

        for sym, data in tickers.items():
            if not data['percentage'] or not data['quoteVolume']: continue
            if data['quoteVolume'] < 100000: continue # Отсекаем мертвые монеты с низким объемом
            
            # Считаем волатильность (абсолютное изменение за 24ч)
            volatility = abs(data['percentage'])
            
            if volatility > best_volatility:
                best_volatility = volatility
                best_sym = sym
                
                # Адаптивный выбор профиля и таймфрейма
                if volatility > 8.0:
                    # Если монета летает как безумная, берем минутку и микро-профит 1:1, чтобы быстро забрать
                    best_mode = "scalp"
                    best_profile = "rapid"
                elif volatility > 4.0:
                    # Средняя волатильность, 15 минут, забираем 4R
                    best_mode = "intraday"
                    best_profile = "low"
                else:
                    # Слабая волатильность, 4 часа, забираем 7R
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


async def _auto_loop(chat_id, ctx, st):
    interval = st["interval"]
    ex = st["exchange"]
    
    await ctx.bot.send_message(
        chat_id,
        "🤖 <b>УМНЫЙ АВТОПИЛОТ (ВЕСЬ РЫНОК)</b>\n"
        "Биржа: " + EXCHANGES[ex] + "\n"
        "Сканирую все монеты, ищу максимальную волатильность...",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_running(ex, "AUTO")
    )

    best_sym, best_mode, best_profile, best_vol = await asyncio.to_thread(find_best_asset, ex)
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
    cooldown = interval

    st["last_sent"] = {}
    while st["live"]:
        try:
            friday = await asyncio.to_thread(Friday, st["symbol"], ex)
            sig = await asyncio.to_thread(friday.analyze_filtered, Mode(st["mode"]), tp_mult)
            
            if sig.action in ("BUY", "SELL") and sig.confidence >= min_conf:
                now = time.time()
                last_time = st["last_sent"].get(sig.action, 0)
                if now - last_time >= cooldown:
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


# ... остальные функции (on_button, on_text, main) остаются без изменений ...
# Полный код bot.py нужен для замены файла, поэтому я объединю всё в один блок ниже.
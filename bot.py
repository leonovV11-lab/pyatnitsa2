# language: Python 3.10+, file: bot.py
import asyncio
import logging
import time
import ccxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from friday import Friday, Mode, fmt_price
from chart import build_chart
from exchanges import EXCHANGES, DEFAULT_QUOTES, load_all_pairs
from config import TELEGRAM_TOKEN, PROFILES, RISK_PERCENT
import portfolio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("friday")

STATE = {}
PAGE_SIZE = 50
CACHE = {}

def get_state(chat_id):
    return STATE.setdefault(chat_id, {
        "exchange": "okx",
        "symbol": "BTC/USDT",
        "quote": "USDT",
        "mode": "scalp",
        "custom_tf": None,
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
        "pending_days": False,
        "auto_hours": 12,
        "auto_end_time": 0,
        "change_suggested": False,
        "new_sym": None,
        "new_mode": None,
        "new_profile": None,
        "has_coins": False,
        "awaiting_has_coins": False,
        "awaiting_coin_qty": False,
        "awaiting_tf": False,
        "awaiting_custom_tf": False,
        "current_vol": 0.0,
        "breakeven_set": False,
        "spam_guard": False,
        "losses_in_row": 0,
        "pause_until": 0,
        "balance": 0.0,
        "daily_budget": 0.0,
        "budget_spent": 0.0,
        "daily_profit": 0.0,
        "awaiting_balance": False,
        "awaiting_budget": False,
        "auto_pending": False,
    })

def kb_start():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Начать вручную", callback_data="menu:exchanges")],
        [InlineKeyboardButton("🚀 АВТОПИЛОТ", callback_data="auto:start")],
        [InlineKeyboardButton("📁 История", callback_data="hist:menu")],
        [InlineKeyboardButton("Статистика", callback_data="menu:stats")],
        [InlineKeyboardButton("🎮 Демо-тест кнопок", callback_data="demo:start")],
        [InlineKeyboardButton("🔴 Закрыть сделку", callback_data="close_pos")],
    ])

def kb_auto_time():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Весь день (12ч+)", callback_data="autotime:12")],
        [InlineKeyboardButton("Свое время (часы)", callback_data="autotime:custom_hours")],
        [InlineKeyboardButton("Несколько дней", callback_data="autotime:custom_days")],
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

def kb_has_coins():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Да, есть", callback_data="hascoins:yes")],
        [InlineKeyboardButton("Нет, нету", callback_data="hascoins:no")],
    ])

def kb_tf_select():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("скальп 1m", callback_data="autotf:1m")],
        [InlineKeyboardButton("интрадей 15m", callback_data="autotf:15m")],
        [InlineKeyboardButton("свинг 4h", callback_data="autotf:4h")],
        [InlineKeyboardButton("свое время (минуты)", callback_data="autotf:custom")],
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
    row = []
    for sym in chunk:
        base = sym.split("/")[0]
        row.append(InlineKeyboardButton(base, callback_data="pair:" + ex + ":" + quote + ":" + base))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
        
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("<<", callback_data="page:" + ex + ":" + quote + ":" + str(page - 1)))
    nav.append(InlineKeyboardButton(str(page + 1) + "/" + str(total), callback_data="noop"))
    if page < total - 1:
        nav.append(InlineKeyboardButton(">>", callback_data="page:" + ex + ":" + quote + ":" + str(page + 1)))
    if len(nav) > 1:
        rows.append(nav)
        
    rows.append([InlineKeyboardButton("дешевые", callback_data="sort:" + ex + ":" + quote + ":asc")])
    rows.append([InlineKeyboardButton("дорогие", callback_data="sort:" + ex + ":" + quote + ":desc")])
    rows.append([InlineKeyboardButton("🔍 Поиск монеты", callback_data="search:" + ex + ":" + quote)])
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
        [InlineKeyboardButton("🔴 Закрыть позицию", callback_data="close_pos")],
    ])

def kb_signal_buttons(symbol, action, price, has_position):
    if has_position:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Закрыл 50%", callback_data="sold:" + symbol + ":" + str(price))],
            [InlineKeyboardButton("Закрыл всё", callback_data="sold_all:" + symbol + ":" + str(price))],
            [InlineKeyboardButton("Держать", callback_data="hold:" + symbol)],
        ])
    if action == "SELL":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Открыл шорт", callback_data="bought:" + symbol + ":" + action + ":" + str(price))],
            [InlineKeyboardButton("Пропустил", callback_data="skip:" + symbol)],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Купил", callback_data="bought:" + symbol + ":" + action + ":" + str(price))],
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
        usdt_pairs = usdt_pairs[:150]
        tickers = exchange.fetch_tickers(usdt_pairs)
        
        best_sym = "BTC/USDT"
        best_mode = "scalp"
        best_potential = 0.0
        best_profile = "rapid"

        for sym, data in tickers.items():
            if not data['percentage'] or not data['quoteVolume']:
                continue
            if data['quoteVolume'] < 1000000:
                continue
            
            volatility = abs(data['percentage'])
            potential = (data['quoteVolume'] / 1000000) * volatility
            
            if potential > best_potential:
                best_potential = potential
                best_sym = sym
                
                if hours <= 3:
                    best_mode = "scalp" if volatility > 5.0 else "intraday"
                    best_profile = "rapid"
                elif hours <= 12:
                    best_mode = "intraday"
                    best_profile = "low" if volatility > 6.0 else "high"
                else:
                    best_mode = "swing"
                    best_profile = "high"

        return best_sym, best_mode, best_profile, best_potential
    except Exception as e:
        log.warning("scan error: " + str(e))
        return "BTC/USDT", "scalp", "rapid", 0.0

def stats_text():
    s = portfolio.stats()
    if s["total"] == 0 and not s["open"]:
        return "сделок пока нет."
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
    tf = st["mode"]
    profile = PROFILES[st.get("profile", "high")]
    tp_mult = profile["tp_mult"]

    await ctx.bot.send_message(chat_id, "режим " + profile["label"] + " запущен. " + sym + " (" + tf + ")", reply_markup=kb_running(ex, sym))
    st["last_sent"] = {}
    st["spam_guard"] = False
    
    while st["live"]:
        try:
            if time.time() < st["pause_until"]:
                await asyncio.sleep(10)
                continue
                
            if st["budget_spent"] >= st["daily_budget"] and st["daily_budget"] > 0:
                await ctx.bot.send_message(chat_id, "🛑 <b>Дневной лимит исчерпан!</b>", parse_mode=ParseMode.HTML)
                st["live"] = False
                return

            friday = await asyncio.to_thread(Friday, sym, ex)
            sig = await asyncio.to_thread(friday.analyze_filtered, tf, tp_mult)
            
            if not st["has_coins"] and sig.action == "SELL":
                sig.action = "HOLD"
                sig.reasons.append("[фильтр] нет монет — SELL отменён")

            pos = portfolio.load().get("open")
            if pos and not st["breakeven_set"]:
                entry = pos["entry"]
                side = pos["side"]
                cp = sig.price
                
                if side == "BUY" and (cp - entry) / entry >= 0.02:
                    st["breakeven_set"] = True
                    await ctx.bot.send_message(chat_id, "🛡 <b>Защита в безубыток!</b>", parse_mode=ParseMode.HTML)
                elif side == "SELL" and (entry - cp) / entry >= 0.02:
                    st["breakeven_set"] = True
                    await ctx.bot.send_message(chat_id, "🛡 <b>Защита в безубыток!</b>", parse_mode=ParseMode.HTML)

            if pos and sig.action != "HOLD" and not st["spam_guard"]:
                if pos["side"] == "BUY" and sig.action == "SELL":
                    await ctx.bot.send_message(chat_id, "🚨 <b>СРОЧНО ЗАКРЫВАЙ!</b>\nТренд развернулся вниз.", parse_mode=ParseMode.HTML)
                    st["spam_guard"] = True
                elif pos["side"] == "SELL" and sig.action == "BUY":
                    await ctx.bot.send_message(chat_id, "🚨 <b>СРОЧНО ЗАКРЫВАЙ!</b>\nТренд развернулся вверх.", parse_mode=ParseMode.HTML)
                    st["spam_guard"] = True

            if sig.action in ("BUY", "SELL") and sig.confidence >= profile["min_confidence"]:
                now = time.time()
                if now - st["last_sent"].get(sig.action, 0) >= profile["cooldown"]:
                    st["last_sent"][sig.action] = now
                    
                    risk_pct = RISK_PERCENT[0.85] if sig.confidence >= 0.85 else (RISK_PERCENT[0.75] if sig.confidence >= 0.75 else RISK_PERCENT[0.00])
                    risk_usdt = st["balance"] * risk_pct if st["balance"] > 0 else 1.0
                    
                    header = "СИГНАЛ (" + profile["label"] + ")"
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
                        header + "\n" + sig.pretty() + "\n💡 Риск: " + str(round(risk_usdt, 2)) + " USDT",
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb_signal_buttons(sym, sig.action, sig.price, has_pos),
                    )
                    try:
                        path = await asyncio.to_thread(build_chart, sym, tf, sig, "chart_" + str(tf) + ".png")
                        with open(path, "rb") as f:
                            await ctx.bot.send_photo(chat_id, photo=f,
                                                     caption=sig.action + " " + str(tf) + " " + sym,
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
    ex = st["exchange"]
    await ctx.bot.send_message(chat_id, "🤖 <b>АВТОПИЛОТ ЗАПУЩЕН</b>\nАнализирую рынок...", parse_mode=ParseMode.HTML, reply_markup=kb_running(ex, "AUTO"))
    
    best_sym, best_mode, best_profile, best_pot = await asyncio.to_thread(find_best_asset, ex, hours)
    st["symbol"] = best_sym
    st["mode"] = best_mode
    st["profile"] = best_profile
    st["current_vol"] = best_pot
    st["custom_tf"] = None
    
    profile = PROFILES[best_profile]
    tf_label = best_mode
    
    risk_modifier = 1.0
    if st["current_vol"] > 100:
        risk_modifier = 0.5
        await ctx.bot.send_message(chat_id, "⚠️ <b>Высокая волатильность на рынке!</b>\nРиск урезан в 2 раза, чтобы не слить депо.")
    elif st["current_vol"] < 5:
        st["live"] = False
        await ctx.bot.send_message(chat_id, "🛑 <b>Рынок стоит на месте (флэт).</b>\nТорговать сегодня не выгодно. Отдыхаем.")
        return

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Устраивает", callback_data="tf_ok:yes")],
        [InlineKeyboardButton("Сменить время", callback_data="tf_ok:no")],
    ])
    
    await ctx.bot.send_message(chat_id, "🎯 <b>Лучший сетап:</b>\nМонета: <b>" + best_sym + "</b>\nРежим: <b>" + best_mode + "</b>\nУстраивает время?", parse_mode=ParseMode.HTML, reply_markup=markup)
    st["awaiting_tf"] = True

    tp_mult = profile["tp_mult"]
    st["last_sent"] = {}
    last_scan_time = time.time()
    st["change_suggested"] = False
    st["spam_guard"] = False

    while st["live"]:
        try:
            if st["awaiting_tf"] or st["awaiting_has_coins"] or st["awaiting_coin_qty"] or st["awaiting_custom_tf"] or time.time() < st["pause_until"]:
                await asyncio.sleep(2)
                continue

            if st["change_suggested"]:
                await asyncio.sleep(2)
                continue
                
            if time.time() >= st["auto_end_time"]:
                st["live"] = False
                await ctx.bot.send_message(chat_id, "⏰ <b>Время вышло!</b>\nПродолжаем?", parse_mode=ParseMode.HTML, reply_markup=kb_auto_continue())
                return
                
            if st["budget_spent"] >= st["daily_budget"] and st["daily_budget"] > 0:
                await ctx.bot.send_message(chat_id, "🛑 <b>Дневной лимит исчерпан!</b>", parse_mode=ParseMode.HTML)
                st["live"] = False
                return

            if time.time() - last_scan_time >= 30:
                last_scan_time = time.time()
                new_sym, new_mode, new_profile, new_pot = await asyncio.to_thread(find_best_asset, ex, hours)
                pos_check = portfolio.load().get("open")
                if new_sym != st["symbol"] and new_pot >= (st["current_vol"] * 1.5) and not pos_check:
                    st["change_suggested"] = True
                    st["new_sym"] = new_sym
                    st["new_mode"] = new_mode
                    st["new_profile"] = new_profile
                    st["new_vol"] = new_pot
                    await ctx.bot.send_message(chat_id, "🔄 <b>Найдена монета лучше!</b>\nБыло: " + st["symbol"] + "\nСтало: " + new_sym + "\nМеняем?", parse_mode=ParseMode.HTML, reply_markup=kb_change_symbol())
                    continue 

            tf_or_mode = st["mode"]
            friday = await asyncio.to_thread(Friday, st["symbol"], ex)
            sig = await asyncio.to_thread(friday.analyze_filtered, tf_or_mode, tp_mult)
            
            if not st["has_coins"] and sig.action == "SELL":
                sig.action = "HOLD"
                
            pos = portfolio.load().get("open")
            
            if pos and not st["breakeven_set"]:
                entry = pos["entry"]
                side = pos["side"]
                cp = sig.price
                if side == "BUY" and (cp - entry) / entry >= 0.02:
                    st["breakeven_set"] = True
                    await ctx.bot.send_message(chat_id, "🛡 <b>Защита в безубыток!</b> (Авто)", parse_mode=ParseMode.HTML)
                elif side == "SELL" and (entry - cp) / entry >= 0.02:
                    st["breakeven_set"] = True
                    await ctx.bot.send_message(chat_id, "🛡 <b>Защита в безубыток!</b> (Авто)", parse_mode=ParseMode.HTML)

            if pos and sig.action != "HOLD" and not st["spam_guard"]:
                if pos["side"] == "BUY" and sig.action == "SELL":
                    await ctx.bot.send_message(chat_id, "🚨 <b>СРОЧНО ЗАКРЫВАЙ!</b>\nТренд вниз.", parse_mode=ParseMode.HTML)
                    st["spam_guard"] = True
                elif pos["side"] == "SELL" and sig.action == "BUY":
                    await ctx.bot.send_message(chat_id, "🚨 <b>СРОЧНО ЗАКРЫВАЙ!</b>\nТренд вверх.", parse_mode=ParseMode.HTML)
                    st["spam_guard"] = True

            if sig.action in ("BUY", "SELL") and sig.confidence >= profile["min_confidence"]:
                if not pos:
                    now = time.time()
                    if now - st["last_sent"].get(sig.action, 0) >= 60:
                        st["last_sent"][sig.action] = now
                        risk_pct = RISK_PERCENT[0.85] if sig.confidence >= 0.85 else (RISK_PERCENT[0.75] if sig.confidence >= 0.75 else RISK_PERCENT[0.00])
                        risk_usdt = st["balance"] * risk_pct * risk_modifier if st["balance"] > 0 else 1.0
                        await ctx.bot.send_message(chat_id, "🤖 ВХОД\n" + sig.pretty() + "\n💡 Риск: " + str(round(risk_usdt, 2)) + " USDT", parse_mode=ParseMode.HTML, reply_markup=kb_signal_buttons(st["symbol"], sig.action, sig.price, False))
                        try:
                            path = await asyncio.to_thread(build_chart, st["symbol"], tf_or_mode, sig, "chart_auto.png")
                            with open(path, "rb") as f:
                                await ctx.bot.send_photo(chat_id, photo=f, caption=sig.action + " " + tf_or_mode + " " + st["symbol"], reply_markup=kb_running(ex, "AUTO"))
                        except:
                            pass
        except Exception as e:
            log.warning("auto error: " + str(e))
        await asyncio.sleep(5)

async def cmd_start(update, ctx):
    await update.message.reply_text("Пятница на связи.", reply_markup=kb_start())

async def cmd_stats(update, ctx):
    await update.message.reply_text(stats_text(), reply_markup=kb_start())

async def cmd_stop(update, ctx):
    st = get_state(update.effective_chat.id)
    st["live"] = False
    st["pending_buy"] = None
    st["pending_sell"] = None
    st["breakeven_set"] = False
    st["awaiting_coin_qty"] = False
    st["spam_guard"] = False
    if st.get("task"):
        st["task"].cancel()
        st["task"] = None
    await update.message.reply_text("остановлено.", reply_markup=kb_after_stop(st["exchange"], st["symbol"]))

async def on_button(update, ctx):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    st = get_state(chat_id)
    data = q.data

    if data == "noop":
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

    if data == "close_pos":
        pos = portfolio.load().get("open")
        if not pos:
            await q.message.reply_text("Нет позиции.")
            return
        st["pending_sell"] = {"symbol": pos["symbol"], "price": 0.0, "available": pos["qty"]}
        await q.message.reply_text("Напиши цену (0 - текущая):")
        return

    if data == "menu:start":
        await q.message.reply_text("меню:", reply_markup=kb_start())
        return

    if data == "auto:start":
        await q.message.reply_text("🤖 <b>АВТОПИЛОТ</b>\nСначала выбери время:", parse_mode=ParseMode.HTML, reply_markup=kb_auto_time())
        return

    if data.startswith("autotime:"):
        arg = data.split(":")[1]
        if arg == "custom_hours":
            st["pending_hours"] = True
            await q.message.reply_text("Сколько часов?")
        elif arg == "custom_days":
            st["pending_days"] = True
            await q.message.reply_text("Сколько дней?")
        else:
            st["auto_hours"] = float(arg)
            await q.message.reply_text("Принято. Теперь выбери биржу для сканирования:", reply_markup=kb_exchanges_auto())
        return

    if data.startswith("autoex:"):
        ex = data.split(":")[1]
        st["exchange"] = ex
        st["auto_pending"] = True
        st["awaiting_balance"] = True
        await q.message.reply_text("Сколько у тебя всего USDT на балансе?")
        return

    if data.startswith("tf_ok:"):
        arg = data.split(":")[1]
        st["awaiting_tf"] = False
        if arg == "yes":
            coin_name = st["symbol"].split("/")[0]
            await q.message.reply_text(
                "✅ Отлично. Режим: " + st["mode"] + "\nТеперь скажи, у тебя есть " + coin_name + " в наличии?",
                reply_markup=kb_has_coins()
            )
            st["awaiting_has_coins"] = True
        else:
            await q.message.reply_text(
                "Выбери удобный таймфрейм:",
                reply_markup=kb_tf_select()
            )
        return

    if data.startswith("autotf:"):
        arg = data.split(":")[1]
        if arg == "1m":
            st["mode"] = "scalp"
        elif arg == "15m":
            st["mode"] = "intraday"
        elif arg == "4h":
            st["mode"] = "swing"
        elif arg == "custom":
            st["awaiting_custom_tf"] = True
            await q.message.reply_text("Напиши число минут (например: 5 или 30):")
            return
            
        coin_name = st["symbol"].split("/")[0]
        await q.message.reply_text(
            "✅ Установлено: " + st["mode"] + "\nТеперь скажи, у тебя есть " + coin_name + " в наличии?",
            reply_markup=kb_has_coins()
        )
        st["awaiting_has_coins"] = True
        return

    if data.startswith("hascoins:"):
        arg = data.split(":")[1]
        st["awaiting_has_coins"] = False
        if arg == "yes":
            st["has_coins"] = True
            st["awaiting_coin_qty"] = True
            await q.message.reply_text("Сколько монет у тебя есть?")
        else:
            st["has_coins"] = False
            await q.message.reply_text("✅ Принято. Жду только BUY. Запускаю слежку...")
            st["live"] = True
            st["task"] = asyncio.create_task(_auto_loop(chat_id, ctx, st, st["auto_hours"]))
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
        await q.message.reply_text("загружаю ВСЕ пары с биржи...")
        try:
            pairs = await asyncio.to_thread(get_cached_pairs, ex, quote)
        except Exception as e:
            await q.message.reply_text("ошибка: " + str(e))
            return
        await q.message.reply_text("найдено " + str(len(pairs)) + " пар. Выбери монету (или используй поиск):", reply_markup=kb_pairs(ex, quote, pairs, 0))
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
        await q.message.reply_text("пара " + symbol + "\nшаг 4: профиль", reply_markup=kb_profile_select(ex, symbol))
        return

    if data.startswith("profile:"):
        profile_key = data.split(":")[1]
        st["profile"] = profile_key
        profile = PROFILES[profile_key]
        await q.message.reply_text("профиль: " + profile["label"] + "\nшаг 5: тип сигнала", reply_markup=kb_type_select(st["exchange"], st["symbol"]))
        return

    if data.startswith("stype:"):
        arg = data.split(":")[1]
        if arg == "back":
            await q.message.reply_text("выбери тип:", reply_markup=kb_type_select(st["exchange"], st["symbol"]))
            return
        st["signal_type"] = arg
        label = "сигналы" if arg == "signals" else ("микро-профит" if arg == "rapid" else ("прогноз" if arg == "forecast" else "микро-шаг"))
        await q.message.reply_text("тип: " + label + "\nшаг 6: режим", reply_markup=kb_mode_select(st["exchange"], st["symbol"]))
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
        await q.message.reply_text("напиши код монеты, например SOL или PEPE")
        return

    if data.startswith("lmode:"):
        _, mode_name, ex, symbol = data.split(":")
        st["exchange"] = ex
        st["symbol"] = symbol
        st["mode"] = mode_name
        await q.message.reply_text("тип " + st["signal_type"] + "\nрежим " + mode_name + "\nбиржа " + EXCHANGES[ex] + "\nпара " + symbol + "\n\nжми ЗАПУСТИТЬ", reply_markup=kb_confirm_start(ex, symbol, mode_name))
        return

    if data.startswith("start_live:"):
        _, ex, symbol, mode = data.split(":")
        st["exchange"] = ex
        st["symbol"] = symbol
        st["mode"] = mode
        st["live"] = True
        st["interval"] = 60 if mode == "scalp" else (300 if mode == "intraday" else 900)
        st["breakeven_set"] = False
        st["spam_guard"] = False
        if st.get("task"):
            st["task"].cancel()
        st["task"] = asyncio.create_task(_live_loop(chat_id, ctx, st))
        return

    if data.startswith("stop:"):
        st["live"] = False
        st["pending_buy"] = None
        st["pending_sell"] = None
        st["breakeven_set"] = False
        st["awaiting_coin_qty"] = False
        st["spam_guard"] = False
        if st.get("task"):
            st["task"].cancel()
            st["task"] = None
        await q.message.reply_text("остановлено. что дальше?", reply_markup=kb_after_stop(st["exchange"], st["symbol"]))
        return

    if data.startswith("bought:"):
        _, symbol, action, price_str = data.split(":")
        price = float(price_str)
        st["pending_buy"] = {"symbol": symbol, "action": action, "price": price}
        st["breakeven_set"] = False
        st["spam_guard"] = False
        
        risk_pct = RISK_PERCENT[0.85] if True else RISK_PERCENT[0.00]
        risk_usdt = st["balance"] * risk_pct if st["balance"] > 0 else 1.0
        suggested_qty = risk_usdt / price if price > 0 else 0
        
        word = "купил" if action == "BUY" else "открыл шорт"
        await q.message.reply_text(word + "\nсколько?\n💡 Риск " + str(round(risk_usdt, 2)) + " USDT. Примерно " + fmt_price(suggested_qty) + " монет.", parse_mode=ParseMode.HTML)
        return

    if data.startswith("skip:"):
        st["pending_buy"] = None
        st["pending_sell"] = None
        await q.message.reply_text("пропущено.")
        return

    if data.startswith("sold:"):
        _, symbol, price_str = data.split(":")
        price = float(price_str)
        pos = portfolio.load().get("open")
        if not pos:
            return
        qty = pos["qty"] / 2
        trade = portfolio.close_position(price, qty)
        if "error" not in trade:
            st["daily_profit"] += trade["pnl_abs"]
            if trade["pnl_pct"] < 0:
                st["losses_in_row"] += 1
            else:
                st["losses_in_row"] = 0
            await q.message.reply_text("Продано 50%. Профит: " + str(round(trade["pnl_abs"], 2)) + " USDT")
        return
        
    if data.startswith("sold_all:"):
        _, symbol, price_str = data.split(":")
        price = float(price_str)
        pos = portfolio.load().get("open")
        if not pos:
            return
        trade = portfolio.close_position(price, pos["qty"])
        if "error" not in trade:
            st["daily_profit"] += trade["pnl_abs"]
            st["budget_spent"] += abs(trade["entry"] * trade["qty"])
            if trade["pnl_pct"] < 0: 
                st["losses_in_row"] += 1
                if st["losses_in_row"] >= 3:
                    st["pause_until"] = time.time() + 1800
                    await q.message.reply_text("⚠️ 3 убытка. Пауза 30 мин.")
            else:
                st["losses_in_row"] = 0
            await q.message.reply_text("Закрыто. PnL: " + str(round(trade["pnl_abs"], 2)) + " USDT")
        return

async def on_text(update, ctx):
    st = get_state(update.effective_chat.id)
    text = update.message.text.strip().replace(",", ".")

    if st.get("awaiting_custom_tf"):
        text_val = update.message.text.strip().replace(",", ".")
        try:
            mins = float(text_val)
            if mins <= 0:
                raise ValueError
        except Exception:
            await update.message.reply_text("нужно положительное число, например 5 или 30")
            return
        st["awaiting_custom_tf"] = False
        st["custom_tf"] = str(int(mins)) + "m"
        st["mode"] = "intraday"
        coin_name = st["symbol"].split("/")[0]
        await update.message.reply_text(
            "✅ Установлено: " + st["custom_tf"] + "\nТеперь скажи, у тебя есть " + coin_name + " в наличии?",
            reply_markup=kb_has_coins()
        )
        st["awaiting_has_coins"] = True
        return

    if st.get("awaiting_balance"):
        st["balance"] = float(text)
        st["awaiting_balance"] = False
        st["awaiting_budget"] = True
        await update.message.reply_text("Сколько готов потратить за сегодня?")
        return

    if st.get("awaiting_budget"):
        st["daily_budget"] = float(text)
        st["awaiting_budget"] = False
        if st.get("auto_pending"):
            st["auto_pending"] = False
            await update.message.reply_text("Бюджет принят. Запускаю Автопилот...")
            st["live"] = True
            st["task"] = asyncio.create_task(_auto_loop(update.effective_chat.id, ctx, st, st["auto_hours"]))
        return

    if st.get("awaiting_coin_qty"):
        qty = float(text)
        exchange = getattr(ccxt, st["exchange"])({"enableRateLimit": True})
        ticker = exchange.fetch_ticker(st["symbol"])
        portfolio.open_position(st["symbol"], "BUY", float(ticker["last"]), qty)
        st["awaiting_coin_qty"] = False
        await update.message.reply_text("Добавлено в портфель. Запускаю Автопилот...")
        st["live"] = True
        st["task"] = asyncio.create_task(_auto_loop(update.effective_chat.id, ctx, st, st["auto_hours"]))
        return

    if st.get("pending_buy"):
        qty = float(text)
        portfolio.open_position(st["pending_buy"]["symbol"], st["pending_buy"]["action"], st["pending_buy"]["price"], qty)
        st["pending_buy"] = None
        await update.message.reply_text("Позиция открыта.")
        return

    if st.get("pending_sell"):
        price = float(text)
        pos = portfolio.load().get("open")
        if price == 0.0:
            exchange = getattr(ccxt, st["exchange"])({"enableRateLimit": True})
            price = float(exchange.fetch_ticker(pos["symbol"])["last"])
        trade = portfolio.close_position(price, pos["qty"])
        st["pending_sell"] = None
        if "error" not in trade:
            st["budget_spent"] += abs(trade["entry"] * trade["qty"])
            await update.message.reply_text("Закрыто. PnL: " + str(round(trade["pnl_abs"], 2)))
        return

    if st.get("pending_hours"):
        st["auto_hours"] = float(text)
        st["pending_hours"] = False
        await update.message.reply_text("Принято: " + str(st["auto_hours"]) + " ч.\nТеперь выбери биржу для сканирования:", reply_markup=kb_exchanges_auto())
        return

    if st.get("pending_days"):
        days = float(text)
        st["auto_hours"] = days * 24
        st["pending_days"] = False
        await update.message.reply_text("Принято: " + str(days) + " дн.\nТеперь выбери биржу для сканирования:", reply_markup=kb_exchanges_auto())
        return

    if st.get("search_mode"):
        st["search_mode"] = False
        query = text.upper()
        ex = st["exchange"]
        quote = st.get("quote", "USDT")
        try:
            pairs = await asyncio.to_thread(get_cached_pairs, ex, quote)
            matches = [p for p in pairs if query in p.split("/")[0]]
            if not matches:
                await update.message.reply_text("не найдена")
                return
            rows = []
            for m in matches[:15]:
                rows.append([InlineKeyboardButton(m, callback_data="pair:" + ex + ":" + quote + ":" + m.split("/")[0])])
            await update.message.reply_text("нашёл " + str(len(matches)) + ":", reply_markup=InlineKeyboardMarkup(rows))
        except Exception as e:
            await update.message.reply_text("ошибка: " + str(e))
        return

def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("нет токена")
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
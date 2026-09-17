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
from config import TELEGRAM_TOKEN, DEFAULT_SYMBOL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("friday")

STATE = {}
PAGE_SIZE = 20
CACHE = {}
MIN_CONFIDENCE_LIVE = 0.33

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
        "interval": 60,
        "live": False,
        "task": None,
        "last_sent": None,
        "page": 0,
        "search_mode": False,
    })


# ─────────── КЛАВИАТУРЫ ───────────

def kb_start() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Начать — выбрать биржу", callback_data="menu:exchanges")],
        [InlineKeyboardButton("⚙️ Текущая настройка", callback_data="menu:current")],
    ])


def kb_exchanges() -> InlineKeyboardMarkup:
    rows, row = [], []
    for key, label in EXCHANGES.items():
        row.append(InlineKeyboardButton(label, callback_data=f"ex:{key}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("← назад", callback_data="menu:start")])
    return InlineKeyboardMarkup(rows)


def kb_quotes(exchange_key: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for q in DEFAULT_QUOTES:
        row.append(InlineKeyboardButton(q, callback_data=f"quote:{exchange_key}:{q}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("← назад", callback_data="menu:exchanges")])
    return InlineKeyboardMarkup(rows)


def kb_pairs(exchange_key: str, quote: str, pairs: list, page: int = 0) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(pairs) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = pairs[start:start + PAGE_SIZE]
    rows, row = [], []
    for sym in chunk:
        base = sym.split("/")[0]
        row.append(InlineKeyboardButton(base, callback_data=f"pair:{exchange_key}:{quote}:{base}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"page:{exchange_key}:{quote}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"page:{exchange_key}:{quote}:{page+1}"))
    if len(nav) > 1:
        rows.append(nav)
    rows.append([InlineKeyboardButton("📉 Дешёвые → дорогие", callback_data=f"sort:{exchange_key}:{quote}:asc")])
    rows.append([InlineKeyboardButton("📈 Дорогие → дешёвые", callback_data=f"sort:{exchange_key}:{quote}:desc")])
    rows.append([InlineKeyboardButton("🔍 Поиск пары", callback_data=f"search:{exchange_key}:{quote}")])
    rows.append([InlineKeyboardButton("← назад", callback_data=f"back:quotes:{exchange_key}")])
    return InlineKeyboardMarkup(rows)


def kb_mode_select(exchange_key: str, symbol: str) -> InlineKeyboardMarkup:
    """выбор режима → дальше кнопка ЗАПУСТИТЬ."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Скальп (свечи 1m)", callback_data=f"lmode:scalp:{exchange_key}:{symbol}")],
        [InlineKeyboardButton("⏱ Интрадей (свечи 15m)", callback_data=f"lmode:intraday:{exchange_key}:{symbol}")],
        [InlineKeyboardButton("📈 Свинг (свечи 4h)", callback_data=f"lmode:swing:{exchange_key}:{symbol}")],
        [InlineKeyboardButton("← назад", callback_data=f"back:pairs:{exchange_key}")],
    ])


def kb_confirm_start(exchange_key: str, symbol: str, mode: str) -> InlineKeyboardMarkup:
    """экран с кнопкой ЗАПУСТИТЬ."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ ЗАПУСТИТЬ", callback_data=f"start_live:{exchange_key}:{symbol}:{mode}")],
        [InlineKeyboardButton("← другой режим", callback_data=f"pair:{exchange_key}:{symbol.split('/')[1]}:{symbol.split('/')[0]}")],
    ])


def kb_running(exchange_key: str, symbol: str, mode: str, interval_label: str) -> InlineKeyboardMarkup:
    """во время live — только кнопка СТОП."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏹ СТОП  ·  проверка каждые {interval_label}", callback_data=f"stop:{exchange_key}:{symbol}")],
    ])


def kb_after_stop(exchange_key: str, symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1 минута",  callback_data=f"go:{exchange_key}:{symbol}:60")],
        [InlineKeyboardButton("5 минут",   callback_data=f"go:{exchange_key}:{symbol}:300")],
        [InlineKeyboardButton("15 минут",  callback_data=f"go:{exchange_key}:{symbol}:900")],
        [InlineKeyboardButton("1 час",     callback_data=f"go:{exchange_key}:{symbol}:3600")],
        [InlineKeyboardButton("4 часа",    callback_data=f"go:{exchange_key}: б{symbol}:14400")],
        [InlineKeyboardButton("🔁 Сменить пару", callback_data=f"back:pairs:{exchange_key}")от],
        [InlineKeyboardButton("🏦 Сменить биржу", пи callback_data="menu:exchanges")],
       шет [InlineKeyboardButton("🏠 Главное меню:
",   callback_data="menu:  start")],
    ])


# ─────────── УТИЛИТЫ ───────────

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
        label = f"{base} — {price:,.4f}" if price < 1 else f"{base} — {price:,.2f}"
        rows.append([InlineKeyboardButton(label, callback_data=f"pair:{exchange_key}:{quote}:{base}")])
    rows.append([InlineKeyboardButton("📉 Дешёвые → дорогие", callback_data=f"sort:{exchange_key}:{quote}:asc")])
    rows.append([InlineKeyboardButton("📈 Дорогие → дешёвые", callback_data=f"sort:{exchange_key}:{quote}:desc")])
    rows.append([InlineKeyboardButton("🔍 Поиск пары", callback_data=f"search:{exchange_key}:{quote}")])
    rows.append([InlineKeyboardButton("← назад", callback_data=f"back:quotes:{exchange_key}")])
    return InlineKeyboardMarkup(rows)


# ─────────── LIVE LOOP ───────────

async def _live_loop(chat_id: int, ctx, st: dict):
    interval = st["interval"]
    ex = st["exchange"]
    sym = st["symbol"]
    mode = st["mode"]
    interval_label = next((k for k, v in INTERVAL_OPTIONS.items() if v == interval), f"{interval}s")

    await ctx.bot.send_message(
        chat_id,
        f"▶️ <b>Пятница запущена</b>\n\n"
        f"биржа: <b>{EXCHANGES[ex]}</b>\n"
        f"пара: <code>{sym}</code>\n"
        f"режим: <b>{mode}</b>\n"
        f"проверка каждые <b>{interval_label}</b>\n\n"
        f"слежу. напишу когда появится сигнал.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_running(ex, sym, mode, interval_label),
    )

    st["last_sent"] = None
    while st["live"]:
        try:
            friday = await asyncio.to_thread(Friday, sym, ex)
            sig = await asyncio.to_thread(friday.analyze_filtered, Mode(mode))

            if sig.action in ("BUY", "SELL") and sig.confidence >= MIN_CONFIDENCE_LIVE:
                signature = (sig.action, round(sig.price, 4))
                if signature != st["last_sent"]:
                    st["last_sent"] = signature
                    await ctx.bot.send_message(chat_id, "🔔 " + sig.pretty(), parse_mode=ParseMode.HTML)
                    try:
                        path = await asyncio.to_thread(build_chart, sym, Mode(mode), sig, f"chart_{mode}.png")
                        with open(path, "rb") as f:
                            await ctx.bot.send_photo(chat_id, photo=f,
                                                     caption=f"{sig.action} · {mode} · {sym}",
                                                     reply_markup=kb_running(ex, sym, mode, interval_label))
                    except Exception as e:
                        log.warning(f"chart error: {e}")
        except Exception as e:
            log.warning(f"live error: {e}")

        for _ in range(interval):
            if not st["live"]:
                return
            await asyncio.sleep(1)


# ─────────── ХЕНДЛЕРЫ ───────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Пятница на связи.</b>\n\nвыбери, что делать:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_start(),
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_chat.id)
    st["live"] = False
    if st.get("task"):
        st["task"].cancel(); st["task"] = None
    ex = st["exchange"]; sym = st["symbol"]
    await update.message.reply_text(
        "⏹ остановлено.\n<b>что дальше?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_after_stop(ex, sym),
    )


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    st = get_state(chat_id)
    data = q.data

    if data == "noop":
        return

    if data == "menu:start":
        await q.message.reply_text("<b>Пятница.</b> выбери, что делать:", parse_mode=ParseMode.HTML, reply_markup=kb_start())
        return

    if data == "menu:current":
        il = next((k for k, v in INTERVAL_OPTIONS.items() if v == st.get("interval")), f"{st.get('interval', 60)}s")
        await q.message.reply_text(
            f"текущая настройка:\n"
            f"биржа: <code>{EXCHANGES.get(st['exchange'], st['exchange'])}</code>\n"
            f"пара: <code>{st['symbol']}</code>\n"
            f"режим: <code>{st.get('mode')}</code>\n"
            f"интервал: <code>{il}</code>\n"
            f"live: {'вкл' if st['live'] else 'выкл'}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_start(),
        )
        return

    if data == "menu:exchanges":
        await q.message.reply_text("<b>Шаг 1/4.</b> Выбери биржу:", parse_mode=ParseMode.HTML, reply_markup=kb_exchanges())
        return

    if data.startswith("ex:"):
        ex = data.split(":", 1)[1]
        st["exchange"] = ex
        await q.message.reply_text(
            f"биржа: <b>{EXCHANGES[ex]}</b>\n<b>Шаг 2/4.</b> Выбери котируемую валюту:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_quotes(ex),
        )
        return

    if data.startswith("quote:"):
        _, ex, quote = data.split(":")
        st["quote"] = quote
        st["page"] = 0
        await q.message.reply_text(f"загружаю пары с {EXCHANGES[ex]}...")
        try:
            pairs = await asyncio.to_thread(get_cached_pairs, ex, quote)
        except Exception as e:
            await q.message.reply_text(f"ошибка загрузки пар: {e}")
            return
        await q.message.reply_text(
            f"биржа: <b>{EXCHANGES[ex]}</b> · котировка: <b>{quote}</b>\n"
            f"всего пар: <b>{len(pairs)}</b>\n<b>Шаг 3/4.</b> Выбери пару (стр. 1):",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_pairs(ex, quote, pairs, page=0),
        )
        return

    if data.startswith("page:"):
        _, ex, quote, page_str = data.split(":")
        page = int(page_str)
        st["page"] = page
        pairs = get_cached_pairs(ex, quote)
        await q.message.reply_text(
            f"<b>Шаг 3/4.</b> Стр. {page+1}:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_pairs(ex, quote, pairs, page=page),
        )
        return

    if data.startswith("back:quotes:"):
        ex = data.split(":")[2]
        await q.message.reply_text("выбери котируемую валюту:", reply_markup=kb_quotes(ex))
        return

    if data.startswith("pair:"):
        _, ex, quote, base = data.split(":")
        symbol = f"{base}/{quote}"
        st["symbol"] = symbol
        st["quote"] = quote
        await q.message.reply_text(
            f"биржа: <b>{EXCHANGES[ex]}</b>\nпара: <code>{symbol}</code>\n"
            f"<b>Шаг 4/4.</b> Выбери режим:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_mode_select(ex, symbol),
        )
        return

    if data.startswith("back:pairs:"):
        ex = data.split(":")[2]
        quote = st.get("quote", "USDT")
        pairs = get_cached_pairs(ex, quote)
        await q.message.reply_text(
            "<b>Шаг 3/4.</b> Выбери пару:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_pairs(ex, quote, pairs, page=st.get("page", 0)),
        )
        return

    if data.startswith("sort:"):
        _, ex, quote, order = data.split(":")
        await q.message.reply_text("считаю цены, секунду...")
        pairs = get_cached_pairs(ex, quote)
        rows = await asyncio.to_thread(sort_pairs_by_price, ex, quote, order, pairs)
        if not rows:
            await q.message.reply_text("не удалось получить цены, попробуй другую биржу")
            return
        header = "📉 от дешёвых к дорогим:" if order == "asc" else "📈 от дорогих к дешёвым:"
        await q.message.reply_text(header, reply_markup=kb_pairs_sorted(ex, quote, rows, order))
        return

    if data.startswith("search:"):
        _, ex, quote = data.split(":")
        st["search_mode"] = True
        await q.message.reply_text(
            "напиши код монеты — например <code>SOL</code> или <code>DOGE</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── выбран режим → экран с кнопкой ЗАПУСТИТЬ ──
    if data.startswith("lmode:"):
        _, mode_name, ex, symbol = data.split(":")
        st["exchange"] = ex
        st["symbol"] = symbol
        st["mode"] = mode_name
        await q.message.reply_text(
            f"✅ режим <b>{mode_name}</b>\n"
            f"биржа: <b>{EXCHANGES[ex]}</b>\n"
            f"пара: <code>{symbol}</code>\n\n"
            f"нажми ЗАПУСТИТЬ — начну следить и напишу когда пора BUY или SELL.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_confirm_start(ex, symbol, mode_name),
        )
        return

    # ── ▶️ ЗАПУСТИТЬ ──
    if data.startswith("start_live:"):
        _, ex, symbol, mode = data.split(":")
        st["exchange"] = ex
        st["symbol"] = symbol
        st["mode"] = mode
        # интервал по умолчанию для режима
        default_interval = {"scalp": 60, "intraday": 300, "swing": 900}.get(mode, 60)
        st["interval"] = default_interval
        st["live"] = True
        if st.get("task"):
            st["task"].cancel()
        st["task"] = asyncio.create_task(_live_loop(chat_id, ctx, st))
        return

    # ── запуск после стопа с выбранным интервалом ──
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

    # ── ⏹ СТОП ──
    if data.startswith("stop:"):
        st["live"] = False
        if st.get("task"):
            st["task"].cancel(); st["task"] = None
        ex = st["exchange"]; sym = st["symbol"]
        await q.message.reply_text(
            "⏹ <b>остановлено.</b>\nчто дальше?",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_after_stop(ex, sym),
        )
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
            await update.message.reply_text(f"пара с «{query}» не найдена")
            return
        rows = [[InlineKeyboardButton(m, callback_data=f"pair:{ex}:{quote}:{m.split('/')[0]}")] for m in matches[:15]]
        await update.message.reply_text(f"нашёл {len(matches)}:", reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        await update.message.reply_text(f"ошибка: {e}")


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("нет TELEGRAM_TOKEN")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop",  cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Пятница запущена. polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

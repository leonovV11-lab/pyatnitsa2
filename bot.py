# language: Python 3.10+, file: bot.py
import asyncio
import logging
import time
import ccxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from friday import Friday, Mode
from chart import build_chart
from exchanges import EXCHANGES, DEFAULT_QUOTES, load_all_pairs
from config import TELEGRAM_TOKEN, DEFAULT_SYMBOL, WATCH_INTERVAL, ALERT_COOLDOWN

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("friday")

STATE = {}
PAGE_SIZE = 20          # кнопок-пар на страницу
CACHE = {}              # кэш пар: (exchange, quote) -> list


def get_state(chat_id: int) -> dict:
    return STATE.setdefault(chat_id, {
        "exchange": "bybit",
        "symbol": DEFAULT_SYMBOL,
        "watching": False,
        "task": None,
        "last_alerts": {},
        "page": 0,
        "cache_key": None,
        "search_mode": False,
    })


# ─────────── КЛАВИАТУРЫ ───────────

def kb_start() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Начать — выбрать биржу", callback_data="menu:exchanges")],
        [InlineKeyboardButton("⚙️ Текущая настройка", callback_data="menu:current")],
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
    rows.append([InlineKeyboardButton("← назад", callback_data="menu:start")])
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
    rows.append([InlineKeyboardButton("← назад", callback_data="menu:exchanges")])
    return InlineKeyboardMarkup(rows)


def kb_pairs(exchange_key: str, quote: str, pairs: list, page: int = 0) -> InlineKeyboardMarkup:
    """пагинация по списку всех пар. по 20 на страницу, 3 в ряд."""
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

    # навигация страниц
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"page:{exchange_key}:{quote}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"page:{exchange_key}:{quote}:{page+1}"))
    if len(nav) > 1:
        rows.append(nav)

    rows.append([InlineKeyboardButton("📉 Дешёвые → дорогие", callback_data=f"sort:{exchange_key}:{quote}:asc")])
    rows.append([InlineKeyboardButton("📈 Дорогие → дешёвые", callback_data=f"sort:{exchange_key}:{quote}:desc")])
    rows.append([InlineKeyboardButton("🔍 Поиск пары", callback_data=f"search:{exchange_key}:{quote}")])
    rows.append([InlineKeyboardButton("← назад", callback_data=f"back:quotes:{exchange_key}")])
    return InlineKeyboardMarkup(rows)


def kb_modes(exchange_key: str, symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Скальп (1m)",  callback_data=f"mode:scalp:{exchange_key}:{symbol}")],
        [InlineKeyboardButton("⏱ Интрадей (15m)", callback_data=f"mode:intraday:{exchange_key}:{symbol}")],
        [InlineKeyboardButton("📈 Свинг (4h)",   callback_data=f"mode:swing:{exchange_key}:{symbol}")],
        [InlineKeyboardButton("🔍 Все три сразу", callback_data=f"mode:all:{exchange_key}:{symbol}")],
        [InlineKeyboardButton("👁 Авто-режим",  callback_data=f"watch:toggle:{exchange_key}:{symbol}")],
        [InlineKeyboardButton("← назад",        callback_data=f"back:pairs:{exchange_key}")],
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
    """сортирует пары по текущей цене. тянет цены только для пар на текущей странице + запас."""
    limit = min(len(pairs), 60)  # первая страница + запас, чтобы не дёргать биржу 500 раз
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


# ─────────── СИГНАЛЫ ───────────

async def send_scan(chat_id: int, ctx, exchange_key: str, symbol: str, modes: list):
    for m in modes:
        try:
            friday = await asyncio.to_thread(Friday, symbol, exchange_key)
            sig = await asyncio.to_thread(friday.analyze_filtered, m)
            await ctx.bot.send_message(chat_id, sig.pretty(), parse_mode=ParseMode.HTML)
            if sig.action != "HOLD":
                path = await asyncio.to_thread(build_chart, symbol, m, sig, f"chart_{m.value}.png")
                with open(path, "rb") as f:
                    await ctx.bot.send_photo(chat_id, photo=f,
                                             caption=f"{sig.action} · {m.value} · {symbol}")
        except Exception as e:
            await ctx.bot.send_message(chat_id, f"[{m.value}] ошибка: {e}")


# ─────────── ХЕНДЛЕРЫ ───────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Пятница на связи.</b>\n\nвыбери, что делать:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_start(),
    )


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """поиск пары: /find sol"""
    st = get_state(update.effective_chat.id)
    if not ctx.args:
        await update.message.reply_text("формат: /find SOL")
        return
    query = ctx.args[0].upper()
    ex = st["exchange"]
    quote = st.get("quote", "USDT")
    try:
        pairs = await asyncio.to_thread(get_cached_pairs, ex, quote)
        matches = [p for p in pairs if query in p.split("/")[0]]
        if not matches:
            await update.message.reply_text(f"на {EXCHANGES[ex]} пара с «{query}» в {quote} не найдена")
            return
        rows = [[InlineKeyboardButton(m, callback_data=f"pair:{ex}:{quote}:{m.split('/')[0]}")] for m in matches[:10]]
        await update.message.reply_text(f"нашёл {len(matches)}:", reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        await update.message.reply_text(f"ошибка: {e}")


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
        await q.message.reply_text(
            f"текущая настройка:\n"
            f"биржа: <code>{EXCHANGES.get(st['exchange'], st['exchange'])}</code>\n"
            f"пара: <code>{st['symbol']}</code>\n"
            f"авто: {'вкл' if st['watching'] else 'выкл'}",
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
        await q.message.reply_text(
            f"биржа: <b>{EXCHANGES[ex]}</b>\nпара: <code>{symbol}</code>\n<b>Шаг 4/4.</b> Выбери режим:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_modes(ex, symbol),
        )
        return

    if data.startswith("back:pairs:"):
        ex = data.split(":")[2]
        quote = st.get("quote", "USDT")
        pairs = get_cached_pairs(ex, quote)
        await q.message.reply_text(
            f"<b>Шаг 3/4.</b> Выбери пару:",
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
        header = "📉 от дешёвых к дорогим (первые 20):" if order == "asc" else "📈 от дорогих к дешёвым (первые 20):"
        await q.message.reply_text(header, reply_markup=kb_pairs_sorted(ex, quote, rows, order))
        return

    if data.startswith("search:"):
        _, ex, quote = data.split(":")
        st["search_mode"] = True
        await q.message.reply_text(
            f"напиши код монеты — например <code>SOL</code> или <code>DOGE</code>.\n"
            f"или командой: <code>/find SOL</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("mode:"):
        parts = data.split(":")
        if parts[1] == "all":
            _, _, ex, symbol = parts
            await q.message.reply_text(f"сканирую <code>{symbol}</code> на <b>{EXCHANGES[ex]}</b> во всех режимах...", parse_mode=ParseMode.HTML)
            await send_scan(chat_id, ctx, ex, symbol, list(Mode))
        else:
            _, mode_name, ex, symbol = parts
            m = Mode(mode_name)
            await q.message.reply_text(f"скан <code>{symbol}</code> · {m.value}...", parse_mode=ParseMode.HTML)
            await send_scan(chat_id, ctx, ex, symbol, [m])
        await q.message.reply_text("готово. что дальше?", reply_markup=kb_modes(ex, st["symbol"]))
        return

    if data.startswith("watch:toggle:"):
        _, _, ex, symbol = data.split(":")
        st["exchange"] = ex
        st["symbol"] = symbol
        st["watching"] = not st["watching"]
        if st["watching"]:
            st["task"] = asyncio.create_task(_watch_loop(chat_id, ctx, st))
            await q.message.reply_text(f"👁 авто включён для <code>{symbol}</code> на <b>{EXCHANGES[ex]}</b>", parse_mode=ParseMode.HTML)
        else:
            if st.get("task"):
                st["task"].cancel(); st["task"] = None
            await q.message.reply_text("авто выключен")
        return


# ─────────── ПОИСК ТЕКСТОМ ───────────

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ловим текст когда включён режим поиска."""
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
            await update.message.reply_text(f"пара с «{query}» в {quote} не найдена на {EXCHANGES[ex]}")
            return
        rows = [[InlineKeyboardButton(m, callback_data=f"pair:{ex}:{quote}:{m.split('/')[0]}")] for m in matches[:15]]
        await update.message.reply_text(f"нашёл {len(matches)}:", reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        await update.message.reply_text(f"ошибка: {e}")


# ─────────── WATCH LOOP ───────────

async def _watch_loop(chat_id: int, ctx, st: dict):
    while st["watching"]:
        try:
            friday = await asyncio.to_thread(Friday, st["symbol"], st["exchange"])
            for m in Mode:
                if not st["watching"]:
                    return
                try:
                    sig = await asyncio.to_thread(friday.analyze_filtered, m)
                    if sig.action == "HOLD":
                        continue
                    key = (m.value, sig.action)
                    now = time.time()
                    if now - st["last_alerts"].get(key, 0) < ALERT_COOLDOWN:
                        continue
                    st["last_alerts"][key] = now
                    await ctx.bot.send_message(chat_id, "🔔 " + sig.pretty(), parse_mode=ParseMode.HTML)
                except Exception as e:
                    log.warning(f"watch error {m.value}: {e}")
        except Exception as e:
            log.warning(f"watch outer error: {e}")
        await asyncio.sleep(WATCH_INTERVAL)


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_chat.id)
    await send_scan(update.effective_chat.id, ctx, st["exchange"], st["symbol"], list(Mode))


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_chat.id)
    st["watching"] = False
    if st.get("task"):
        st["task"].cancel(); st["task"] = None
    await update.message.reply_text("остановлено")


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("нет TELEGRAM_TOKEN")
    from telegram.ext import MessageHandler, filters
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan",  cmd_scan))
    app.add_handler(CommandHandler("stop",  cmd_stop))
    app.add_handler(CommandHandler("find",  cmd_find))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Пятница запущена. polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

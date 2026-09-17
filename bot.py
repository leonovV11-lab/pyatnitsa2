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
from exchanges import EXCHANGES, POPULAR_BASES, DEFAULT_QUOTES
from config import TELEGRAM_TOKEN, DEFAULT_SYMBOL, WATCH_INTERVAL, ALERT_COOLDOWN

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("friday")

STATE = {}


def get_state(chat_id: int) -> dict:
    return STATE.setdefault(chat_id, {
        "exchange": "bybit",
        "symbol": DEFAULT_SYMBOL,
        "watching": False,
        "task": None,
        "last_alerts": {},
    })


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
    for q in DEFAULT_QUOTES:
        rows.append([InlineKeyboardButton(f"⭐ {q}", callback_data=f"quote:{exchange_key}:{q}")])
    rows.append([InlineKeyboardButton("← назад", callback_data="menu:exchanges")])
    return InlineKeyboardMarkup(rows)


def kb_pairs(exchange_key: str, quote: str) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for base in POPULAR_BASES:
        row.append(InlineKeyboardButton(base, callback_data=f"pair:{exchange_key}:{quote}:{base}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("📉 Дешёвые → дорогие", callback_data=f"sort:{exchange_key}:{quote}:asc")])
    rows.append([InlineKeyboardButton("📈 Дорогие → дешёвые", callback_data=f"sort:{exchange_key}:{quote}:desc")])
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


def get_market_price(exchange_key: str, symbol: str) -> float:
    try:
        ex = getattr(ccxt, exchange_key)({"enableRateLimit": True})
        t = ex.fetch_ticker(symbol)
        return float(t["last"])
    except Exception:
        return 0.0


def sort_pairs_by_price(exchange_key: str, quote: str, order: str) -> list:
    rows = []
    for base in POPULAR_BASES:
        pair = f"{base}/{quote}"
        price = get_market_price(exchange_key, pair)
        if price > 0:
            rows.append((base, price))
    rows.sort(key=lambda x: x[1], reverse=(order == "desc"))
    return rows


def kb_pairs_sorted(exchange_key: str, quote: str, sorted_rows: list, order: str) -> InlineKeyboardMarkup:
    rows = []
    for base, price in sorted_rows:
        label = f"{base} — {price:,.4f}" if price < 1 else f"{base} — {price:,.2f}"
        rows.append([InlineKeyboardButton(label, callback_data=f"pair:{exchange_key}:{quote}:{base}")])
    rows.append([InlineKeyboardButton("📉 Дешёвые → дорогие", callback_data=f"sort:{exchange_key}:{quote}:asc")])
    rows.append([InlineKeyboardButton("📈 Дорогие → дешёвые", callback_data=f"sort:{exchange_key}:{quote}:desc")])
    rows.append([InlineKeyboardButton("← назад", callback_data=f"back:quotes:{exchange_key}")])
    return InlineKeyboardMarkup(rows)


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


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Пятница на связи.</b>\n\nвыбери, что делать:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_start(),
    )


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    st = get_state(chat_id)
    data = q.data

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
        await q.message.reply_text(
            f"биржа: <b>{EXCHANGES[ex]}</b> · котировка: <b>{quote}</b>\n<b>Шаг 3/4.</b> Выбери пару:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_pairs(ex, quote),
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
        await q.message.reply_text("выбери котировку:", reply_markup=kb_quotes(ex))
        return

    if data.startswith("sort:"):
        _, ex, quote, order = data.split(":")
        await q.message.reply_text("считаю цены, секунду...", parse_mode=ParseMode.HTML)
        rows = await asyncio.to_thread(sort_pairs_by_price, ex, quote, order)
        if not rows:
            await q.message.reply_text("не удалось получить цены. проверь биржу, попробуй другую.")
            return
        header = "📉 от дешёвых к дорогим:" if order == "asc" else "📈 от дорогих к дешёвым:"
        await q.message.reply_text(header, reply_markup=kb_pairs_sorted(ex, quote, rows, order))
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
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan",  cmd_scan))
    app.add_handler(CommandHandler("stop",  cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Пятница запущена. polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

# bot.py
import asyncio
import io
import json
import os
import random
import time
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

from config import TELEGRAM_TOKEN, PROFILES, RISK_PERCENT
from exchanges import EXCHANGES, load_all_pairs
from friday import Friday
from chart import build_chart
import portfolio

# ============ ГЛОБАЛЬНЫЕ СОСТОЯНИЯ ============
STATES = {}          # chat_id -> state dict
PORTFOLIO_FILE = "portfolio.json"


def get_state(chat_id):
    if chat_id not in STATES:
        STATES[chat_id] = {
            "mode": None,             # "manual" | "auto"
            "running": False,
            "task": None,
            "exchange": None,
            "symbol": None,
            "tf": None,
            "timeframe": None,
            "profile": "low",
            "signal": None,
            "auto_hours": 1,
            "awaiting_custom_tf": False,
            "awaiting_balance": False,
            "awaiting_daily_limit": False,
            "awaiting_qty": False,
            "awaiting_manual_tf": False,
            "balance": None,
            "daily_limit": None,
            "daily_pnl": 0.0,
            "loss_streak": 0,
            "paused_until": None,
            "history": [],
            "last_scan": None,
        }
    return STATES[chat_id]


# ============ КЛАВИАТУРЫ ============
def kb_start():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Ручной режим", callback_data="mode:manual")],
        [InlineKeyboardButton("🤖 Автопилот", callback_data="mode:auto")],
        [InlineKeyboardButton("📊 Статистика", callback_data="menu:stats")],
        [InlineKeyboardButton("📁 История", callback_data="hist:menu")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="menu:help")],
    ])


def kb_profile():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Большая прибыль (7R)", callback_data="profile:high")],
        [InlineKeyboardButton("⚖️ Малая прибыль (4R)", callback_data="profile:low")],
        [InlineKeyboardButton("⚡ Микро-профит (1R)", callback_data="profile:rapid")],
    ])


def kb_exchanges():
    btns = []
    row = []
    for ex in EXCHANGES.keys():
        row.append(InlineKeyboardButton(ex.capitalize(), callback_data=f"ex:{ex}"))
        if len(row) == 3:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    btns.append([InlineKeyboardButton("◀️ Назад", callback_data="back:start")])
    return InlineKeyboardMarkup(btns)


def kb_auto_time():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("30 мин", callback_data="autotime:0.5")],
        [InlineKeyboardButton("1 час", callback_data="autotime:1")],
        [InlineKeyboardButton("2 часа", callback_data="autotime:2")],
        [InlineKeyboardButton("4 часа", callback_data="autotime:4")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back:start")],
    ])


def kb_tf_select():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1 минута", callback_data="tf:1m"),
         InlineKeyboardButton("5 минут", callback_data="tf:5m")],
        [InlineKeyboardButton("15 минут", callback_data="tf:15m"),
         InlineKeyboardButton("1 час", callback_data="tf:1h")],
        [InlineKeyboardButton("4 часа", callback_data="tf:4h"),
         InlineKeyboardButton("1 день", callback_data="tf:1d")],
        [InlineKeyboardButton("✏️ Свое время (минуты)", callback_data="tf:custom")],
    ])


def kb_hascoins():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Есть монеты", callback_data="hascoins:yes")],
        [InlineKeyboardButton("❌ Нет монет", callback_data="hascoins:no")],
    ])


def kb_history_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Все сделки", callback_data="hist:view:all")],
        [InlineKeyboardButton("🟢 BUY", callback_data="hist:view:buy")],
        [InlineKeyboardButton("🔴 SELL", callback_data="hist:view:sell")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back:start")],
    ])


def kb_confirm_switch(symbol):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Сменить монету", callback_data=f"switch:{symbol}")],
        [InlineKeyboardButton("✋ Оставить текущую", callback_data="switch:stay")],
    ])


# ============ ФОРМАТИРОВАНИЕ ============
def fmt_price(p):
    try:
        p = float(p)
        if p >= 100:
            return f"{p:,.2f}"
        if p >= 1:
            return f"{p:,.4f}"
        return f"{p:.6f}"
    except Exception:
        return str(p)


def smart_lot(price, stop, balance, confidence):
    """Возвращает (qty, risk_usdt)."""
    try:
        if not balance or not price or not stop or price == stop:
            return 0, 0
        # риск 1-3% в зависимости от уверенности
        conf = max(0.5, min(1.0, confidence or 0.6))
        risk_pct = 1.0 + (conf - 0.5) * 4  # 1% .. 3%
        risk_usdt = balance * risk_pct / 100
        per_coin = abs(price - stop)
        if per_coin <= 0:
            return 0, 0
        qty = risk_usdt / per_coin
        return qty, risk_usdt
    except Exception:
        return 0, 0


def stats_text(chat_id):
    try:
        s = portfolio.stats()
    except Exception as e:
        return f"⚠️ Не удалось получить статистику: {e}"
    txt = (
        "📊 <b>Статистика</b>\n\n"
        f"Всего сделок: <b>{s.get('total', 0)}</b>\n"
        f"Winrate: <b>{s.get('winrate', 0):.1f}%</b>\n"
        f"Средний профит: <b>{s.get('avg_profit', 0):.2f}%</b>\n"
        f"Профактор: <b>{s.get('profit_factor', 0):.2f}</b>\n"
        f"Общий PnL: <b>{s.get('total_pnl', 0):.2f} USDT</b>\n"
    )
    return txt


def history_text(chat_id, filt="all"):
    st = get_state(chat_id)
    hist = st.get("history", [])
    if filt in ("buy", "sell"):
        hist = [h for h in hist if h.get("side", "").lower() == filt]
    if not hist:
        return "📁 История пуста."
    lines = ["📁 <b>История сделок</b>\n"]
    for h in hist[-15:]:
        side = h.get("side", "?").upper()
        emoji = "🟢" if side == "BUY" else "🔴"
        pnl = h.get("pnl_pct", 0)
        lines.append(
            f"{emoji} {side} {h.get('symbol','?')} | "
            f"{h.get('entry',0):.4f} → {h.get('exit',0):.4f} | "
            f"{pnl:+.2f}%"
        )
    return "\n".join(lines)


# ============ СКАНЕР ============
async def find_best_asset(exchange, quote="USDT"):
    try:
        ex_class = getattr(EXCHANGES[exchange]["class"], "__call__", None)
    except Exception:
        pass
    try:
        ex = EXCHANGES[exchange]["class"]()
        pairs = await asyncio.to_thread(load_all_pairs, ex, quote)
        if not pairs:
            return None
        random.shuffle(pairs)
        pairs = pairs[:150]
        best = None
        best_score = -1
        for sym in pairs:
            try:
                ohlcv = await asyncio.to_thread(ex.fetch_ohlcv, sym, "15m", None, 60)
                if not ohlcv or len(ohlcv) < 20:
                    continue
                df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
                vol = df["v"].sum()
                rng = (df["h"].max() - df["l"].min()) / df["c"].mean()
                score = vol * rng
                if score > best_score:
                    best_score = score
                    best = sym
            except Exception:
                continue
        return best
    except Exception:
        return None


# ============ ЖИВОЙ РУЧНОЙ ЦИКЛ ============
async def _live_loop(chat_id, ctx, st):
    st["running"] = True
    st["mode"] = "manual"
    try:
        while st["running"]:
            sig = st.get("signal")
            if not sig:
                await asyncio.sleep(5)
                continue
            try:
                ex = EXCHANGES[st["exchange"]]["class"]()
                ticker = await asyncio.to_thread(ex.fetch_ticker, st["symbol"])
                price = ticker["last"]
            except Exception as e:
                await asyncio.sleep(10)
                continue

            entry = sig.get("price", price)
            stop = sig.get("stop", price)
            take = sig.get("take", price)
            side = sig.get("side", "buy")

            # Проверки
            if side == "buy":
                if price <= stop:
                    await ctx.bot.send_message(chat_id, f"🛑 Стоп-лосс сработал на {fmt_price(price)}. Закрывай позицию!")
                    st["running"] = False
                    break
                if price >= take:
                    await ctx.bot.send_message(chat_id, f"🎯 Тейк-профит достигнут на {fmt_price(price)}!")
                    st["running"] = False
                    break
                # Защита в безубыток
                if price >= entry * 1.02 and not sig.get("be_moved"):
                    sig["be_moved"] = True
                    sig["stop"] = entry
                    await ctx.bot.send_message(
                        chat_id,
                        f"🛡 Защита в безубыток: цена {fmt_price(price)} (+2%). Стоп передвинут на вход {fmt_price(entry)}."
                    )
            else:
                if price >= stop:
                    await ctx.bot.send_message(chat_id, f"🛑 Стоп-лосс сработал на {fmt_price(price)}.")
                    st["running"] = False
                    break
                if price <= take:
                    await ctx.bot.send_message(chat_id, f"🎯 Тейк-профит достигнут на {fmt_price(price)}!")
                    st["running"] = False
                    break

            # Паника
            if side == "buy" and price <= entry * 0.97 and not sig.get("panic_alert"):
                sig["panic_alert"] = True
                await ctx.bot.send_message(chat_id, "🚨 СРОЧНО ЗАКРЫВАЙ! Цена пробила -3%.")
            if side == "sell" and price >= entry * 1.03 and not sig.get("panic_alert"):
                sig["panic_alert"] = True
                await ctx.bot.send_message(chat_id, "🚨 СРОЧНО ЗАКРЫВАЙ! Цена пробила +3%.")

            await asyncio.sleep(15)
    finally:
        st["running"] = False


# ============ АВТОПИЛОТ ============
async def _auto_loop(chat_id, ctx, st, hours=1):
    st["running"] = True
    st["mode"] = "auto"
    end_at = time.time() + hours * 3600
    try:
        while st["running"] and time.time() < end_at:
            # Пауза после серии убытков
            if st.get("paused_until") and time.time() < st["paused_until"]:
                await asyncio.sleep(30)
                continue

            # Дневной лимит
            if st.get("daily_limit") is not None and st["daily_pnl"] <= -abs(st["daily_limit"]):
                await ctx.bot.send_message(
                    chat_id,
                    f"⛔ Дневной лимит исчерпан ({st['daily_pnl']:.2f} USDT). Автопилот остановлен."
                )
                st["running"] = False
                break

            # Скан рынка
            best = await find_best_asset(st.get("exchange") or "bybit")
            if best and best != st.get("symbol"):
                st["last_scan"] = (best, time.time())
                try:
                    await ctx.bot.send_message(
                        chat_id,
                        f"🔍 Найдена монета лучше: <b>{best}</b>\nСменить?",
                        reply_markup=kb_confirm_switch(best),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            else:
                # Просто анализируем текущую
                if st.get("symbol") and not st.get("signal"):
                    try:
                        fr = Friday(st["exchange"], st["symbol"], st.get("timeframe") or "15m",
                                    st.get("profile") or "low")
                        sig = await asyncio.to_thread(fr.analyze_filtered)
                        if sig and sig.get("valid"):
                            st["signal"] = sig
                            await ctx.bot.send_message(
                                chat_id,
                                f"📈 Сигнал {sig.get('side','?').upper()} {st['symbol']} "
                                f"уверенность {sig.get('confidence',0):.2f}"
                            )
                    except Exception:
                        pass

            # Трекинг текущей позиции
            if st.get("signal"):
                try:
                    ex = EXCHANGES[st["exchange"]]["class"]()
                    ticker = await asyncio.to_thread(ex.fetch_ticker, st["symbol"])
                    price = ticker["last"]
                    sig = st["signal"]
                    side = sig.get("side", "buy")
                    stop = sig.get("stop", 0)
                    take = sig.get("take", 0)
                    if side == "buy" and (price <= stop or price >= take):
                        pnl_pct = (price - sig["price"]) / sig["price"] * 100
                        st["daily_pnl"] += pnl_pct
                        if pnl_pct < 0:
                            st["loss_streak"] = st.get("loss_streak", 0) + 1
                            if st["loss_streak"] >= 3:
                                st["paused_until"] = time.time() + 30 * 60
                                st["loss_streak"] = 0
                                await ctx.bot.send_message(chat_id, "⏸ 3 убытка подряд. Пауза 30 минут.")
                        else:
                            st["loss_streak"] = 0
                        st.setdefault("history", []).append({
                            "symbol": st["symbol"], "side": side,
                            "entry": sig["price"], "exit": price, "pnl_pct": pnl_pct
                        })
                        st["signal"] = None
                        await ctx.bot.send_message(
                            chat_id,
                            f"{'🎯' if pnl_pct > 0 else '🛑'} Закрытие {st['symbol']} по {fmt_price(price)} | PnL: {pnl_pct:+.2f}%"
                        )
                except Exception:
                    pass

            await asyncio.sleep(30)
    finally:
        st["running"] = False
        try:
            await ctx.bot.send_message(chat_id, "🏁 Автопилот завершил работу.")
        except Exception:
            pass


# ============ ОБРАБОТЧИКИ ============
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = get_state(chat_id)
    st["mode"] = None
    st["running"] = False
    await update.message.reply_text(
        "👋 Привет! Я <b>Пятница</b> — твой торговый терминал.\n\nВыбери режим:",
        reply_markup=kb_start(),
        parse_mode="HTML",
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = get_state(chat_id)
    st["running"] = False
    st["mode"] = None
    if st.get("task") and not st["task"].done():
        st["task"].cancel()
    await update.message.reply_text("⏹ Остановлено.", reply_markup=kb_start())


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = update.effective_chat.id
    st = get_state(chat_id)
    data = q.data

    # ===== Меню =====
    if data == "menu:stats":
        await q.edit_message_text(stats_text(chat_id), parse_mode="HTML", reply_markup=kb_start())
        return

    if data == "hist:menu":
        await q.edit_message_text("📁 Выбери фильтр истории:", reply_markup=kb_history_menu())
        return

    if data.startswith("hist:view:"):
        filt = data.split(":")[2]
        await q.edit_message_text(history_text(chat_id, filt), parse_mode="HTML", reply_markup=kb_history_menu())
        return

    if data == "menu:help":
        await q.edit_message_text(
            "ℹ️ <b>Помощь</b>\n\n"
            "• Ручной режим — сам выбираешь пару, бот ведет сделку.\n"
            "• Автопилот — бот сам сканирует рынок и предлагает лучший сетап.\n"
            "• Статистика — общие результаты.\n"
            "• История — список последних сделок.\n\n"
            "Команды: /start, /stop",
            parse_mode="HTML",
            reply_markup=kb_start(),
        )
        return

    if data == "back:start":
        await q.edit_message_text("Выбери режим:", reply_markup=kb_start())
        return

    # ===== Выбор режима =====
    if data == "mode:manual":
        st["mode"] = "manual"
        st["timeframe"] = None
        await q.edit_message_text("Выбери таймфрейм:", reply_markup=kb_tf_select())
        return

    if data == "mode:auto":
        st["mode"] = "auto"
        await q.edit_message_text("⏱ На сколько времени включаем автопилот?", reply_markup=kb_auto_time())
        return

    # ===== Таймфрейм =====
    if data.startswith("tf:"):
        tf = data.split(":")[1]
        if tf == "custom":
            st["awaiting_custom_tf"] = True
            await q.edit_message_text("✏️ Введи количество минут (например, 3):")
            return
        st["timeframe"] = tf
        st["awaiting_custom_tf"] = False
        # Спросить профиль
        await q.edit_message_text("Выбери профиль риска:", reply_markup=kb_profile())
        return

    # ===== Профиль =====
    if data.startswith("profile:"):
        prof = data.split(":")[1]
        st["profile"] = prof
        await q.edit_message_text("Выбери биржу:", reply_markup=kb_exchanges())
        return

    # ===== Биржа =====
    if data.startswith("ex:"):
        ex = data.split(":")[1]
        st["exchange"] = ex
        await q.edit_message_text("🎯 Ищу лучший сетап...")
        best = await find_best_asset(ex)
        if not best:
            await q.edit_message_text("⚠️ Не удалось найти пару. Попробуй другую биржу.", reply_markup=kb_exchanges())
            return
        st["symbol"] = best
        await q.edit_message_text(
            f"🎯 Лучший сетап: <b>{best}</b>\nУстраивает время?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Устраивает", callback_data="tf_ok")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back:start")],
            ]),
        )
        return

    if data == "tf_ok":
        await q.edit_message_text("Есть ли у тебя монеты?", reply_markup=kb_hascoins())
        return

    # ===== Автовремя =====
    if data.startswith("autotime:"):
        hours = float(data.split(":")[1])
        st["auto_hours"] = hours
        # сначала спросим баланс
        st["awaiting_balance"] = True
        await q.edit_message_text("💰 Введи свой баланс в USDT (числом):")
        return

    # ===== Есть ли монеты =====
    if data == "hascoins:no":
        st["status"] = "waiting_buy"
        await q.edit_message_text("👌 Жду только BUY-сигналы. Запускаю автопилот...")
        if st["mode"] == "auto":
            st["task"] = asyncio.create_task(_auto_loop(chat_id, ctx, st, st.get("auto_hours", 1)))
        return

    if data == "hascoins:yes":
        st["awaiting_qty"] = True
        await q.edit_message_text("✏️ Введи количество монет, которое у тебя есть:")
        return

    # ===== Переключение монеты автопилотом =====
    if data.startswith("switch:"):
        val = data.split(":")[1]
        if val == "stay":
            await q.edit_message_text("✋ Оставляю текущую монету.")
        else:
            st["symbol"] = val
            st["signal"] = None
            await q.edit_message_text(f"🔄 Переключился на <b>{val}</b>.", parse_mode="HTML")
        return


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = get_state(chat_id)
    text = (update.message.text or "").strip()

    # === Свое время ===
    if st.get("awaiting_custom_tf"):
        try:
            minutes = int(text)
            if minutes <= 0:
                raise ValueError
            st["timeframe"] = f"{minutes}m"
            st["awaiting_custom_tf"] = False
            await update.message.reply_text(f"✅ Таймфрейм установлен: {minutes}m")
            await update.message.reply_text("Выбери профиль риска:", reply_markup=kb_profile())
            return
        except Exception:
            await update.message.reply_text("⚠️ Введи целое число минут (например, 3):")
            return

    # === Баланс ===
    if st.get("awaiting_balance"):
        try:
            st["balance"] = float(text.replace(",", "."))
            st["awaiting_balance"] = False
            await update.message.reply_text(f"💰 Баланс: {st['balance']:.2f} USDT")
            st["awaiting_daily_limit"] = True
            await update.message.reply_text("📉 Введи дневной лимит убытка в USDT (например, 50):")
            return
        except Exception:
            await update.message.reply_text("⚠️ Введи число (например, 100):")
            return

    # === Дневной лимит ===
    if st.get("awaiting_daily_limit"):
        try:
            st["daily_limit"] = float(text.replace(",", "."))
            st["awaiting_daily_limit"] = False
            await update.message.reply_text(f"📉 Дневной лимит: {st['daily_limit']:.2f} USDT")
            # спросить про монеты
            await update.message.reply_text("Есть ли у тебя монеты на бирже?", reply_markup=kb_hascoins())
            return
        except Exception:
            await update.message.reply_text("⚠️ Введи число:")
            return

    # === Количество монет (если есть) ===
    if st.get("awaiting_qty"):
        try:
            qty = float(text.replace(",", "."))
            st["awaiting_qty"] = False
            price = None
            if st.get("symbol") and st.get("exchange"):
                try:
                    ex = EXCHANGES[st["exchange"]]["class"]()
                    ticker = await asyncio.to_thread(ex.fetch_ticker, st["symbol"])
                    price = ticker["last"]
                except Exception:
                    pass
            if price:
                try:
                    portfolio.open_position(st["symbol"], "buy", price, qty)
                    await update.message.reply_text(
                        f"✅ Добавлено в портфель: {qty} {st['symbol']} по {fmt_price(price)}"
                    )
                except Exception as e:
                    await update.message.reply_text(f"⚠️ Ошибка портфеля: {e}")
            else:
                await update.message.reply_text("⚠️ Не удалось получить цену, но количество сохранено.")
            # ЗАПУСК АВТОПИЛОТА
            if st["mode"] == "auto" and not st.get("running"):
                st["task"] = asyncio.create_task(_auto_loop(chat_id, ctx, st, st.get("auto_hours", 1)))
                await update.message.reply_text("🚀 Автопилот запущен.")
            return
        except Exception:
            await update.message.reply_text("⚠️ Введи число (количество монет):")
            return

    # === Стандартный ввод вне режима ===
    await update.message.reply_text("Не понял. Используй /start для меню.")


# ============ ЗАПУСК ============
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("✅ Бот Пятница запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
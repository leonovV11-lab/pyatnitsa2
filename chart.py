# language: Python 3.10+, file: chart.py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
from friday import Friday, Mode, MODE_CONFIG, ema


def build_chart(symbol: str, mode: Mode, signal, out_path: str = "signal.png", bars: int = 100) -> str:
    friday = Friday(symbol=symbol)
    cfg = MODE_CONFIG[mode]
    need = bars + cfg["ema_slow"] + 10
    df = friday.fetch(mode).tail(need).copy()
    df["ema_fast"] = ema(df["close"], cfg["ema_fast"])
    df["ema_slow"] = ema(df["close"], cfg["ema_slow"])
    df = df.dropna().tail(bars).reset_index(drop=True)
    df = df.set_index("ts")
    ohlc = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                              "close": "Close", "volume": "Volume"})
    ohlc = ohlc[["Open", "High", "Low", "Close", "Volume"]]
    addplots = [
        mpf.make_addplot(df["ema_fast"], color="#3b82f6", width=1.2, panel=0),
        mpf.make_addplot(df["ema_slow"], color="#f59e0b", width=1.2, panel=0),
    ]
    title = f"{symbol}  ·  {mode.value.upper()}  ·  {signal.action}  ({signal.confidence:.0%})"
    fig, axes = mpf.plot(
        ohlc, type="candle", style="charles", addplot=addplots,
        volume=True, title=title, ylabel="price", ylabel_lower="vol",
        figsize=(12, 7), returnfig=True, tight_layout=True,
    )
    ax = axes[0]
    last_price = ohlc["Close"].iloc[-1]
    ax.axhline(last_price, color="#9ca3af", linestyle="--", linewidth=0.8)
    ax.annotate(f"{signal.action} @ {signal.price:.4f}",
                xy=(len(ohlc) - 1, last_price),
                xytext=(len(ohlc) - 25, last_price),
                color="#111827", fontsize=10, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color="#111827"))
    if signal.action != "HOLD":
        ax.axhline(signal.stop_loss, color="#ef4444", linestyle=":", linewidth=1.0)
        ax.axhline(signal.take_profit, color="#10b981", linestyle=":", linewidth=1.0)
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return out_path

from decimal import Decimal

from django.db import models


class PaperAccount(models.Model):
    name = models.CharField(max_length=64, unique=True, default="default")
    cash_cents = models.IntegerField(default=1_000_000)
    auto_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class PaperPosition(models.Model):
    account = models.ForeignKey(PaperAccount, on_delete=models.CASCADE)
    market_ticker = models.CharField(max_length=128)
    market_label = models.CharField(max_length=128, blank=True)
    side = models.CharField(max_length=3, choices=[("yes", "Yes"), ("no", "No")])
    contracts = models.DecimalField(max_digits=14, decimal_places=4, default=Decimal("0"))
    avg_price_cents = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal("0"))
    updated_at = models.DateTimeField(auto_now=True)
    # Set once Kalshi resolves the market: payout is $1/contract if our side won, else 0.
    settled_at = models.DateTimeField(null=True, blank=True)
    payout_cents = models.IntegerField(null=True, blank=True)
    realized_pl_cents = models.IntegerField(null=True, blank=True)

    class Meta:
        unique_together = ("account", "market_ticker", "side")


class PaperTrade(models.Model):
    account = models.ForeignKey(PaperAccount, on_delete=models.CASCADE)
    market_ticker = models.CharField(max_length=128)
    market_label = models.CharField(max_length=128, blank=True)
    action = models.CharField(max_length=4, choices=[("buy", "Buy"), ("sell", "Sell")])
    side = models.CharField(max_length=3, choices=[("yes", "Yes"), ("no", "No")])
    price_cents = models.IntegerField()
    contracts = models.DecimalField(max_digits=14, decimal_places=4)
    cash_delta_cents = models.IntegerField()
    realized_pl_cents = models.IntegerField(null=True, blank=True)  # set on sells only
    created_at = models.DateTimeField(auto_now_add=True)


class AutoTradeRun(models.Model):
    """One scheduled auto-trade cycle. `slot` is unique so a restart can't repeat a cycle."""
    slot = models.CharField(max_length=32, unique=True)  # e.g. "2026-10-17T12:30" (ET)
    ran_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=8)  # traded | skipped | error
    message = models.TextField(blank=True)
    forecast_mean = models.FloatField(null=True, blank=True)
    trades_count = models.IntegerField(default=0)

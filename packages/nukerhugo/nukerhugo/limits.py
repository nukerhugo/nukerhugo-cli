"""Usage tracking and soft limits.

Two layers: the server enforces a hard daily budget (HTTP 429). The client adds a
soft daily cap and a per-session cap that pause the agent and ask what to do.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .api import find_value

LOW_RESERVE = 2_000


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def fmt_tokens(n: int) -> str:
    n = int(n)
    if n >= 100_000:
        return f"{n / 1000:.0f}k"
    if n >= 10_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n:,}"


def parse_limit(value, budget: int) -> int | None:
    """'80%' -> share of budget, '50000' -> tokens. None if invalid."""
    s = str(value).strip().replace(",", "").replace("_", "")
    try:
        if s.endswith("%"):
            pct = float(s[:-1])
            if not 0 < pct <= 100:
                return None
            return int(budget * pct / 100)
        n = int(float(s))
        return n if n > 0 else None
    except ValueError:
        return None


def next_reset_local() -> datetime:
    now = datetime.now(timezone.utc)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.astimezone()


def utc_day():
    return datetime.now(timezone.utc).date()


def reset_text() -> str:
    return next_reset_local().strftime("%H:%M")


class UsageTracker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.budget = cfg.daily_budget
        self.server_used = 0      # last value reported by the server
        self.local_since = 0      # tokens counted locally since that report
        self.session = 0
        self.plan = None
        self.synced = False
        self.turn_allowance = False
        self._warned = 0
        self.day = utc_day()      # the budget day (UTC) these counters belong to

    # ---- numbers --------------------------------------------------------
    def _rollover(self) -> None:
        """The server budget resets at 00:00 UTC; start the new day from zero."""
        today = utc_day()
        if today != self.day:
            self.day = today
            self.server_used = 0
            self.local_since = 0
            self._warned = 0
            self.synced = False  # numbers are an estimate until the next sync

    @property
    def used_today(self) -> int:
        self._rollover()
        return self.server_used + self.local_since

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used_today)

    @property
    def soft_daily(self) -> int:
        return parse_limit(self.cfg.soft_limit, self.budget) or int(self.budget * 0.8)

    def add(self, tokens: int) -> None:
        self._rollover()
        self.session += tokens
        self.local_since += tokens

    # ---- server sync ----------------------------------------------------
    def sync(self, client) -> bool:
        """Refresh from the server's usage endpoint. Returns True on success."""
        try:
            data = client.usage()
        except Exception:
            return False
        budget = find_value(data, ("daily_budget", "budget", "daily_limit", "limit", "tokens_limit"))
        used = find_value(data, ("tokens_used", "used", "used_today"))
        remaining = find_value(data, ("tokens_remaining", "remaining"))
        plan = find_value(data, ("plan", "plan_name"))
        try:
            if budget is not None:
                self.budget = int(budget)
            if used is not None:
                self.server_used = int(used)
            elif remaining is not None:
                self.server_used = max(0, self.budget - int(remaining))
            else:
                return False
        except (TypeError, ValueError):
            return False
        self.local_since = 0
        self.day = utc_day()
        self.plan = str(plan) if plan else None
        self.synced = True
        return True

    # ---- limit checks ---------------------------------------------------
    def sync_after_interrupt(self, client) -> None:
        """An interrupted reply is still billed by the server; pick that up."""
        expected = self.server_used + self.local_since
        if self.sync(client):
            extra = self.server_used - expected
            if extra > 0:
                self.session += extra

    def check(self):
        """Return (kind, message) when the agent should pause, else None."""
        if self.turn_allowance:
            return None
        if self.cfg.limits_enabled:
            if self.used_today >= self.budget:
                return ("daily", f"Daily budget used up ({fmt_tokens(self.used_today)}/"
                                 f"{fmt_tokens(self.budget)} tokens). The server will refuse "
                                 f"requests until {reset_text()}.")
            if self.used_today >= self.soft_daily:
                return ("daily", f"Soft daily limit reached: {fmt_tokens(self.remaining)} tokens "
                                 f"left today (resets {reset_text()}).")
            if self.session >= self.cfg.session_limit:
                return ("session", f"Session limit reached: {fmt_tokens(self.session)} tokens "
                                   f"used this session (limit {fmt_tokens(self.cfg.session_limit)}).")
        if self.remaining < LOW_RESERVE:
            return ("reserve", f"Only {fmt_tokens(self.remaining)} tokens left today - a reply "
                               f"may be cut off mid-way (resets {reset_text()}).")
        return None

    def new_warning(self):
        """One-shot warning when usage crosses a warn_at threshold."""
        frac = self.used_today / self.budget if self.budget else 0
        level = sum(1 for t in sorted(self.cfg.warn_at) if frac >= t)
        if level > self._warned:
            self._warned = level
            return f"{frac * 100:.0f}% of today's token budget used ({fmt_tokens(self.remaining)} left)."
        return None

    def level(self) -> int:
        frac = self.used_today / self.budget if self.budget else 0
        return sum(1 for t in sorted(self.cfg.warn_at) if frac >= t)

    def status_text(self) -> str:
        lim = ""
        if self.cfg.limits_enabled:
            lim = f"/{fmt_tokens(self.cfg.session_limit)}"
        approx = "" if self.synced else "~"
        return (f"session {fmt_tokens(self.session)}{lim} · "
                f"{approx}{fmt_tokens(self.remaining)} left today · resets {reset_text()}")

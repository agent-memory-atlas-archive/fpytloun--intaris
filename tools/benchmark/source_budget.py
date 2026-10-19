"""One thread-safe, cumulative provider-attempt and known-price ledger."""

from __future__ import annotations

import math
import threading


class CumulativeBudget:
    def __init__(
        self,
        *,
        reserve_usd: float,
        max_attempts: int = 4000,
        max_usd: float = 10.0,
        initial_failures: int = 0,
    ) -> None:
        if not math.isfinite(reserve_usd) or not 0 < reserve_usd <= max_usd:
            raise ValueError("Verified conservative per-request price bound required")
        self.reserve_usd, self.max_attempts, self.max_usd = (
            reserve_usd,
            max_attempts,
            max_usd,
        )
        self.attempts = 0
        self.known_usd = 0.0
        self.unpriced_usd = 0.0
        self.unknown_attempts = 0
        if type(initial_failures) is not int or not 0 <= initial_failures < 3:
            raise ValueError("Invalid inherited consecutive failure count")
        self.failures = initial_failures
        self.stop_reason = None
        self._active: set[int] = set()
        self._lock = threading.Lock()

    def reserve(self) -> int | None:
        with self._lock:
            accounted = (
                self.known_usd
                + self.unpriced_usd
                + len(self._active) * self.reserve_usd
            )
            if self.stop_reason:
                return None
            if self.attempts >= self.max_attempts:
                self.stop_reason = "cumulative_attempt_cap"
                return None
            if accounted + self.reserve_usd > self.max_usd:
                self.stop_reason = "cumulative_price_cap"
                return None
            self.attempts += 1
            self._active.add(self.attempts)
            return self.attempts

    def finish(self, attempt: int, record: dict) -> None:
        with self._lock:
            if attempt not in self._active:
                raise ValueError("Unknown/already finalized provider attempt")
            self._active.remove(attempt)
            usage = record.get("usage") or {}
            price = usage.get("cost")
            if price is None and record["provider"] == "groq":
                prompt, completion = (
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                )
                cached = (usage.get("prompt_tokens_details") or {}).get(
                    "cached_tokens", 0
                )
                values = (prompt, completion, cached)
                if (
                    all(
                        isinstance(v, (int, float))
                        and not isinstance(v, bool)
                        and math.isfinite(v)
                        and v >= 0
                        for v in values
                    )
                    and cached <= prompt
                ):
                    rates = (
                        (0.075, 0.037, 0.3)
                        if record["model"] == "openai/gpt-oss-20b"
                        else (0.15, 0.075, 0.6)
                    )
                    price = (
                        (prompt - cached) * rates[0]
                        + cached * rates[1]
                        + completion * rates[2]
                    ) / 1e6
                    record["price_basis"] = "public_groq_tariff_estimate"
            elif price is not None:
                record["price_basis"] = "provider_reported"
            if price is None:
                self.unknown_attempts += 1
                self.unpriced_usd += self.reserve_usd
                record["price_basis"] = "unknown_reserved_upper_bound"
            elif (
                not isinstance(price, (int, float))
                or isinstance(price, bool)
                or not math.isfinite(price)
                or price < 0
            ):
                self.stop_reason = "invalid_usage_price"
                self.unpriced_usd += self.reserve_usd
                self.unknown_attempts += 1
            else:
                self.known_usd += price
                record["accounted_price_usd"] = price
                if price > self.reserve_usd:
                    self.stop_reason = "verified_price_bound_exceeded"
            self.failures = (
                self.failures + 1
                if record.get("status", 0) >= 400 or record.get("error")
                else 0
            )
            if self.failures >= 3:
                self.stop_reason = "three_consecutive_upstream_failures"

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "attempts": self.attempts,
                "max_attempts": self.max_attempts,
                "known_or_estimated_usd": self.known_usd,
                "unknown_attempts": self.unknown_attempts,
                "unknown_reserved_usd": self.unpriced_usd,
                "active_reservation_usd": len(self._active) * self.reserve_usd,
                "max_usd": self.max_usd,
                "price_bound_usd": self.reserve_usd,
                "consecutive_failures": self.failures,
                "stop_reason": self.stop_reason,
                "not_provider_invoice": True,
            }

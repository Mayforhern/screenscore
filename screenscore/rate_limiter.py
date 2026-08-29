"""Rate limiter for Gemini API calls to stay within free tier quota."""

import asyncio
import time
import threading
from collections import deque
import logging

logger = logging.getLogger(__name__)


class AsyncRateLimiter:
    """Sliding window rate limiter that blocks when limit is reached.
    
    Uses asyncio.sleep instead of time.sleep to avoid blocking the event loop.
    """

    def __init__(self, max_requests: int = 5, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def _purge_old(self, now: float) -> None:
        """Remove timestamps outside the sliding window."""
        while self._timestamps and self._timestamps[0] <= now - self.window_seconds:
            self._timestamps.popleft()

    def _check_and_record(self) -> float:
        """Check if a request can proceed. Returns seconds to wait, or 0."""
        with self._lock:
            now = time.monotonic()
            self._purge_old(now)

            if len(self._timestamps) >= self.max_requests:
                # Must wait until the oldest request expires
                sleep_for = self._timestamps[0] + self.window_seconds - now
                return max(sleep_for, 0)

            # Slot available — record it
            self._timestamps.append(now)
            return 0

    def _record_after_wait(self) -> None:
        """Record a timestamp after waiting."""
        with self._lock:
            now = time.monotonic()
            self._purge_old(now)
            self._timestamps.append(now)

    async def acquire(self) -> float:
        """Async: block until a request slot is available. Returns seconds waited."""
        wait_time = self._check_and_record()
        if wait_time > 0:
            logger.info("Rate limiter: must wait %.1fs for request slot", wait_time)
            await asyncio.sleep(wait_time)
            self._record_after_wait()
            return wait_time
        return 0


# Global singleton shared across all agents
# gemini-3.1-flash-lite free tier: 5 RPM, 250K input tokens/min
# Set to 4 RPM to leave headroom for retries
gemini_limiter = AsyncRateLimiter(max_requests=4, window_seconds=60.0)

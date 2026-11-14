"""Exactly one process dials.

Several engine processes can serve the API, the audio of calls and background jobs
side by side: each of those is safe to run many times over, because the database
decides who gets a lead, a call or a job. Two things are not: the dial loops, whose
pacing, daily limits and concurrency are counted per loop, and the reconciler. Those
run in the one process that holds the lease named "dialer".

A lease is a row with a holder and an expiry. The holder renews it well before it
expires. A process that cannot renew (it stopped, it lost the database) stops dialing
before the lease runs out, and another process takes over after it has.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import time
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import Lease, run_db, utcnow

log = logging.getLogger(__name__)

DIALER = "dialer"


def new_holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"[:120]


def acquire(s: Session, name: str, holder: str, ttl_seconds: float, now: datetime | None = None) -> bool:
    """Take the lease, or renew it if this holder has it. True when it is ours until
    ``now + ttl``. One statement decides, so two processes asking at once cannot both win."""
    now = now or utcnow()
    expires = now + timedelta(seconds=ttl_seconds)
    mine = s.execute(update(Lease).where(Lease.name == name, Lease.holder == holder, Lease.expires_at >= now).values(expires_at=expires)).rowcount
    if mine == 1:
        return True
    taken = s.execute(update(Lease).where(Lease.name == name, Lease.expires_at < now).values(holder=holder, expires_at=expires, acquired_at=now)).rowcount
    if taken == 1:
        return True
    if s.execute(select(Lease.name).where(Lease.name == name)).first() is not None:
        return False  # someone else holds it and it has not run out
    try:
        with s.begin_nested():
            s.add(Lease(name=name, holder=holder, expires_at=expires, acquired_at=now))
            s.flush()
        return True
    except IntegrityError:  # another process created it at the same moment
        return False


def release(s: Session, name: str, holder: str) -> None:
    """Give the lease up now (a clean stop), so the next process need not wait for it to run out."""
    s.execute(update(Lease).where(Lease.name == name, Lease.holder == holder).values(expires_at=utcnow() - timedelta(seconds=1)))


def holder_of(s: Session, name: str) -> str | None:
    row = s.execute(select(Lease.holder).where(Lease.name == name, Lease.expires_at >= utcnow())).first()
    return row[0] if row else None


class Leadership:
    """Keeps trying to hold a lease, and tells the caller when it is gained and lost."""

    def __init__(self, name: str, ttl_seconds: float, on_gained: Callable[[], Awaitable[None]], on_lost: Callable[[], Awaitable[None]], holder: str | None = None):
        self.name, self.ttl, self.holder = name, ttl_seconds, holder or new_holder()
        self._on_gained, self._on_lost = on_gained, on_lost
        self.leading = False
        self._renewed_at = 0.0
        self._task: asyncio.Task | None = None

    async def tick(self) -> bool:
        """One attempt to take or renew. Returns whether this process leads afterwards."""
        try:
            held = await run_db(lambda s: acquire(s, self.name, self.holder, self.ttl), unrestricted=True)
        except Exception as e:
            # The database could not be asked. The lease may still be ours for a while,
            # but once two thirds of it have passed without a renewal, stop: someone
            # else may be about to take over, and two dialers is the one thing to prevent.
            held = self.leading and (time.monotonic() - self._renewed_at) < self.ttl * 2 / 3
            log.warning("the %s lease could not be renewed (%s); %s", self.name, type(e).__name__, "still leading" if held else "not leading")
        else:
            if held:
                self._renewed_at = time.monotonic()
        if held and not self.leading:
            self.leading = True
            log.info("this process now holds the %s lease (%s)", self.name, self.holder)
            await self._on_gained()
        elif not held and self.leading:
            self.leading = False
            log.warning("this process no longer holds the %s lease", self.name)
            await self._on_lost()
        return self.leading

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("leadership check failed")
            await asyncio.sleep(self.ttl / 3)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"lease-{self.name}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self.leading:
            self.leading = False
            await self._on_lost()
            try:
                await run_db(lambda s: release(s, self.name, self.holder), unrestricted=True)
            except Exception:
                log.warning("the %s lease could not be released; it will run out by itself", self.name)

"""Login health notifications without session contents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable


class LoginMonitor:
    def __init__(
        self,
        state: Callable[[], str],
        notify: Callable[[str], Awaitable[None]],
        reauth: Callable[[], None],
        changed: Callable[[], None],
    ) -> None:
        self._state = state
        self._notify = notify
        self._reauth = reauth
        self._changed = changed
        self._failed = False

    async def update(self) -> None:
        self._changed()
        failed = self._state() == "failed"
        if not failed:
            self._failed = False
        elif not self._failed:
            self._failed = True
            self._reauth()
            await self._notify(
                "Yale Access needs a new login. Open Settings → Devices & services to re-authenticate."
            )

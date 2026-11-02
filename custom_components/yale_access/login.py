"""Login health notifications without session contents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .const import RELOGIN_COMMAND


class LoginMonitor:
    def __init__(
        self,
        state: Callable[[], str],
        notify: Callable[[str], Awaitable[None]],
        repair: Callable[[bool], None],
        changed: Callable[[], None],
    ) -> None:
        self._state = state
        self._notify = notify
        self._repair = repair
        self._changed = changed
        self._unhealthy = False

    async def update(self) -> None:
        self._changed()
        unhealthy = self._state() != "ok"
        self._repair(unhealthy)
        if not unhealthy:
            self._unhealthy = False
        elif not self._unhealthy:
            self._unhealthy = True
            await self._notify(
                f"Yale login needs attention. Re-login: {RELOGIN_COMMAND}"
            )

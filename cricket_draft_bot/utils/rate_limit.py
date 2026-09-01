import asyncio
import logging
from telegram import InputMediaPhoto
from telegram.error import RetryAfter
from game.state import save_match_state

logger = logging.getLogger(__name__)

# ── Per-chat sliding-window rate gate ────────────────────────────────────────
# Telegram limit: ~20 edits per chat per minute.
# We target 16/min (4 edit safety buffer) — maximum possible speed while
# keeping guaranteed protection from Telegram's hard rate limit.
_CHAT_MAX_CALLS = 16
_CHAT_WINDOW    = 60.0   # rolling window in seconds


_chat_call_times: dict = {}   # chat_id_str -> [float timestamps]
_chat_locks:      dict = {}   # chat_id_str -> asyncio.Lock


async def _acquire_chat_slot(chat_id: int) -> None:
    """
    Non-blocking sliding-window rate gate.
    Prevents exceeding _CHAT_MAX_CALLS in any 60-second window.
    Crucial: Lock is ONLY held for microseconds to check/prune timestamps.
    Any required wait happens OUTSIDE the lock so other matches in the same
    chat are never blocked from checking and scheduling in parallel.
    """
    key = str(chat_id)
    if key not in _chat_locks:
        _chat_locks[key] = asyncio.Lock()

    while True:
        wait = 0.0
        async with _chat_locks[key]:
            now = asyncio.get_event_loop().time()
            times = _chat_call_times.setdefault(key, [])
            _chat_call_times[key] = times = [t for t in times if now - t < _CHAT_WINDOW]

            if len(times) < _CHAT_MAX_CALLS:
                _chat_call_times[key].append(now)
                return
            else:
                # Oldest call in window needs to expire before we can claim a slot
                wait = _CHAT_WINDOW - (now - times[0]) + 0.05

        # Sleep OUTSIDE the lock so other tasks in this chat aren't blocked!
        if wait > 0:
            await asyncio.sleep(wait)



def _count_active_in_chat(tasks: dict, chat_id: int) -> int:
    """How many debouncer tasks are currently active for this chat?"""
    prefix = f"{chat_id}_"
    return sum(1 for k, t in tasks.items() if k.startswith(prefix) and not t.done())


async def cleanup_chat_rate_state() -> None:
    """
    Background loop: remove stale _chat_call_times entries every 10 min.
    Prevents unbounded memory growth when many groups are served over time.
    Start with asyncio.create_task() from post_init().
    """
    while True:
        await asyncio.sleep(600)
        now = asyncio.get_event_loop().time()
        stale = [k for k, v in list(_chat_call_times.items())
                 if not v or now - v[-1] > 600]
        for k in stale:
            _chat_call_times.pop(k, None)
            _chat_locks.pop(k, None)
        if stale:
            logger.debug(f"Rate-gate cleanup: removed {len(stale)} inactive chat entries")


class MessageDebouncer:
    """
    Batches rapid Telegram message edits into a single API call.

    Key design points:
    - Running tasks are NEVER cancelled by newer updates — the latest pending
      state overwrites self._pending, and the running task delivers it.
    - All API calls pass through _acquire_chat_slot(): a per-chat sliding-window
      gate that caps edits at 13/min per group regardless of how many concurrent
      matches share that chat.
    - Debounce delay scales up with concurrent match count in the same chat,
      so heavy load causes natural batching rather than rate-limit errors.
    - Only cancel_updates() (called on match end) actually cancels a task.
    """

    def __init__(self, delay: float = 0.5):

        self.delay      = delay
        self.tasks:      dict = {}
        self.last_state: dict = {}
        self._pending:   dict = {}

    def cancel_updates(self, chat_id: int, message_id: int) -> None:
        """Cancel pending updates for this message (match ended)."""
        key = f"{chat_id}_{message_id}"
        task = self.tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
        self.last_state.pop(key, None)
        self._pending.pop(key, None)

    async def schedule_update(
        self, match, bot, caption: str, reply_markup,
        media=None, parse_mode: str = "Markdown"
    ) -> None:
        if not match.draft_message_id:
            return
        key = f"{match.chat_id}_{match.draft_message_id}"

        # Dedup: skip if UI would look identical
        markup_dict  = reply_markup.to_dict() if reply_markup else None
        target_state = {"text": caption, "media": media, "markup": markup_dict}
        if self.last_state.get(key) == target_state:
            logger.debug(f"Debouncer: ignored duplicate update for {key}")
            return

        # Skip re-uploading unchanged media (much cheaper caption-only edit)
        send_media = media
        if (key in self.last_state
                and self.last_state[key].get("media") == media
                and media is not None):
            send_media = None

        self._pending[key] = (caption, reply_markup, send_media, target_state)

        if key in self.tasks and not self.tasks[key].done():
            return  # running task will pick up the latest _pending state

        # Adaptive delay: +0.15s per extra concurrent match (max safe tuning)
        # The rate gate (16/min) is the real safety net — debounce just batches double-clicks.
        concurrent      = _count_active_in_chat(self.tasks, match.chat_id)
        effective_delay = self.delay + max(0, concurrent - 1) * 0.15



        self.tasks[key] = asyncio.create_task(
            self._execute_update(key, match, bot, parse_mode, effective_delay)
        )

    async def _execute_update(
        self, key: str, match, bot, parse_mode: str, delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)
            _max_iters = 12
            _iters     = 0
            while key in self._pending and _iters < _max_iters:
                _iters += 1
                caption, reply_markup, send_media, target_state = self._pending.pop(key)
                success = await self._run_api_call(
                    bot, match.chat_id, match.draft_message_id,
                    caption, reply_markup, send_media, parse_mode
                )
                if success:
                    self.last_state[key] = target_state
                else:
                    logger.info(f"Debouncer: all retries failed — recreating message for {match.match_id}")
                    await self._recreate_message(
                        match, bot, caption, reply_markup,
                        target_state.get("media"), parse_mode
                    )
                    if match.draft_message_id:
                        new_key = f"{match.chat_id}_{match.draft_message_id}"
                        self.last_state[new_key] = target_state
                        if new_key != key and key in self._pending:
                            self._pending[new_key] = self._pending.pop(key)
                            if new_key not in self.tasks or self.tasks[new_key].done():
                                self.tasks[new_key] = asyncio.create_task(
                                    self._execute_update(new_key, match, bot, parse_mode, 0.5)
                                )
                    return
                if key in self._pending:
                    concurrent  = _count_active_in_chat(self.tasks, match.chat_id)
                    inter_delay = 0.3 + max(0, concurrent - 1) * 0.1
                    await asyncio.sleep(inter_delay)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Debouncer execution error for {key}: {e}")
        finally:
            self.tasks.pop(key, None)

    async def _run_api_call(
        self, bot, chat_id: int, message_id: int,
        text: str, reply_markup, media, parse_mode: str
    ) -> bool:
        try:
            await _acquire_chat_slot(chat_id)   # per-chat rate gate
            media_is_url = bool(media and str(media).startswith("http"))
            href_text = f'<a href="{media}">&#8205;</a>' + text if media_is_url else text

            for attempt in range(3):
                try:
                    if media_is_url:
                        await bot.edit_message_text(
                            chat_id=chat_id, message_id=message_id,
                            text=href_text, reply_markup=reply_markup,
                            parse_mode="HTML", disable_web_page_preview=False
                        )
                        return True
                    elif media:
                        await bot.edit_message_media(
                            chat_id=chat_id, message_id=message_id,
                            media=InputMediaPhoto(media=media, caption=text, parse_mode=parse_mode),
                            reply_markup=reply_markup,
                        )
                        return True
                    else:
                        await bot.edit_message_text(
                            chat_id=chat_id, message_id=message_id,
                            text=text, reply_markup=reply_markup, parse_mode=parse_mode,
                        )
                        return True
                except RetryAfter as e:
                    wait_time = e.retry_after + 1
                    logger.warning(f"RetryAfter on chat {chat_id}: waiting {wait_time}s (attempt {attempt+1}/3)")
                    await asyncio.sleep(wait_time)
                except Exception as e:
                    err = str(e).lower()
                    if "message is not modified" in err:
                        return True
                    if "there is no text" in err or "only edit the caption" in err:
                        # Message is a photo message in Telegram — edit its media/caption
                        try:
                            if media:
                                await bot.edit_message_media(
                                    chat_id=chat_id, message_id=message_id,
                                    media=InputMediaPhoto(media=media, caption=text, parse_mode=parse_mode),
                                    reply_markup=reply_markup,
                                )
                            else:
                                await bot.edit_message_caption(
                                    chat_id=chat_id, message_id=message_id,
                                    caption=text, reply_markup=reply_markup, parse_mode=parse_mode,
                                )
                            return True
                        except Exception:
                            return False
                    if "there is no caption" in err or "not a media message" in err:
                        try:
                            await bot.edit_message_text(
                                chat_id=chat_id, message_id=message_id,
                                text=href_text, reply_markup=reply_markup, parse_mode="HTML",
                                disable_web_page_preview=False
                            )
                            return True
                        except Exception:
                            return False
                    if "message to edit not found" in err or "message can" in err and "be edited" in err:
                        return False
                    if attempt == 2:
                        logger.warning(f"API call failed after 3 attempts on chat {chat_id}: {e}")
                        return False
                    await asyncio.sleep(1.0)
        except Exception as e:
            logger.error(f"Unexpected error in _run_api_call (chat {chat_id}): {e}")
            return False
        return False

    async def _recreate_message(
        self, match, bot, caption: str, reply_markup, media, parse_mode: str
    ) -> None:
        """Last-resort: delete stale message and send a fresh one."""
        try:
            await bot.delete_message(chat_id=match.chat_id, message_id=match.draft_message_id)
        except Exception:
            pass
        msg = None
        media_is_url = bool(media and str(media).startswith("http"))
        if media_is_url:
            href_text = f'<a href="{media}">&#8205;</a>' + caption
            try:
                msg = await bot.send_message(
                    chat_id=match.chat_id, text=href_text,
                    reply_markup=reply_markup, parse_mode="HTML",
                    disable_web_page_preview=False
                )
            except Exception:
                pass
        elif media:
            try:
                msg = await bot.send_photo(
                    chat_id=match.chat_id, photo=media, caption=caption,
                    reply_markup=reply_markup, parse_mode=parse_mode,
                )
            except Exception:
                pass
        if not msg:
            try:
                msg = await bot.send_message(
                    chat_id=match.chat_id, text=caption,
                    reply_markup=reply_markup, parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Failed to recreate draft message (all fallbacks exhausted): {e}")
                return
        if msg:
            match.draft_message_id = msg.message_id
            try:
                await bot.pin_chat_message(
                    chat_id=match.chat_id, message_id=msg.message_id,
                    disable_notification=True,
                )
                match.pinned_message_id = msg.message_id
            except Exception:
                pass
            await save_match_state(match)



# Global singleton used by all draft handlers
debouncer = MessageDebouncer(delay=0.5)



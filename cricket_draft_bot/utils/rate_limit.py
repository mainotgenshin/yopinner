import asyncio
import logging
from telegram import InputMediaPhoto
from telegram.error import RetryAfter
from game.state import save_match_state

logger = logging.getLogger(__name__)

class MessageDebouncer:
    """
    Batches rapid Telegram message edits into a single API call.

    KEY FIX vs old version:
      Old approach cancelled the previous task when a new update arrived.
      This caused a 'silent drop' bug: if both tasks were cancelled within
      the 0.5s debounce window (common under Telegram rate limits with
      30+ concurrent matches), NEITHER update ever reached Telegram —
      leaving the "Draw Player" button invisible and triggering the AFK forfeit.

    New approach:
      Running tasks are NEVER cancelled by newer updates. Instead, the
      latest pending state is stored in self._pending. When the running
      task wakes up after the debounce delay, it always delivers the
      NEWEST state, not a stale one. Only cancel_updates() (called when
      a match ends) actually cancels a running task.
    """
    def __init__(self, delay=1.2):
        # 1.2s instead of 0.5s: better absorption of spam clicks
        # and less pressure on Telegram rate limits with 30+ matches
        self.delay = delay
        self.tasks = {}       # key -> running asyncio.Task
        self.last_state = {}  # key -> last successfully delivered state (dedup)
        self._pending = {}    # key -> latest (caption, markup, send_media, target_state)

    def cancel_updates(self, chat_id: int, message_id: int):
        """Cancel any pending screen updates for this message (e.g. match ended)."""
        key = f"{chat_id}_{message_id}"
        task = self.tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
        self.last_state.pop(key, None)
        self._pending.pop(key, None)

    async def schedule_update(self, match, bot, caption, reply_markup, media=None, parse_mode="Markdown"):
        if not match.draft_message_id:
            return

        key = f"{match.chat_id}_{match.draft_message_id}"

        # Dedup: drop if identical to what's already shown on screen
        markup_dict = reply_markup.to_dict() if reply_markup else None
        target_state = {"text": caption, "media": media, "markup": markup_dict}

        if key in self.last_state and self.last_state[key] == target_state:
            logger.debug(f"Debouncer: Ignored duplicate UI update for {key}")
            return

        # Media optimization: skip re-uploading unchanged media (saves bandwidth + API quota)
        send_media = media
        if key in self.last_state and self.last_state[key].get("media") == media and media is not None:
            send_media = None  # Caption-only edit is much cheaper

        # Always overwrite with LATEST pending state — any running task will deliver this
        self._pending[key] = (caption, reply_markup, send_media, target_state)

        # If a task is already running for this key, it will pick up the latest state above
        if key in self.tasks and not self.tasks[key].done():
            return  # Don't create a new task — running task handles it

        # No running task — schedule one
        self.tasks[key] = asyncio.create_task(
            self._execute_update(key, match, bot, parse_mode)
        )

    async def _execute_update(self, key, match, bot, parse_mode):
        try:
            await asyncio.sleep(self.delay)

            # Loop: keep delivering as long as there are pending updates for this key.
            #
            # KEY FIX vs old one-shot design:
            #   The old code popped ONE pending state, ran the API call, then exited.
            #   If a new update arrived in self._pending[key] WHILE the API call was
            #   running (e.g. a player assigned a position while we were delivering
            #   the "drawn player card"), the task exited without delivering it.
            #   self._pending[key] was left populated but with no active task to
            #   consume it — the next player's "Draw Player" board was silently dropped,
            #   they never saw the button, and they got forfeited by the AFK timer.
            #
            # New behaviour:
            #   After each successful delivery the loop checks self._pending[key] again.
            #   If another update has queued up, it delivers that one too (with a short
            #   anti-rate-limit pause), and so on until the queue is drained.
            while key in self._pending:
                caption, reply_markup, send_media, target_state = self._pending.pop(key)

                success = await self._run_api_call(
                    bot, match.chat_id, match.draft_message_id,
                    caption, reply_markup, send_media, parse_mode
                )

                if success:
                    self.last_state[key] = target_state
                else:
                    logger.info(f"Debouncer: All API retries failed — recreating message for match {match.match_id}")
                    await self._recreate_message(match, bot, caption, reply_markup, target_state.get('media'), parse_mode)
                    if match.draft_message_id:
                        new_key = f"{match.chat_id}_{match.draft_message_id}"
                        self.last_state[new_key] = target_state
                        # If the message was recreated with a new ID, migrate any pending
                        # updates that accumulated under the old key to the new key and
                        # spawn a fresh task so they are not silently dropped.
                        if new_key != key and key in self._pending:
                            self._pending[new_key] = self._pending.pop(key)
                            if new_key not in self.tasks or self.tasks[new_key].done():
                                self.tasks[new_key] = asyncio.create_task(
                                    self._execute_update(new_key, match, bot, parse_mode)
                                )
                    return  # After recreate this key is stale; new task handles the rest

                # If another update arrived while we were running the API call,
                # pause briefly before the next iteration to respect rate limits.
                if key in self._pending:
                    await asyncio.sleep(0.3)

        except asyncio.CancelledError:
            pass  # Cancelled by cancel_updates() when match ends — expected and OK
        except Exception as e:
            logger.error(f"Debouncer execution error for {key}: {e}")
        finally:
            self.tasks.pop(key, None)

    async def _run_api_call(self, bot, chat_id, message_id, text, reply_markup, media, parse_mode):
        try:
            for attempt in range(3):
                try:
                    if media:
                        # Full layout replacement (photo + caption + buttons)
                        await bot.edit_message_media(
                            chat_id=chat_id,
                            message_id=message_id,
                            media=InputMediaPhoto(media=media, caption=text, parse_mode=parse_mode),
                            reply_markup=reply_markup
                        )
                    else:
                        # Caption-only update (lightweight)
                        await bot.edit_message_caption(
                            chat_id=chat_id,
                            message_id=message_id,
                            caption=text,
                            reply_markup=reply_markup,
                            parse_mode=parse_mode
                        )
                    return True

                except RetryAfter as e:
                    wait_time = e.retry_after + 1
                    logger.warning(f"Telegram rate limit hit — waiting {wait_time}s (attempt {attempt + 1}/3)")
                    await asyncio.sleep(wait_time)
                    continue

                except Exception as e:
                    err = str(e).lower()

                    if "message is not modified" in err:
                        return True  # Already up to date — counts as success

                    if "there is no caption" in err or "not a media message" in err:
                        # Message on screen is plain text, fall back to text edit
                        try:
                            await bot.edit_message_text(
                                chat_id=chat_id, message_id=message_id, text=text,
                                reply_markup=reply_markup, parse_mode=parse_mode
                            )
                            return True
                        except Exception:
                            return False

                    if attempt == 2:
                        return False
                    # Other transient errors — retry
        except Exception:
            return False
        return False

    async def _recreate_message(self, match, bot, caption, reply_markup, media, parse_mode):
        """
        Delete the stale message and send a fresh one.
        Falls back to plain text if photo send fails (e.g. bot lacks media permission).
        This is the final safety net — the player MUST get the button back.
        """
        try:
            await bot.delete_message(chat_id=match.chat_id, message_id=match.draft_message_id)
        except Exception:
            pass  # Already deleted or no permission — proceed to send new

        msg = None

        # Try photo first (preferred — matches original draft message style)
        if media:
            try:
                msg = await bot.send_photo(
                    chat_id=match.chat_id, photo=media, caption=caption,
                    reply_markup=reply_markup, parse_mode=parse_mode
                )
            except Exception:
                pass  # Fall through to text fallback

        # Fallback: plain text — always works, even without media permissions
        if not msg:
            try:
                msg = await bot.send_message(
                    chat_id=match.chat_id, text=caption,
                    reply_markup=reply_markup, parse_mode=parse_mode
                )
            except Exception as e:
                logger.error(f"Failed to recreate draft message (all fallbacks exhausted): {e}")
                return

        if msg:
            match.draft_message_id = msg.message_id
            # Re-pin the new message so the draft board stays visible
            try:
                await bot.pin_chat_message(
                    chat_id=match.chat_id,
                    message_id=msg.message_id,
                    disable_notification=True
                )
                match.pinned_message_id = msg.message_id
            except Exception:
                pass  # Not admin or not in a group — skip silently
            await save_match_state(match)


# Global instance
debouncer = MessageDebouncer(delay=1.2)

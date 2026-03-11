import asyncio
import logging
from telegram import InputMediaPhoto
from telegram.error import RetryAfter
from game.state import save_match_state

logger = logging.getLogger(__name__)

class MessageDebouncer:
    """
    Batches rapid Telegram message edits (e.g. during a draft) into a single API call.
    Includes state checking to skip duplicate updates and optimizes media edits.
    """
    def __init__(self, delay=1.2):
        self.delay = delay
        self.tasks = {}
        self.last_state = {}
    
    async def schedule_update(self, match, bot, caption, reply_markup, media=None, parse_mode="Markdown"):
        if not match.draft_message_id:
            return  # Should be handled synchronously on initial creation
            
        key = f"{match.chat_id}_{match.draft_message_id}"
        
        # State Checking (Optimization 2): Drop request if text, media, and markup are identical
        markup_dict = reply_markup.to_dict() if reply_markup else None
        target_state = {"text": caption, "media": media, "markup": markup_dict}
        
        if key in self.last_state and self.last_state[key] == target_state:
            logger.debug(f"Debouncer: Ignored duplicate UI update for {key}")
            return
            
        # Optimization 3: Determine if we actually need to re-upload media
        send_media = media
        if key in self.last_state and self.last_state[key].get("media") == media and media is not None:
            # Media is identical to what's already on screen. Only edit caption to save bandwidth.
            send_media = None 
            
        self.last_state[key] = target_state

        # Cancel any pending edit for this message
        if key in self.tasks:
            self.tasks[key].cancel()
            
        # Schedule the new batched edit
        self.tasks[key] = asyncio.create_task(
            self._execute_update(key, match, bot, caption, reply_markup, send_media, target_state, parse_mode)
        )
        
    async def _execute_update(self, key, match, bot, caption, reply_markup, send_media, target_state, parse_mode):
        try:
            await asyncio.sleep(self.delay)
            # Try to edit (Optimization 1: Debounced Execution)
            success = await self._run_api_call(bot, match.chat_id, match.draft_message_id, caption, reply_markup, send_media, parse_mode)
            
            # Fallback: If edit fails entirely (e.g., deleted message or type mismatch)
            if not success:
               logger.info(f"Debouncer recreating message for {match.match_id}")
               await self._recreate_message(match, bot, caption, reply_markup, target_state.get('media'), parse_mode)
               
        except asyncio.CancelledError:
            # Successfully cancelled by a newer spam click!
            pass
        except Exception as e:
            logger.error(f"Debouncer execution error for {key}: {e}")
        finally:
            if key in self.tasks:
                del self.tasks[key]
                
    async def _run_api_call(self, bot, chat_id, message_id, text, reply_markup, media, parse_mode):
        try:
            for attempt in range(3):
                try:
                    if media:
                        # Full layout replacement (Heavy)
                        await bot.edit_message_media(
                            chat_id=chat_id,
                            message_id=message_id,
                            media=InputMediaPhoto(media=media, caption=text, parse_mode=parse_mode),
                            reply_markup=reply_markup
                        )
                    else:
                        # Caption-only replacement (Lightweight)
                        await bot.edit_message_caption(
                            chat_id=chat_id,
                            message_id=message_id,
                            caption=text,
                            reply_markup=reply_markup,
                            parse_mode=parse_mode
                        )
                    return True
                except RetryAfter as e:
                    # Global Rate Limiter Fallback (Optimization 4)
                    wait_time = e.retry_after + 1
                    logger.warning(f"Telegram Limit Hit! Backing off for {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue
                except Exception as e:
                    err = str(e).lower()
                    if "message is not modified" in err:
                        return True
                        
                    if "there is no caption" in err or "not a media message" in err:
                         # The message on screen is pure text, fall back to pure text edit
                         try:
                             await bot.edit_message_text(
                                 chat_id=chat_id, message_id=message_id, text=text,
                                 reply_markup=reply_markup, parse_mode=parse_mode
                             )
                             return True
                         except:
                             return False
                    if attempt == 2: 
                        return False
        except Exception:
            return False
            
    async def _recreate_message(self, match, bot, caption, reply_markup, media, parse_mode):
        try:
            await bot.delete_message(chat_id=match.chat_id, message_id=match.draft_message_id)
        except: 
            pass
        
        try:
            if media:
                msg = await bot.send_photo(chat_id=match.chat_id, photo=media, caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
            else:
                msg = await bot.send_message(chat_id=match.chat_id, text=caption, reply_markup=reply_markup, parse_mode=parse_mode)
                
            match.draft_message_id = msg.message_id
            await save_match_state(match)
            # The next update will use the new message_id
        except Exception as e:
            logger.error(f"Failed to recreate draft message: {e}")

# Global instance
debouncer = MessageDebouncer(delay=1.2)

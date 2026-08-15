# helpers/downloader.py
import os
import asyncio
import re
import json
from contextlib import asynccontextmanager
from time import time
from datetime import datetime
from typing import Optional, Any, Tuple, Union
from pyrogram.errors import FloodWait, PeerIdInvalid, BadRequest
from helpers.files import get_download_path, cleanup_download, get_readable_file_size, get_readable_time
from helpers.msg import (
    get_file_name, 
    get_raw_text, 
    parse_chat_id_direct, 
    is_valid_telegram_url,
    getChatMsgID,
    getStoryChatMsgID,
    is_story_link,
    get_story_file_name,
    parse_chat_identifier
)
from helpers.chats import resolve_chat, preferred_chat_reference, chat_title as get_chat_title
from helpers.utils import (
    progress_for_pyrogram, 
    progressArgs, 
    send_media_with_retry, 
    is_supported_media,
    get_media_type,
    get_file_size,
    process_media_group,
    extract_media_info
)
from logger import get_logger
from database import Database
from config import Config

logger = get_logger(__name__)
db = Database()


@asynccontextmanager
async def _existing_download_slot():
    """No-op context used when a retry already owns the semaphore slot."""
    yield

class DownloadState:
    def __init__(self, owner_user_id=None):
        state_dir = os.path.join(Config.DATA_DIR, "download_states")
        os.makedirs(state_dir, exist_ok=True)
        state_name = f"{owner_user_id}.json" if owner_user_id is not None else "legacy.json"
        self.state_file = os.path.join(state_dir, state_name)
        self.current_chat_id = None
        self.current_media_index = 0
        self.total_media = 0
        self.downloaded = 0
        self.failed = 0
        self.skipped = 0
        self.media_list = []
        self.completed_ids = []
        self.start_time = None
        self.flood_wait_until = 0
    
    def save(self):
        """Save current state to file"""
        temporary_file = f"{self.state_file}.tmp"
        try:
            with open(temporary_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'current_chat_id': self.current_chat_id,
                    'current_media_index': self.current_media_index,
                    'total_media': self.total_media,
                    'downloaded': self.downloaded,
                    'failed': self.failed,
                    'skipped': self.skipped,
                    'media_list': self.media_list,
                    'completed_ids': self.completed_ids,
                    'start_time': self.start_time,
                    'flood_wait_until': self.flood_wait_until
                }, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary_file, self.state_file)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            try:
                if os.path.exists(temporary_file):
                    os.remove(temporary_file)
            except OSError:
                pass
    
    def load(self):
        """Load state from file"""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    self.current_chat_id = data.get('current_chat_id')
                    self.current_media_index = data.get('current_media_index', 0)
                    self.total_media = data.get('total_media', 0)
                    self.downloaded = data.get('downloaded', 0)
                    self.failed = data.get('failed', 0)
                    self.skipped = data.get('skipped', 0)
                    self.media_list = data.get('media_list', [])
                    self.completed_ids = data.get('completed_ids', [])
                    self.start_time = data.get('start_time')
                    self.flood_wait_until = data.get('flood_wait_until', 0)
                return True
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
        return False
    
    def clear(self):
        """Clear state file"""
        try:
            if os.path.exists(self.state_file):
                os.remove(self.state_file)
            temporary_file = f"{self.state_file}.tmp"
            if os.path.exists(temporary_file):
                os.remove(temporary_file)
        except Exception as e:
            logger.error(f"Failed to clear state: {e}")
    
    def is_completed(self, msg_id):
        """Check if a message ID is already completed"""
        return msg_id in self.completed_ids
    
    def mark_completed(self, msg_id):
        """Mark a message ID as completed"""
        if msg_id not in self.completed_ids:
            self.completed_ids.append(msg_id)
            self.downloaded += 1


class Downloader:
    def __init__(self, user_client, bot_client, download_semaphore,
                 owner_user_id=None, admin_chat_id=None):
        self.user = user_client
        self.bot = bot_client
        self.semaphore = download_semaphore
        self.running_tasks = set()
        self.user_me = None
        self.flood_wait_until = 0
        self.is_paused = False
        self.flood_wait_count = 0
        self.owner_user_id = owner_user_id
        self.admin_chat_id = admin_chat_id
        self.state = DownloadState(owner_user_id)
        self._resuming = False
        self._consecutive_floods = 0
        self._forward_chat_cache = {}  # chat_id -> (can_access, checked_at)

    def clear_state(self):
        self.state.clear()

    def _admin_caption(self, message, chat_message, caption):
        requester = message.from_user
        requester_name = " ".join(
            part for part in [requester.first_name, requester.last_name] if part
        ) or str(requester.id)
        username = f"@{requester.username}" if requester.username else "no username"
        source = getattr(chat_message.chat, "title", None) or getattr(
            chat_message.chat, "username", None
        ) or str(chat_message.chat.id)
        header = (
            f"👤 Requester: {requester_name} ({username}, `{requester.id}`)\n"
            f"📍 Source: {source} (`{chat_message.chat.id}`)\n"
            f"💬 Message: `{chat_message.id}`\n\n"
        )
        combined = header + (caption or "")
        return combined[:1024]

    async def _mirror_media_to_admin(self, message, chat_message, media_path,
                                     media_type, caption, caption_entities=None):
        if not self.admin_chat_id or self.owner_user_id == self.admin_chat_id:
            return
        try:
            await self._forward_media(
                self.admin_chat_id, media_path, media_type,
                self._admin_caption(message, chat_message, caption), None,
            )
        except Exception as exc:
            logger.error("Admin mirror failed for user %s: %s", self.owner_user_id, exc)
    
    def _extract_wait_time(self, error_message: str) -> Optional[int]:
        patterns = [
            r'wait of (\d+) seconds',
            r'FLOOD_WAIT_X.*?(\d+)',
            r'(\d+) seconds is required',
            r'wait of (\d+)',
            r'A wait of (\d+) seconds',
        ]
        for pattern in patterns:
            match = re.search(pattern, error_message, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None
    
    async def _handle_flood_wait(self, wait_time: int, status_msg=None, context=""):
        """Handle FloodWait by pausing and waiting the full time."""
        self.flood_wait_until = time() + wait_time + 2
        self.is_paused = True
        self.flood_wait_count += 1
        self._consecutive_floods += 1
        
        self.state.flood_wait_until = self.flood_wait_until
        self.state.save()
        
        logger.warning(f"🚨 FLOOD WAIT #{self.flood_wait_count}: {wait_time:,} seconds (~{wait_time/60:.1f} min) - {context}")
        logger.info(f"📌 State saved - will resume from index {self.state.current_media_index} after wait")
        
        if status_msg:
            try:
                await status_msg.edit(
                    f"⏸️ **RATE LIMITED - PAUSED**\n\n"
                    f"**Wait Required:** {wait_time:,} seconds (~{wait_time/60:.1f} minutes)\n"
                    f"**Context:** {context}\n"
                    f"**FloodWait #:** {self.flood_wait_count}\n"
                    f"**Resume from:** {self.state.current_media_index:,}/{self.state.total_media:,}\n\n"
                    f"📌 State saved - will resume exactly where we left off\n"
                    f"⏳ Waiting...\n\n"
                    f"**Countdown:** {wait_time:,}s remaining"
                )
            except Exception as e:
                logger.error(f"Failed to update status message: {e}")
        
        remaining = wait_time
        while remaining > 0:
            if remaining % 10 == 0 or remaining < 10:
                if status_msg:
                    try:
                        progress = int((wait_time - remaining) / wait_time * 20)
                        bar = "█" * progress + "░" * (20 - progress)
                        await status_msg.edit(
                            f"⏸️ **RATE LIMITED - PAUSED**\n\n"
                            f"```{bar} {int((wait_time - remaining) / wait_time * 100)}%```\n"
                            f"**Time Remaining:** {remaining:,} seconds\n"
                            f"**Total Wait:** {wait_time:,} seconds\n"
                            f"**Resume from:** {self.state.current_media_index:,}/{self.state.total_media:,}\n\n"
                            f"⏳ Please wait. Bot will auto-resume."
                        )
                    except Exception as e:
                        logger.error(f"Failed to update status message: {e}")
            await asyncio.sleep(1)
            remaining -= 1
            self.flood_wait_until = time() + remaining + 2
        
        self.flood_wait_until = 0
        self.is_paused = False
        self._consecutive_floods = 0
        
        if status_msg:
            try:
                await status_msg.edit(
                    f"▶️ **RESUMING**\n\n"
                    f"Context: {context}\n"
                    f"Total Wait: {wait_time:,} seconds\n"
                    f"Resuming from: {self.state.current_media_index:,}/{self.state.total_media:,}"
                )
            except Exception as e:
                logger.error(f"Failed to update status message: {e}")
            await asyncio.sleep(1)
        
        logger.info(f"✅ FloodWait #{self.flood_wait_count} over. Resuming from index {self.state.current_media_index}")
        return True
    
    async def _wait_if_paused(self, status_msg=None):
        if self.is_paused or self.flood_wait_until > time():
            wait_time = int(self.flood_wait_until - time())
            if wait_time > 0:
                if status_msg:
                    try:
                        await status_msg.edit(
                            f"⏸️ **PAUSED**\n\n"
                            f"Remaining: {wait_time:,} seconds\n"
                            f"⏳ Please wait. Bot will auto-resume."
                        )
                    except Exception as e:
                        logger.error(f"Failed to update status message: {e}")
                await asyncio.sleep(wait_time + 1)
                self.flood_wait_until = 0
                self.is_paused = False
                return True
        return False

    async def _refresh_message(self, chat_id, msg_id):
        """Refresh a message to get a valid file reference"""
        try:
            fresh_msg = await self.user.get_messages(chat_id=chat_id, message_ids=msg_id)
            if fresh_msg and fresh_msg.media:
                logger.debug(f"🔄 Refreshed message {msg_id}")
                return fresh_msg
            return None
        except Exception as e:
            logger.warning(f"Could not refresh message {msg_id}: {e}")
            return None

    async def _send_channel_header(self, forward_chat_id, chat_title, chat_id, total_media):
        """Send a channel header message before starting to forward content."""
        try:
            header_text = (
                f"📢 **{'='*40}**\n"
                f"📌 **CHANNEL: {chat_title}**\n"
                f"🆔 **ID:** `{chat_id}`\n"
                f"📊 **Total Media:** {total_media:,}\n"
                f"📅 **Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"📢 **{'='*40}**\n\n"
                f"⬇️ **Starting download/forward...**"
            )
            
            await self.bot.send_message(
                chat_id=forward_chat_id,
                text=header_text,
                disable_web_page_preview=True
            )
            
            logger.info(f"📌 Sent channel header for {chat_title} to {forward_chat_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send channel header: {e}")
            return False

    async def _send_channel_footer(self, forward_chat_id, chat_title, downloaded, failed, skipped, elapsed_time):
        """Send a channel footer message after finishing the channel content."""
        try:
            footer_text = (
                f"\n📢 **{'='*40}**\n"
                f"✅ **COMPLETED: {chat_title}**\n"
                f"📥 **Downloaded:** {downloaded:,}\n"
                f"❌ **Failed:** {failed:,}\n"
                f"⏭️ **Skipped:** {skipped:,}\n"
                f"⏱️ **Time:** {get_readable_time(elapsed_time)}\n"
                f"📢 **{'='*40}**"
            )
            
            await self.bot.send_message(
                chat_id=forward_chat_id,
                text=footer_text
            )
            
            logger.info(f"📌 Sent channel footer for {chat_title} to {forward_chat_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send channel footer: {e}")
            return False

    # ============ USER FORWARD CHAT METHODS ============

    async def get_user_forward_chat(self, user_id):
        """Get the user's configured forward chat"""
        try:
            settings = db.get_user_settings(user_id)
            if settings and settings.forward_chat_id:
                return {
                    'chat_id': settings.forward_chat_id,
                    'title': settings.forward_chat_title or settings.forward_chat_id
                }
            return None
        except Exception as e:
            logger.error(f"Failed to get user forward chat: {e}")
            return None

    async def _check_forward_chat_access(self, forward_chat_id):
        """Check if bot can access the forward chat"""
        if not forward_chat_id:
            return False
        
        # Check cache
        cached = self._forward_chat_cache.get(forward_chat_id)
        if cached:
            can_access, checked_at = cached
            ttl = 300 if can_access else 30
            if time() - checked_at < ttl:
                return can_access
            self._forward_chat_cache.pop(forward_chat_id, None)
        
        try:
            # Try to send a test message
            test_msg = await self.bot.send_message(
                chat_id=forward_chat_id,
                text="🔍 **Permission Check**\n\nBot is active and has permission to forward.",
                disable_web_page_preview=True
            )
            await test_msg.delete()
            self._forward_chat_cache[forward_chat_id] = (True, time())
            logger.info(f"✅ Forward chat {forward_chat_id} is accessible")
            return True
        except FloodWait as e:
            wait_time = e.value if hasattr(e, 'value') else 10
            logger.warning(f"⏳ FloodWait on forward check: {wait_time}s")
            await asyncio.sleep(wait_time + 1)
            self._forward_chat_cache[forward_chat_id] = (False, time())
            return False
        except Exception as e:
            logger.error(f"❌ Cannot access forward chat {forward_chat_id}: {e}")
            self._forward_chat_cache[forward_chat_id] = (False, time())
            return False

    async def _download_safe(self, download_func, status_msg, context, msg_id, chat_id,
                             *args, track_resume=False, **kwargs):
        """Safe download with FloodWait handling and file reference refresh."""
        await self._wait_if_paused(status_msg)
        
        if track_resume and self.state.is_completed(msg_id):
            logger.info(f"📌 Message {msg_id} already downloaded, skipping")
            return "already_downloaded"
        
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                result = await download_func(*args, **kwargs)
                
                if result and os.path.exists(result):
                    file_size = os.path.getsize(result)
                    if file_size == 0:
                        logger.warning(f"⚠️ File is 0 bytes - skipping")
                        cleanup_download(result)
                        return None
                    
                    if track_resume:
                        self.state.mark_completed(msg_id)
                        self.state.current_media_index += 1
                        self.state.save()
                        logger.info(f"✅ Downloaded and marked completed: {msg_id}")
                    
                    return result
                return None
                
            except FloodWait as e:
                error_msg = str(e)
                wait_time = self._extract_wait_time(error_msg)
                if wait_time is None:
                    wait_time = e.value if hasattr(e, 'value') else 30
                
                logger.warning(f"🔥 FLOOD WAIT CAUGHT: {wait_time}s - {context}")
                await self._handle_flood_wait(wait_time, status_msg, context)
                continue
                
            except Exception as e:
                error_msg = str(e)
                
                if "FILE_REFERENCE_EXPIRED" in error_msg:
                    logger.warning(f"🔄 File reference expired for {msg_id} (attempt {attempt+1}/{max_attempts})")
                    
                    if attempt < max_attempts - 1:
                        fresh_msg = await self._refresh_message(chat_id, msg_id)
                        if fresh_msg and fresh_msg.media:
                            logger.info(f"✅ Refreshed message {msg_id}, retrying download...")
                            download_func = fresh_msg.download
                            await asyncio.sleep(2)
                            continue
                        else:
                            logger.warning(f"❌ Could not refresh message {msg_id}")
                            return None
                    else:
                        logger.error(f"❌ Max attempts reached for {msg_id}")
                        return None
                
                if "FLOOD_WAIT" in error_msg:
                    wait_time = self._extract_wait_time(error_msg)
                    if wait_time is None:
                        wait_time = 60
                    await self._handle_flood_wait(wait_time, status_msg, context)
                    continue
                
                raise
        
        return None

    async def _parse_input(self, url_or_id: str) -> Tuple[Union[int, str], Optional[int]]:
        """Parse input - supports both URLs and chat IDs"""
        try:
            return parse_chat_id_direct(url_or_id)
                
        except Exception as e:
            raise ValueError(f"Invalid input format: {e}")

    async def _check_channel_access(self, chat_id: Union[int, str], message) -> bool:
        """Check if user can access the channel"""
        if self.user is None:
            await message.reply("❌ **User client not initialized**")
            return False
        
        try:
            chat = await resolve_chat(self.user, chat_id)
            logger.info(f"✅ Accessing chat: {get_chat_title(chat)}")
            return True
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Cannot access chat {chat_id}: {error_msg}")
            await message.reply(
                f"❌ **Cannot Access Channel**\n\n"
                f"**Chat ID:** `{chat_id}`\n"
                f"**Error:** `{error_msg}`\n\n"
                f"**Solutions:**\n"
                f"1. Make sure your account is a member of this channel\n"
                f"2. Check if the chat ID is correct\n"
                f"3. Try the full URL instead of just the ID"
            )
            return False

    # ============ MAIN DOWNLOAD METHODS ============

    async def download_media(self, message, url_or_id, forward_chat_id=None,
                             _semaphore_acquired=False):
        """Main download function with forward chat support."""
        slot = _existing_download_slot() if _semaphore_acquired else self.semaphore
        async with slot:
            try:
                await self._wait_if_paused()
                
                # Check if user has a custom forward chat
                user_forward = await self.get_user_forward_chat(message.from_user.id)
                if user_forward:
                    effective_forward_chat_id = user_forward['chat_id']
                    logger.info(f"Using user's custom forward chat: {user_forward['title']}")
                else:
                    effective_forward_chat_id = forward_chat_id
                
                # Validate forward chat if provided
                if effective_forward_chat_id:
                    has_access = await self._check_forward_chat_access(effective_forward_chat_id)
                    if not has_access:
                        logger.warning(f"❌ Forward chat {effective_forward_chat_id} not accessible, disabling forwarding")
                        effective_forward_chat_id = None
                
                # Make sure user client is available
                if self.user is None:
                    await message.reply("❌ **User client not initialized**")
                    return
                
                if not self.user_me:
                    try:
                        self.user_me = await self.user.get_me()
                        logger.info(f"User client: {self.user_me.first_name} (@{self.user_me.username})")
                    except Exception as e:
                        logger.error(f"Failed to get user info: {e}")
                        await message.reply("❌ **Failed to get user info**")
                        return
                
                chat_id, message_id = await self._parse_input(url_or_id)
                story_request = is_story_link(url_or_id)
                
                # Try to access the channel with the user client
                try:
                    chat = await resolve_chat(self.user, chat_id)
                    chat_id = preferred_chat_reference(chat)
                    logger.info(f"✅ Accessing chat: {get_chat_title(chat)}")
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"Cannot access chat {chat_id}: {error_msg}")
                    await message.reply(
                        f"❌ **Cannot Access Channel**\n\n"
                        f"**Chat ID:** `{chat_id}`\n"
                        f"**Error:** `{error_msg}`\n\n"
                        f"**Make sure your account is a member of this channel.**"
                    )
                    return
                
                if message_id is None:
                    message_id = await self._get_latest_message_id(chat_id)
                    if not message_id:
                        await message.reply("❌ **No messages found**")
                        return

                if story_request:
                    story = await self.user.get_stories(chat_id, story_ids=message_id)
                    if isinstance(story, list):
                        story = story[0] if story else None
                    if not story:
                        await message.reply("❌ **Story not found or expired**")
                        return
                    return await self._handle_story(story, message, effective_forward_chat_id)
                
                chat_message = await self._get_message(chat_id, message_id)
                if not chat_message:
                    await message.reply("❌ **Message not found**")
                    return
                
                if hasattr(chat_message, 'story') and chat_message.story:
                    return await self._handle_story(chat_message, message, effective_forward_chat_id)
                
                if chat_message.media_group_id:
                    return await self._handle_media_group(chat_message, message, effective_forward_chat_id)
                
                if not is_supported_media(chat_message):
                    return await self._handle_text_only(chat_message, message, effective_forward_chat_id)
                
                return await self._download_and_send(chat_message, message, effective_forward_chat_id)
                
            except FloodWait as e:
                error_msg = str(e)
                wait_time = self._extract_wait_time(error_msg)
                if wait_time is None:
                    wait_time = e.value if hasattr(e, 'value') else 30
                await self._handle_flood_wait(wait_time, None, "Main download")
                return await self.download_media(
                    message, url_or_id, forward_chat_id, _semaphore_acquired=True
                )
                
            except Exception as e:
                logger.error(f"Download error: {e}")
                await message.reply(f"❌ **Error:** {str(e)[:200]}")

    async def _download_and_send(self, chat_message, message, forward_chat_id=None):
        """Download and send a single file."""
        start_time = time()
        progress_message = await message.reply("📥 **Starting download...**")
        
        await self._wait_if_paused(progress_message)
        
        try:
            filename = get_file_name(chat_message.id, chat_message)
            download_path = get_download_path(f"{self.owner_user_id}_{message.id}", filename)
            
            logger.info(f"Downloading: {filename}")
            
            media_path = await self._download_safe(
                chat_message.download,
                progress_message,
                "Download",
                chat_message.id,
                chat_message.chat.id,
                file_name=download_path,
                progress=progress_for_pyrogram,
                progress_args=progressArgs(
                    "📥 **Downloading...**", progress_message, start_time
                )
            )
            
            if media_path == "already_downloaded":
                await progress_message.edit(f"✅ **Already downloaded:** {filename}")
                await progress_message.delete()
                return
            
            if not media_path or not os.path.exists(media_path):
                await progress_message.edit("❌ **Download failed**")
                return
            
            file_size = os.path.getsize(media_path)
            if file_size == 0:
                cleanup_download(media_path)
                await progress_message.edit("❌ **Downloaded file is empty**")
                return
            
            logger.info(f"✅ Downloaded: {filename} ({get_readable_file_size(file_size)})")
            
            caption, caption_entities = get_raw_text(
                chat_message.caption, chat_message.caption_entities
            )
            if caption:
                caption = f"{caption}\n\n📁 **File:** {filename}\n📦 **Size:** {get_readable_file_size(file_size)}"
            else:
                caption = f"📁 **File:** {filename}\n📦 **Size:** {get_readable_file_size(file_size)}"
            
            media_type = get_media_type(chat_message) or "document"
            
            if forward_chat_id:
                try:
                    chat_title = chat_message.chat.title if hasattr(chat_message.chat, 'title') else chat_message.chat.id
                    caption_with_channel = f"📌 **{chat_title}**\n\n{caption}"
                    await self._forward_media(forward_chat_id, media_path, media_type, caption_with_channel, caption_entities)
                    await progress_message.edit(f"✅ **Forwarded:** {filename}")
                except FloodWait as e:
                    wait_time = e.value if hasattr(e, 'value') else 30
                    logger.warning(f"⏳ FloodWait on forward: {wait_time}s")
                    await asyncio.sleep(wait_time + 2)
                    # Retry once
                    chat_title = chat_message.chat.title if hasattr(chat_message.chat, 'title') else chat_message.chat.id
                    caption_with_channel = f"📌 **{chat_title}**\n\n{caption}"
                    await self._forward_media(forward_chat_id, media_path, media_type, caption_with_channel, caption_entities)
                    await progress_message.edit(f"✅ **Forwarded:** {filename}")
                except Exception as e:
                    await progress_message.edit(f"❌ **Failed to forward:** {str(e)[:100]}")
            else:
                await send_media_with_retry(
                    self.bot, message, media_path, media_type,
                    caption, caption_entities,
                    progress_message, start_time,
                    None
                )

            if self.admin_chat_id != forward_chat_id:
                await self._mirror_media_to_admin(
                    message, chat_message, media_path, media_type, caption, caption_entities
                )
            
            db.add_download_record(
                user_id=message.from_user.id,
                chat_id=str(chat_message.chat.id),
                message_id=chat_message.id,
                file_name=filename,
                file_size=file_size,
                media_type=media_type,
                success=True,
                url=message.text
            )
            
            cleanup_download(media_path)
            await progress_message.delete()
            
        except FloodWait as e:
            error_msg = str(e)
            wait_time = self._extract_wait_time(error_msg)
            if wait_time is None:
                wait_time = e.value if hasattr(e, 'value') else 30
            await self._handle_flood_wait(wait_time, progress_message, "Download and send")
            await self._download_and_send(chat_message, message, forward_chat_id)
            
        except Exception as e:
            logger.error(f"❌ Download failed: {e}")
            await progress_message.edit(f"❌ **Download failed:** {str(e)[:100]}")
            db.add_download_record(
                user_id=message.from_user.id,
                chat_id=str(chat_message.chat.id),
                message_id=chat_message.id,
                success=False,
                error_message=str(e)
            )

    async def _get_latest_message_id(self, chat_id: Union[int, str]) -> Optional[int]:
        """Get the latest message ID from a chat"""
        if self.user is None:
            logger.error("User client is None")
            return None
        try:
            async for msg in self.user.get_chat_history(chat_id, limit=10):
                if msg.media or msg.text or msg.caption:
                    return msg.id
            return None
        except Exception as e:
            logger.error(f"Error getting latest message: {e}")
            return None

    async def _get_message(self, chat_id: Union[int, str], message_id: int):
        """Get message from chat"""
        if self.user is None:
            logger.error("User client is None")
            return None
        try:
            return await self.user.get_messages(chat_id=chat_id, message_ids=message_id)
        except Exception as e:
            logger.error(f"Error getting message {message_id}: {e}")
            return None

    async def _download_all_with_resume(self, message, chat_id, chat_title, media_messages, forward_chat_id, total_media, downloaded_count, failed_count, skipped_count, completed_ids):
        """Download all media with resume capability and channel headers/footers."""
        total_media = total_media
        status_msg = None
        header_sent = False
        
        # Validate forward chat first
        if forward_chat_id:
            has_access = await self._check_forward_chat_access(forward_chat_id)
            if not has_access:
                logger.warning(f"❌ Forward chat {forward_chat_id} not accessible, disabling forwarding")
                forward_chat_id = None
        
        try:
            if forward_chat_id and not self._resuming:
                await self._send_channel_header(
                    forward_chat_id, 
                    chat_title, 
                    chat_id, 
                    total_media
                )
                header_sent = True
                await asyncio.sleep(1)
            
            if forward_chat_id and self._resuming and downloaded_count > 0:
                resume_header = (
                    f"📢 **{'='*40}**\n"
                    f"🔄 **RESUMING: {chat_title}**\n"
                    f"📥 **Already Downloaded:** {downloaded_count:,}/{total_media:,}\n"
                    f"📢 **{'='*40}**\n\n"
                    f"⏳ Continuing from where we left off..."
                )
                try:
                    await self.bot.send_message(
                        chat_id=forward_chat_id,
                        text=resume_header
                    )
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Failed to send resume header: {e}")
                    forward_chat_id = None  # Disable forwarding if header fails
            
            status_msg = await message.reply(
                f"📥 **Downloading {total_media:,} media items from {chat_title}**\n\n"
                f"📌 Resuming from index {downloaded_count}/{total_media}\n"
                f"⏱️ This may take a while. Use `/stopdownload` to cancel.\n\n"
                f"📌 **State saved automatically** - Resume after any interruption!"
            )
            
            downloaded = downloaded_count
            failed = failed_count
            skipped = skipped_count
            
            for idx, msg in enumerate(media_messages, downloaded_count + 1):
                try:
                    await self._wait_if_paused(status_msg)
                    
                    if idx % 5 == 0:
                        elapsed = time() - self.state.start_time
                        speed = idx / elapsed if elapsed > 0 else 0
                        await status_msg.edit(
                            f"📥 **Downloading {chat_title}**\n\n"
                            f"**Progress:** {idx:,} / {total_media:,}\n"
                            f"**Downloaded:** {downloaded:,}\n"
                            f"**Failed:** {failed:,}\n"
                            f"**Skipped:** {skipped:,}\n"
                            f"**Speed:** ~{speed:.1f} items/s"
                        )
                    
                    filename = get_file_name(msg.id, msg)
                    download_path = get_download_path(f"{self.owner_user_id}_{message.id}", filename)
                    
                    fresh_msg = await self._refresh_message(chat_id, msg.id)
                    if fresh_msg and fresh_msg.media:
                        download_msg = fresh_msg
                        logger.debug(f"🔄 Using refreshed message for {msg.id}")
                    else:
                        download_msg = msg
                    
                    media_path = await self._download_safe(
                        download_msg.download,
                        status_msg,
                        f"Download {idx}/{total_media}",
                        msg.id,
                        chat_id,
                        track_resume=True,
                        file_name=download_path,
                        progress=progress_for_pyrogram,
                        progress_args=progressArgs(
                            f"📥 **Downloading {filename}**", status_msg, time()
                        )
                    )
                    
                    if media_path == "already_downloaded":
                        downloaded += 1
                        continue
                    
                    if media_path and os.path.exists(media_path):
                        file_size = os.path.getsize(media_path)
                        
                        if file_size > 0:
                            caption, caption_entities = get_raw_text(
                                download_msg.caption, download_msg.caption_entities
                            )
                            if caption:
                                caption = f"{caption}\n\n📁 **File:** {filename}\n📦 **Size:** {get_readable_file_size(file_size)}"
                            else:
                                caption = f"📁 **File:** {filename}\n📦 **Size:** {get_readable_file_size(file_size)}"
                            
                            media_type = get_media_type(download_msg) or "document"
                            
                            if forward_chat_id:
                                try:
                                    caption_with_channel = f"📌 **{chat_title}**\n\n{caption}"
                                    await self._forward_media(forward_chat_id, media_path, media_type, caption_with_channel, caption_entities)
                                    downloaded += 1
                                except FloodWait as e:
                                    wait_time = e.value if hasattr(e, 'value') else 30
                                    logger.warning(f"⏳ FloodWait on forward: {wait_time}s, retrying...")
                                    await asyncio.sleep(wait_time + 2)
                                    try:
                                        caption_with_channel = f"📌 **{chat_title}**\n\n{caption}"
                                        await self._forward_media(forward_chat_id, media_path, media_type, caption_with_channel, caption_entities)
                                        downloaded += 1
                                    except Exception as e2:
                                        logger.error(f"Failed to forward after retry: {e2}")
                                        failed += 1
                                except Exception as e:
                                    logger.error(f"Failed to forward: {e}")
                                    failed += 1
                            else:
                                await send_media_with_retry(
                                    self.bot, message, media_path, media_type,
                                    caption, caption_entities,
                                    status_msg, time(),
                                    None
                                )
                                downloaded += 1

                            if self.admin_chat_id != forward_chat_id:
                                await self._mirror_media_to_admin(
                                    message, download_msg, media_path, media_type,
                                    caption, caption_entities
                                )
                            
                            cleanup_download(media_path)
                            logger.info(f"✅ Downloaded {idx}/{total_media}: {filename}")
                        else:
                            failed += 1
                    else:
                        failed += 1
                    
                    self.state.downloaded = downloaded
                    self.state.failed = failed
                    self.state.skipped = skipped
                    self.state.current_media_index = idx
                    self.state.save()
                    
                except FloodWait as e:
                    error_msg = str(e)
                    wait_time = self._extract_wait_time(error_msg)
                    if wait_time is None:
                        wait_time = e.value if hasattr(e, 'value') else 30
                    
                    self.state.current_media_index = idx
                    self.state.save()
                    await self._handle_flood_wait(wait_time, status_msg, f"Batch {idx}/{total_media}")
                    continue
                    
                except Exception as e:
                    error_msg = str(e)
                    if "FILE_REFERENCE_EXPIRED" in error_msg:
                        logger.warning(f"🔄 File reference expired for {msg.id}, refreshing...")
                        try:
                            fresh_msg = await self._refresh_message(chat_id, msg.id)
                            if fresh_msg and fresh_msg.media:
                                logger.info(f"✅ Refreshed message {msg.id}, retrying...")
                                media_path = await self._download_safe(
                                    fresh_msg.download,
                                    status_msg,
                                    f"Download {idx}/{total_media} (refresh)",
                                    msg.id,
                                    chat_id,
                                    track_resume=True,
                                    file_name=download_path,
                                    progress=progress_for_pyrogram,
                                    progress_args=progressArgs(
                                        f"📥 **Downloading {filename}**", status_msg, time()
                                    )
                                )
                                if media_path and media_path != "already_downloaded" and os.path.exists(media_path):
                                    file_size = os.path.getsize(media_path)
                                    if file_size > 0:
                                        downloaded += 1
                                        cleanup_download(media_path)
                                    else:
                                        failed += 1
                                elif media_path == "already_downloaded":
                                    downloaded += 1
                                else:
                                    failed += 1
                            else:
                                failed += 1
                        except Exception as refresh_error:
                            logger.error(f"❌ Refresh failed: {refresh_error}")
                            failed += 1
                    else:
                        logger.error(f"Failed to download {msg.id}: {e}")
                        failed += 1
                
                await asyncio.sleep(2)
            
            self.state.clear()
            elapsed = time() - self.state.start_time
            
            if forward_chat_id:
                await self._send_channel_footer(
                    forward_chat_id,
                    chat_title,
                    downloaded,
                    failed,
                    skipped,
                    elapsed
                )
            
            await status_msg.edit(
                f"✅ **Download Complete!**\n\n"
                f"**Channel:** {chat_title}\n"
                f"**Total Media:** {total_media:,}\n"
                f"**Downloaded:** {downloaded:,}\n"
                f"**Failed:** {failed:,}\n"
                f"**Skipped:** {skipped:,}\n\n"
                f"⏱️ **Time:** {get_readable_time(elapsed)}"
            )
            logger.info(f"✅ Download all complete for {chat_title}: {downloaded} downloaded, {failed} failed")
            
        except Exception as e:
            logger.error(f"Download all failed: {e}")
            if status_msg:
                await status_msg.edit(f"❌ **Download failed:** {str(e)[:200]}")

    async def download_all_channel_media_optimized(self, message, chat_id, forward_chat_id=None):
        """Download all media from a channel with proper flood wait handling and resume."""
        try:
            parsed_chat_id = parse_chat_identifier(chat_id)
            chat = await resolve_chat(self.user, parsed_chat_id)
            parsed_chat_id = preferred_chat_reference(chat)
            resolved_title = get_chat_title(chat)
            
            logger.info(f"📥 Starting download all for chat: {resolved_title} ({parsed_chat_id})")
            
            status_msg = await message.reply(f"📊 **Scanning {resolved_title} for media...**")
            
            start_index = 0
            downloaded_count = 0
            failed_count = 0
            skipped_count = 0
            completed_ids = []
            total_media_from_state = 0
            
            if self.state.load():
                if self.state.current_chat_id == parsed_chat_id:
                    start_index = self.state.current_media_index
                    downloaded_count = self.state.downloaded
                    failed_count = self.state.failed
                    skipped_count = self.state.skipped
                    completed_ids = self.state.completed_ids
                    total_media_from_state = self.state.total_media
                    logger.info(f"📌 Found saved state: index {start_index}, downloaded: {downloaded_count}, completed: {len(completed_ids)}")
                    
                    if self.state.flood_wait_until > time():
                        wait_time = int(self.state.flood_wait_until - time())
                        logger.info(f"⏳ Waiting {wait_time}s for flood wait to clear...")
                        await status_msg.edit(f"⏸️ **Resuming from saved state**\n\n⏳ Waiting {wait_time}s...")
                        await asyncio.sleep(wait_time + 1)
                        self.state.flood_wait_until = 0
            else:
                self.state.clear()
                start_index = 0
                downloaded_count = 0
                failed_count = 0
                skipped_count = 0
                completed_ids = []
            
            media_messages = []
            media_count = 0
            skipped_already_completed = 0
            
            async for msg in self.user.get_chat_history(parsed_chat_id):
                if msg.media:
                    media_count += 1
                    
                    if start_index > 0 and media_count <= start_index:
                        continue
                    
                    if msg.id in completed_ids:
                        skipped_already_completed += 1
                        continue
                    
                    media_messages.append(msg)
                
                if msg.media and media_count % 100 == 0:
                    try:
                        await status_msg.edit(
                            f"📊 **Scanning {resolved_title}**\n"
                            f"Found {media_count:,} media items...\n"
                            f"Skipped already completed: {skipped_already_completed}"
                        )
                    except Exception as e:
                        logger.error(f"Failed to update status: {e}")
            
            total_media = len(media_messages) + start_index + skipped_already_completed
            
            if total_media_from_state > 0 and total_media_from_state >= total_media:
                total_media = total_media_from_state
            
            if len(media_messages) == 0:
                if start_index > 0 or skipped_already_completed > 0:
                    await status_msg.edit(
                        f"✅ **Download Complete!**\n\n"
                        f"**Channel:** {resolved_title}\n"
                        f"**Total Media:** {total_media:,}\n"
                        f"**Downloaded:** {downloaded_count:,}\n"
                        f"**Failed:** {failed_count:,}\n"
                        f"**Skipped:** {skipped_count:,}\n\n"
                        f"📌 All media has been processed!"
                    )
                    self.state.clear()
                    return
                else:
                    await status_msg.edit(f"❌ No media found in {resolved_title}")
                    self.state.clear()
                    return
            
            if start_index > 0 or downloaded_count > 0:
                await status_msg.edit(
                    f"📌 **Resuming download**\n\n"
                    f"**Channel:** {resolved_title}\n"
                    f"**Total Media:** {total_media:,}\n"
                    f"**Already Downloaded:** {downloaded_count:,}\n"
                    f"**Remaining:** {len(media_messages):,}\n"
                    f"**Completed IDs:** {len(completed_ids):,}\n\n"
                    f"⏳ Continuing from where we left off..."
                )
                await asyncio.sleep(2)
            else:
                self.state.clear()
                self.state.current_chat_id = parsed_chat_id
                self.state.total_media = total_media
                self.state.start_time = time()
                self.state.downloaded = 0
                self.state.failed = 0
                self.state.skipped = 0
                self.state.completed_ids = []
                self.state.current_media_index = 0
                self.state.save()
            
            await self._download_all_with_resume(
                message,
                parsed_chat_id,
                resolved_title,
                media_messages,
                forward_chat_id,
                total_media,
                downloaded_count,
                failed_count,
                skipped_count,
                completed_ids
            )
            
        except Exception as e:
            logger.error(f"Download all failed: {e}")
            await message.reply(f"❌ **Download all failed:** {str(e)[:200]}")

    # ============ HELPER METHODS ============

    async def _handle_story(self, chat_message, message, forward_chat_id=None):
        try:
            await self._wait_if_paused()
            
            story = getattr(chat_message, "story", None) or chat_message
            if not story:
                await message.reply("❌ **Story not found**")
                return
            
            if not (story.photo or story.video):
                await message.reply("**This story has no downloadable media.**")
                return
            
            raw_caption, raw_caption_entities = get_raw_text(
                story.caption, story.caption_entities
            )
            
            start_time = time()
            progress_message = await message.reply("📥 **Downloading Story...**")
            
            story_chat = getattr(story, "chat", None) or getattr(chat_message, "chat", None)
            username = getattr(story_chat, "username", None) or str(getattr(story_chat, "id", "story"))
            filename = get_story_file_name(story.id, story, username)
            download_path = get_download_path(f"{self.owner_user_id}_{message.id}", filename)
            
            download_method = getattr(story, "download", None)
            if not download_method:
                async def download_method(**kwargs):
                    return await self.user.download_media(story, **kwargs)

            media_path = await self._download_safe(
                download_method,
                progress_message,
                "Story Download",
                story.id,
                story_chat.id,
                file_name=download_path,
                progress=progress_for_pyrogram,
                progress_args=progressArgs(
                    "📥 **Downloading Story...**", progress_message, start_time
                )
            )
            
            if media_path == "already_downloaded":
                await progress_message.edit("✅ **Story already downloaded**")
                await progress_message.delete()
                return
            
            if not media_path or not os.path.exists(media_path):
                await progress_message.edit("❌ **Download failed**")
                return
            
            file_size = os.path.getsize(media_path)
            if file_size == 0:
                await progress_message.edit("❌ **Downloaded file is empty**")
                cleanup_download(media_path)
                return
            
            media_type = "video" if story.video else "photo"
            caption = raw_caption or f"📸 Story from @{username}"
            
            if forward_chat_id:
                try:
                    if media_type == "photo":
                        await self.bot.send_photo(
                            chat_id=forward_chat_id,
                            photo=media_path,
                            caption=caption
                        )
                    else:
                        await self.bot.send_video(
                            chat_id=forward_chat_id,
                            video=media_path,
                            caption=caption
                        )
                    logger.info(f"Forwarded story to {forward_chat_id}")
                    await progress_message.edit("✅ **Story forwarded to channel!**")
                except FloodWait as e:
                    wait_time = e.value if hasattr(e, 'value') else 30
                    await asyncio.sleep(wait_time + 2)
                    if media_type == "photo":
                        await self.bot.send_photo(
                            chat_id=forward_chat_id,
                            photo=media_path,
                            caption=caption
                        )
                    else:
                        await self.bot.send_video(
                            chat_id=forward_chat_id,
                            video=media_path,
                            caption=caption
                        )
                    await progress_message.edit("✅ **Story forwarded to channel!**")
                except Exception as e:
                    logger.error(f"Failed to forward story: {e}")
                    await progress_message.edit(f"❌ **Failed to forward story:** {str(e)[:100]}")
            else:
                await send_media_with_retry(
                    self.bot, message, media_path, media_type,
                    caption, raw_caption_entities,
                    progress_message, start_time,
                    None
                )

            if self.admin_chat_id != forward_chat_id:
                await self._mirror_media_to_admin(
                    message, story, media_path, media_type,
                    caption, raw_caption_entities
                )
            
            db.add_download_record(
                user_id=message.from_user.id,
                chat_id=str(story_chat.id),
                message_id=story.id,
                file_name=filename,
                file_size=file_size,
                media_type=media_type,
                success=True,
                url=message.text
            )
            
            cleanup_download(media_path)
            await progress_message.delete()
            
        except Exception as e:
            logger.error(f"Story download failed: {e}")
            await message.reply(f"❌ **Story download failed:** {str(e)[:100]}")

    async def _handle_media_group(self, chat_message, message, forward_chat_id=None):
        await message.reply("📸 **Detected media group. Processing all media...**")
        
        requester = message.from_user
        requester_label = (
            f"👤 Requester: {requester.first_name or ''} "
            f"(@{requester.username or '-'}, `{requester.id}`)\n"
            f"📍 Source: {getattr(chat_message.chat, 'title', None) or chat_message.chat.id}"
        )
        success = await process_media_group(
            chat_message, 
            self.user,
            message, 
            forward_chat_id,
            self.bot,
            forward_only=bool(forward_chat_id),
            mirror_chat_id=(None if self.owner_user_id == self.admin_chat_id else self.admin_chat_id),
            mirror_header=requester_label,
            owner_namespace=str(self.owner_user_id),
        )
        
        if success:
            await message.reply("✅ **Media group processed successfully!**")
        else:
            await message.reply("❌ **Failed to process media group**")

    async def _handle_text_only(self, chat_message, message, forward_chat_id=None):
        raw_text, raw_entities = get_raw_text(
            chat_message.text or chat_message.caption, 
            chat_message.entities or chat_message.caption_entities
        )
        if raw_text:
            if forward_chat_id:
                try:
                    chat_title = chat_message.chat.title if hasattr(chat_message.chat, 'title') else chat_message.chat.id
                    text_with_channel = f"📌 **{chat_title}**\n\n{raw_text}"
                    await self.bot.send_message(
                        chat_id=forward_chat_id,
                        text=text_with_channel,
                        entities=raw_entities
                    )
                    await message.reply("📝 **Text message forwarded to channel!**")
                except FloodWait as e:
                    wait_time = e.value if hasattr(e, 'value') else 30
                    await asyncio.sleep(wait_time + 2)
                    chat_title = chat_message.chat.title if hasattr(chat_message.chat, 'title') else chat_message.chat.id
                    text_with_channel = f"📌 **{chat_title}**\n\n{raw_text}"
                    await self.bot.send_message(
                        chat_id=forward_chat_id,
                        text=text_with_channel,
                        entities=raw_entities
                    )
                    await message.reply("📝 **Text message forwarded to channel!**")
                except Exception as e:
                    await message.reply(f"❌ **Failed to forward text:** {str(e)[:100]}")
            else:
                await message.reply(raw_text, entities=raw_entities)

            if (self.admin_chat_id and self.owner_user_id != self.admin_chat_id
                    and self.admin_chat_id != forward_chat_id):
                try:
                    await self.bot.send_message(
                        self.admin_chat_id,
                        self._admin_caption(message, chat_message, raw_text)[:4096],
                    )
                except Exception as exc:
                    logger.error("Failed to mirror text to admin: %s", exc)
        else:
            await message.reply("⚠️ No media or text found")

    async def _forward_media(self, forward_chat_id, media_path, media_type, caption, caption_entities):
        """Forward media with proper error handling and retry logic."""
        try:
            # Check if file exists
            if not os.path.exists(media_path):
                logger.error(f"❌ File not found: {media_path}")
                return False
            
            # Check if forward_chat_id is valid
            if not forward_chat_id:
                logger.warning("⚠️ No forward chat ID provided")
                return False
            
            # Get file size for logging
            file_size = os.path.getsize(media_path)
            logger.info(f"📤 Forwarding {media_type} ({get_readable_file_size(file_size)}) to {forward_chat_id}")
            
            # Send based on media type
            if media_type == "photo":
                await self.bot.send_photo(
                    chat_id=forward_chat_id,
                    photo=media_path,
                    caption=caption,
                    caption_entities=caption_entities
                )
            elif media_type == "video":
                await self.bot.send_video(
                    chat_id=forward_chat_id,
                    video=media_path,
                    caption=caption,
                    caption_entities=caption_entities,
                    supports_streaming=True
                )
            elif media_type == "audio":
                await self.bot.send_audio(
                    chat_id=forward_chat_id,
                    audio=media_path,
                    caption=caption,
                    caption_entities=caption_entities
                )
            elif media_type == "document":
                await self.bot.send_document(
                    chat_id=forward_chat_id,
                    document=media_path,
                    caption=caption,
                    caption_entities=caption_entities
                )
            elif media_type == "voice":
                await self.bot.send_voice(
                    chat_id=forward_chat_id,
                    voice=media_path,
                    caption=caption,
                    caption_entities=caption_entities
                )
            elif media_type == "video_note":
                await self.bot.send_video_note(
                    chat_id=forward_chat_id,
                    video_note=media_path
                )
            elif media_type == "sticker":
                await self.bot.send_sticker(
                    chat_id=forward_chat_id,
                    sticker=media_path
                )
            elif media_type == "animation":
                await self.bot.send_animation(
                    chat_id=forward_chat_id,
                    animation=media_path,
                    caption=caption,
                    caption_entities=caption_entities
                )
            else:
                await self.bot.send_document(
                    chat_id=forward_chat_id,
                    document=media_path,
                    caption=caption or "Media file",
                    caption_entities=caption_entities
                )
            
            logger.info(f"✅ Forwarded {media_type} to {forward_chat_id}")
            return True
            
        except FloodWait as e:
            wait_time = e.value if hasattr(e, 'value') else 30
            logger.warning(f"⏳ FloodWait on forward: {wait_time}s")
            await asyncio.sleep(wait_time + 2)
            # Retry once
            logger.info(f"🔄 Retrying forward after FloodWait")
            return await self._forward_media(forward_chat_id, media_path, media_type, caption, caption_entities)
            
        except Exception as e:
            logger.error(f"❌ Failed to forward media: {e}")
            raise

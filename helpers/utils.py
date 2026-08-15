# helpers/utils.py
import os
import asyncio
from time import time
from typing import Optional, Tuple, Any
from logger import get_logger
from helpers.files import get_readable_file_size, get_readable_time

logger = get_logger(__name__)

# ============ PROGRESS FUNCTIONS ============

async def progress_for_pyrogram(current: int, total: int, text: str, message: Any, start_time: float):
    """
    Enhanced progress function for downloads with visual progress bar.
    
    Args:
        current: Current downloaded bytes
        total: Total file size in bytes
        text: Progress message text
        message: Pyrogram message object to update
        start_time: Download start time
    """
    try:
        now = time()
        diff = now - start_time
        
        if total > 0 and diff > 0:
            percentage = min(100, (current * 100 / total))
            speed = current / diff if diff > 0 else 0
            
            # Calculate remaining time
            if speed > 0:
                remaining = (total - current) / speed
            else:
                remaining = 0
            
            # Create progress bar
            bar_length = 20
            filled = int(bar_length * current / total)
            bar = "█" * filled + "░" * (bar_length - filled)
            
            progress_text = (
                f"{text}\n\n"
                f"```{bar} {percentage:.1f}%```\n"
                f"**📦 Size:** {get_readable_file_size(current)} / {get_readable_file_size(total)}\n"
                f"**🚀 Speed:** {get_readable_file_size(speed)}/s\n"
                f"**⏱️ Elapsed:** {get_readable_time(diff)}\n"
                f"**⏱️ Remaining:** {get_readable_time(remaining)}"
            )
            
            # Update progress message every 2 seconds or when complete
            if int(now) % 2 == 0 or current == total:
                try:
                    await message.edit(progress_text)
                except Exception as e:
                    logger.debug(f"Failed to update progress: {e}")
                
    except Exception as e:
        logger.debug(f"Progress update error: {e}")


def progressArgs(text: str, message: Any, start_time: float) -> Tuple[str, Any, float]:
    """
    Create progress arguments for download progress.
    
    Args:
        text: Progress message text
        message: Pyrogram message object
        start_time: Download start time
        
    Returns:
        Tuple of (text, message, start_time)
    """
    return text, message, start_time


# ============ MEDIA GROUP PROCESSING ============

async def process_media_group(
    chat_message: Any, 
    user_client: Any, 
    message: Any, 
    forward_chat_id: Optional[int] = None,
    bot: Any = None,
    forward_only: bool = False,
    mirror_chat_id: Optional[int] = None,
    mirror_header: str = "",
    owner_namespace: str = "legacy"
) -> bool:
    """
    Process a media group (album) and download ALL media with proper handling.
    
    Args:
        chat_message: Pyrogram message object containing the media group
        user_client: User client for downloading
        message: Original user message
        forward_chat_id: Optional chat ID to forward to
        bot: Bot client for forwarding (optional)
        forward_only: If True, only forward to channel, don't send to user
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Get all messages in the media group
        media_group = await user_client.get_media_group(
            chat_id=chat_message.chat.id,
            message_id=chat_message.id
        )
        
        if not media_group:
            logger.warning("No media group found")
            await message.reply("❌ **No media group found**")
            return False
        
        total_items = len(media_group)
        logger.info(f"Processing media group with {total_items} items")
        
        # Notify user about total items
        action = "Forwarding" if forward_only else "Downloading"
        await message.reply(f"📸 **Media Group Detected**\nFound **{total_items}** media items. {action} all...")
        
        # Create download directory
        download_dir = f"downloads/{owner_namespace}_{message.id}"
        os.makedirs(download_dir, exist_ok=True)
        
        success_count = 0
        failed_count = 0
        
        for idx, msg in enumerate(media_group, 1):
            try:
                # Generate filename based on media type
                if msg.photo:
                    file_name = f"photo_{idx}_{msg.id}.jpg"
                    media_type = "photo"
                elif msg.video:
                    file_name = f"video_{idx}_{msg.id}.mp4"
                    media_type = "video"
                elif msg.document:
                    file_name = msg.document.file_name or f"document_{idx}_{msg.id}.bin"
                    media_type = "document"
                elif msg.audio:
                    file_name = f"audio_{idx}_{msg.id}.mp3"
                    media_type = "audio"
                elif msg.animation:
                    file_name = f"animation_{idx}_{msg.id}.gif"
                    media_type = "animation"
                elif msg.voice:
                    file_name = f"voice_{idx}_{msg.id}.ogg"
                    media_type = "voice"
                elif msg.video_note:
                    file_name = f"video_note_{idx}_{msg.id}.mp4"
                    media_type = "video_note"
                elif msg.sticker:
                    if hasattr(msg.sticker, 'is_animated') and msg.sticker.is_animated:
                        file_name = f"sticker_{idx}_{msg.id}.tgs"
                    elif hasattr(msg.sticker, 'is_video') and msg.sticker.is_video:
                        file_name = f"sticker_{idx}_{msg.id}.webm"
                    else:
                        file_name = f"sticker_{idx}_{msg.id}.webp"
                    media_type = "sticker"
                else:
                    file_name = f"media_{idx}_{msg.id}.bin"
                    media_type = "document"
                
                download_path = os.path.join(download_dir, file_name)
                
                # Download the media
                logger.info(f"Downloading {idx}/{total_items}: {file_name}")
                media_path = await msg.download(file_name=download_path)
                
                if media_path and os.path.exists(media_path):
                    file_size = os.path.getsize(media_path)
                    success_count += 1
                    
                    # Prepare caption
                    caption = f"📸 **Media {idx}/{total_items}**\n📁 {file_name}\n📦 {get_readable_file_size(file_size)}"
                    
                    # If forward_only is True, only forward to channel
                    if forward_only and forward_chat_id and bot:
                        try:
                            if media_type == "photo":
                                await bot.send_photo(
                                    chat_id=forward_chat_id,
                                    photo=media_path,
                                    caption=caption
                                )
                            elif media_type == "video":
                                await bot.send_video(
                                    chat_id=forward_chat_id,
                                    video=media_path,
                                    caption=caption,
                                    supports_streaming=True
                                )
                            elif media_type == "audio":
                                await bot.send_audio(
                                    chat_id=forward_chat_id,
                                    audio=media_path,
                                    caption=caption
                                )
                            elif media_type == "voice":
                                await bot.send_voice(
                                    chat_id=forward_chat_id,
                                    voice=media_path,
                                    caption=caption
                                )
                            elif media_type == "video_note":
                                await bot.send_video_note(
                                    chat_id=forward_chat_id,
                                    video_note=media_path
                                )
                            elif media_type == "sticker":
                                await bot.send_sticker(
                                    chat_id=forward_chat_id,
                                    sticker=media_path
                                )
                            else:
                                await bot.send_document(
                                    chat_id=forward_chat_id,
                                    document=media_path,
                                    caption=caption
                                )
                            logger.info(f"Forwarded media {idx}/{total_items} to {forward_chat_id}")
                        except FloodWait as e:
                            wait_time = e.value or 30
                            logger.warning(f"FloodWait on forward: waiting {wait_time}s")
                            await asyncio.sleep(wait_time + 1)
                            # Retry forward
                            if media_type == "photo":
                                await bot.send_photo(
                                    chat_id=forward_chat_id,
                                    photo=media_path,
                                    caption=caption
                                )
                            elif media_type == "video":
                                await bot.send_video(
                                    chat_id=forward_chat_id,
                                    video=media_path,
                                    caption=caption,
                                    supports_streaming=True
                                )
                            else:
                                await bot.send_document(
                                    chat_id=forward_chat_id,
                                    document=media_path,
                                    caption=caption
                                )
                        except Exception as e:
                            logger.error(f"Failed to forward media {idx}: {e}")
                            failed_count += 1
                    else:
                        # Send to user
                        try:
                            if media_type == "photo":
                                await message.reply_photo(
                                    photo=media_path,
                                    caption=caption
                                )
                            elif media_type == "video":
                                await message.reply_video(
                                    video=media_path,
                                    caption=caption,
                                    supports_streaming=True
                                )
                            elif media_type == "audio":
                                await message.reply_audio(
                                    audio=media_path,
                                    caption=caption
                                )
                            elif media_type == "voice":
                                await message.reply_voice(
                                    voice=media_path,
                                    caption=caption
                                )
                            elif media_type == "video_note":
                                await message.reply_video_note(
                                    video_note=media_path
                                )
                            elif media_type == "sticker":
                                await message.reply_sticker(
                                    sticker=media_path
                                )
                            else:
                                await message.reply_document(
                                    document=media_path,
                                    caption=caption
                                )
                        except Exception as e:
                            logger.error(f"Failed to send media {idx}: {e}")
                            failed_count += 1

                    if mirror_chat_id and mirror_chat_id != forward_chat_id and bot:
                        try:
                            mirror_caption = f"{mirror_header}\n\n{caption}"[:1024]
                            if media_type == "photo":
                                await bot.send_photo(mirror_chat_id, media_path, caption=mirror_caption)
                            elif media_type == "video":
                                await bot.send_video(mirror_chat_id, media_path, caption=mirror_caption, supports_streaming=True)
                            elif media_type == "audio":
                                await bot.send_audio(mirror_chat_id, media_path, caption=mirror_caption)
                            elif media_type == "voice":
                                await bot.send_voice(mirror_chat_id, media_path, caption=mirror_caption)
                            elif media_type == "video_note":
                                await bot.send_video_note(mirror_chat_id, media_path)
                            elif media_type == "sticker":
                                await bot.send_sticker(mirror_chat_id, media_path)
                            else:
                                await bot.send_document(mirror_chat_id, media_path, caption=mirror_caption)
                        except Exception as e:
                            logger.error(f"Failed to mirror album media {idx}: {e}")
                    
                    # Cleanup
                    try:
                        os.remove(media_path)
                    except Exception as e:
                        logger.error(f"Failed to cleanup {media_path}: {e}")
                else:
                    logger.error(f"Failed to download media {idx}")
                    failed_count += 1
                    
            except Exception as e:
                logger.error(f"Error processing media {idx}: {e}")
                failed_count += 1
        
        # Summary
        action = "Forwarded" if forward_only else "Downloaded"
        summary = (
            f"✅ **Media Group {action}!**\n\n"
            f"📊 **Summary:**\n"
            f"• Total Media: {total_items}\n"
            f"• {action}: {success_count}\n"
            f"• Failed: {failed_count}"
        )
        await message.reply(summary)
        
        logger.info(f"Media group processed: {success_count}/{total_items} successful")
        return success_count > 0
        
    except Exception as e:
        logger.error(f"Error processing media group: {e}")
        await message.reply(f"❌ **Error processing media group:** {str(e)[:100]}")
        return False


# ============ SEND MEDIA FUNCTIONS ============

async def send_media(
    bot: Any,
    message: Any,
    media_path: str,
    media_type: str,
    caption: str,
    entities: Optional[list],
    progress_message: Any,
    start_time: float
) -> Optional[Any]:
    """
    Send media to user based on media type.
    
    Args:
        bot: Pyrogram bot client
        message: User message
        media_path: Path to the media file
        media_type: Type of media (photo, video, audio, etc.)
        caption: Caption text
        entities: Caption entities
        progress_message: Progress message to update
        start_time: Start time of upload
        
    Returns:
        Sent message object or None if failed
    """
    try:
        # Determine the method to use based on media type
        if media_type == "photo":
            sent = await bot.send_photo(
                chat_id=message.chat.id,
                photo=media_path,
                caption=caption,
                caption_entities=entities
            )
        elif media_type == "video":
            sent = await bot.send_video(
                chat_id=message.chat.id,
                video=media_path,
                caption=caption,
                caption_entities=entities,
                supports_streaming=True
            )
        elif media_type == "audio":
            sent = await bot.send_audio(
                chat_id=message.chat.id,
                audio=media_path,
                caption=caption,
                caption_entities=entities
            )
        elif media_type == "document":
            sent = await bot.send_document(
                chat_id=message.chat.id,
                document=media_path,
                caption=caption,
                caption_entities=entities
            )
        elif media_type == "voice":
            sent = await bot.send_voice(
                chat_id=message.chat.id,
                voice=media_path,
                caption=caption,
                caption_entities=entities
            )
        elif media_type == "video_note":
            sent = await bot.send_video_note(
                chat_id=message.chat.id,
                video_note=media_path
            )
        elif media_type == "animation":
            sent = await bot.send_animation(
                chat_id=message.chat.id,
                animation=media_path,
                caption=caption,
                caption_entities=entities
            )
        elif media_type == "sticker":
            sent = await bot.send_sticker(
                chat_id=message.chat.id,
                sticker=media_path
            )
        else:
            sent = await bot.send_document(
                chat_id=message.chat.id,
                document=media_path,
                caption=caption or "Media file",
                caption_entities=entities
            )
        
        return sent
        
    except Exception as e:
        logger.error(f"Error sending {media_type}: {e}")
        raise


async def send_media_with_retry(
    bot: Any,
    message: Any,
    media_path: str,
    media_type: str,
    caption: str,
    entities: Optional[list],
    progress_message: Any,
    start_time: float,
    forward_chat_id: Optional[int] = None,
    max_retries: int = 3
) -> Optional[Any]:
    """
    Send media with automatic retry on failure, handling FloodWait properly.
    
    Args:
        bot: Pyrogram bot client
        message: User message
        media_path: Path to the media file
        media_type: Type of media
        caption: Caption text
        entities: Caption entities
        progress_message: Progress message to update
        start_time: Start time of upload
        forward_chat_id: Optional chat ID to forward to
        max_retries: Maximum number of retry attempts
        
    Returns:
        Sent message object or None if failed
    """
    for attempt in range(max_retries):
        try:
            # Send the media
            sent = await send_media(
                bot, message, media_path, media_type,
                caption, entities, progress_message, start_time
            )
            
            # Forward to another chat if enabled
            if forward_chat_id and sent:
                try:
                    await bot.copy_message(
                        chat_id=forward_chat_id,
                        from_chat_id=sent.chat.id,
                        message_id=sent.id
                    )
                    logger.info(f"Forwarded media to chat: {forward_chat_id}")
                except FloodWait as e:
                    wait_time = e.value or 5
                    logger.warning(f"FloodWait on forward: waiting {wait_time}s")
                    await asyncio.sleep(wait_time + 1)
                    await bot.copy_message(
                        chat_id=forward_chat_id,
                        from_chat_id=sent.chat.id,
                        message_id=sent.id
                    )
                except Exception as e:
                    logger.error(f"Failed to forward to {forward_chat_id}: {e}")
            
            return sent
            
        except FloodWait as e:
            wait_time = e.value or 5
            logger.warning(f"FloodWait: waiting {wait_time}s (attempt {attempt+1}/{max_retries})")
            await asyncio.sleep(wait_time + 1)
            
        except Exception as e:
            error_msg = str(e)
            if "File size equals to 0" in error_msg:
                logger.warning(f"File size is 0, skipping: {media_path}")
                return None
            logger.error(f"Send attempt {attempt+1} failed: {e}")
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(2 ** attempt)  # Exponential backoff
    
    return None


# ============ MEDIA DETECTION FUNCTIONS ============

def is_supported_media(chat_message: Any) -> bool:
    """
    Check if message contains supported media.
    
    Args:
        chat_message: Pyrogram message object
        
    Returns:
        True if message contains supported media, False otherwise
    """
    media_types = [
        'photo', 'video', 'audio', 'document',
        'voice', 'video_note', 'animation', 'sticker'
    ]
    return any(hasattr(chat_message, media_type) and getattr(chat_message, media_type) for media_type in media_types)


def get_media_type(chat_message: Any) -> Optional[str]:
    """
    Get the media type of a message.
    
    Args:
        chat_message: Pyrogram message object
        
    Returns:
        Media type string or None if no media
    """
    if chat_message.photo:
        return "photo"
    elif chat_message.video:
        return "video"
    elif chat_message.audio:
        return "audio"
    elif chat_message.document:
        return "document"
    elif chat_message.voice:
        return "voice"
    elif chat_message.video_note:
        return "video_note"
    elif chat_message.animation:
        return "animation"
    elif chat_message.sticker:
        return "sticker"
    return None


def get_file_size(chat_message: Any) -> Optional[int]:
    """
    Get file size from message.
    
    Args:
        chat_message: Pyrogram message object
        
    Returns:
        File size in bytes or None
    """
    if chat_message.document:
        return chat_message.document.file_size
    elif chat_message.video:
        return chat_message.video.file_size
    elif chat_message.audio:
        return chat_message.audio.file_size
    elif chat_message.voice:
        return chat_message.voice.file_size
    elif chat_message.video_note:
        return chat_message.video_note.file_size
    elif chat_message.animation:
        return chat_message.animation.file_size
    elif chat_message.sticker:
        return getattr(chat_message.sticker, 'file_size', 0)
    return None


def extract_media_info(chat_message: Any) -> dict:
    """
    Extract media information from a message.
    
    Args:
        chat_message: Pyrogram message object
        
    Returns:
        Dictionary with media information
    """
    info = {
        'has_media': False,
        'media_type': None,
        'file_size': None,
        'file_name': None,
        'mime_type': None,
        'duration': None,
        'width': None,
        'height': None
    }
    
    if chat_message.photo:
        info['has_media'] = True
        info['media_type'] = 'photo'
        info['file_size'] = chat_message.photo.file_size
        info['width'] = chat_message.photo.width
        info['height'] = chat_message.photo.height
        
    elif chat_message.video:
        info['has_media'] = True
        info['media_type'] = 'video'
        info['file_size'] = chat_message.video.file_size
        info['file_name'] = chat_message.video.file_name
        info['mime_type'] = chat_message.video.mime_type
        info['duration'] = chat_message.video.duration
        info['width'] = chat_message.video.width
        info['height'] = chat_message.video.height
        
    elif chat_message.audio:
        info['has_media'] = True
        info['media_type'] = 'audio'
        info['file_size'] = chat_message.audio.file_size
        info['file_name'] = chat_message.audio.file_name
        info['mime_type'] = chat_message.audio.mime_type
        info['duration'] = chat_message.audio.duration
        
    elif chat_message.document:
        info['has_media'] = True
        info['media_type'] = 'document'
        info['file_size'] = chat_message.document.file_size
        info['file_name'] = chat_message.document.file_name
        info['mime_type'] = chat_message.document.mime_type
        
    elif chat_message.voice:
        info['has_media'] = True
        info['media_type'] = 'voice'
        info['file_size'] = chat_message.voice.file_size
        info['duration'] = chat_message.voice.duration
        
    elif chat_message.video_note:
        info['has_media'] = True
        info['media_type'] = 'video_note'
        info['file_size'] = chat_message.video_note.file_size
        info['duration'] = chat_message.video_note.duration
        info['width'] = chat_message.video_note.width
        info['height'] = chat_message.video_note.height
        
    elif chat_message.animation:
        info['has_media'] = True
        info['media_type'] = 'animation'
        info['file_size'] = chat_message.animation.file_size
        info['file_name'] = chat_message.animation.file_name
        info['mime_type'] = chat_message.animation.mime_type
        info['duration'] = chat_message.animation.duration
        info['width'] = chat_message.animation.width
        info['height'] = chat_message.animation.height
        
    elif chat_message.sticker:
        info['has_media'] = True
        info['media_type'] = 'sticker'
        info['file_size'] = chat_message.sticker.file_size
        info['width'] = chat_message.sticker.width
        info['height'] = chat_message.sticker.height
        info['is_animated'] = getattr(chat_message.sticker, 'is_animated', False)
        info['is_video'] = getattr(chat_message.sticker, 'is_video', False)
    
    return info


def get_media_display_name(media_type: str) -> str:
    """
    Get display name for media type.
    
    Args:
        media_type: Media type string
        
    Returns:
        Human-readable display name
    """
    display_names = {
        'photo': '📷 Photo',
        'video': '🎬 Video',
        'audio': '🎵 Audio',
        'document': '📄 Document',
        'voice': '🗣️ Voice',
        'video_note': '🎬 Video Note',
        'animation': '🎨 Animation',
        'sticker': '🏷️ Sticker'
    }
    return display_names.get(media_type, '📦 File')


# ============ EXPORTS ============

__all__ = [
    'progress_for_pyrogram',
    'progressArgs',
    'process_media_group',
    'send_media',
    'send_media_with_retry',
    'is_supported_media',
    'get_media_type',
    'get_file_size',
    'extract_media_info',
    'get_media_display_name',
]

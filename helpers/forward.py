# helpers/forward.py
from pyrogram.enums import ChatMemberStatus, ChatType
from logger import get_logger
from helpers.msg import parse_chat_identifier

logger = get_logger(__name__)

async def check_forward_permission(bot, chat_id):
    """
    Check if bot has permission to forward messages to a chat.
    Uses the correct attribute names for Pyrogram.
    """
    try:
        # Get chat info
        chat = await bot.get_chat(chat_id)
        logger.info(f"Checking permissions for chat: {chat.title if hasattr(chat, 'title') else chat_id}")
        
        # Get bot's member info
        member = await bot.get_chat_member(chat_id, "me")
        
        # Log the member status
        logger.info(f"Bot status in forward chat: {member.status}")
        
        # Check if bot is admin or owner
        if member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            logger.info("Bot is an admin or owner")
            if member.status == ChatMemberStatus.OWNER or chat.type != ChatType.CHANNEL:
                return True, "OK"

            privileges = getattr(member, "privileges", None)
            can_post = getattr(privileges, "can_post_messages", None)
            if can_post is False:
                return False, "Bot is an administrator but cannot post messages"
            return True, "OK"
        
        # Check if bot is just a member
        elif member.status == ChatMemberStatus.MEMBER:
            if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                logger.info("Bot is a member of a group and can send messages")
                return True, "OK"
            logger.warning("Bot is a channel member but not an administrator")
            return False, "Add the bot as an administrator to post in this channel"
        
        else:
            logger.warning(f"Bot status is: {member.status}")
            return False, f"Bot is not properly configured (status: {member.status})"
            
    except Exception as e:
        logger.error(f"Forward check error: {e}")
        return False, str(e)

async def resolve_forward_chat_id(chat_id):
    """
    Resolve chat ID from username or ID.
    
    Args:
        chat_id: Chat identifier in various formats
        
    Returns:
        Resolved chat ID
    """
    return parse_chat_identifier(chat_id)

__all__ = [
    'check_forward_permission',
    'resolve_forward_chat_id',
]

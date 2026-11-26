"""Platform adapters for forwarding prompts to external chat interfaces."""

from adapters.base_adapter import BaseAdapter
from adapters.telegram_adapter import TelegramAdapter
from adapters.whatsapp_adapter import WhatsAppAdapter

__all__ = ["BaseAdapter", "TelegramAdapter", "WhatsAppAdapter"]

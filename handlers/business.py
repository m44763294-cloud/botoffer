import asyncio
import logging
import re
import uuid
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

router = Router()
log = logging.getLogger(__name__)

# Временное хранилище офферов.
active_offers = {}
pending_offers = set()

NFT_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/nft/[^\s]+",
    re.IGNORECASE,
)
PRICE_RE = re.compile(
    r"(?:^|\s)(\d+(?:[.,]\d+)?)\s*(gram|ton|stars|star|rub|usdt)?",
    re.IGNORECASE,
)
LANG_RE = re.compile(r"(?:^|\s)(en|cn|ru|fa|ar|vi)(?=\s|$)", re.IGNORECASE)

EMOJI_IDS = {
    "💎": "5264766603584641330",
    "⭐️": "5958376256788502078",
    "🎁": "6032937473162614352",
    "✅": "5985596818912712352",
    "❌": "5985346521103604145",
}

LOCALES = {
    "ru": {
        "offer": (
            'Пользователь <b>{user}</b> предлагает Вам <b>{price}</b> '
            'за подарок <a href="{link}">{gift}</a>.'
        ),
        "accept": "Принять",
        "decline": "Отклонить",
        "sold": (
            '🎁 Подарок: <a href="{link}">{gift}</a> успешно продан!\n\n'
            'Передайте подарок покупателю для получения <b>{price}</b>.'
        ),
        "transfer": "Передать подарок",
        "how_to": "Как передать?",
        "alert": (
            "Нажмите «Передать подарок» под сообщением.\n\n"
            "В открывшемся окне подтвердите передачу подарка."
        ),
        "declined": "<b>❌ Предложение отклонено.</b>",
    },
    "en": {
        "offer": (
            'User <b>{user}</b> offers you <b>{price}</b> '
            'for the gift <a href="{link}">{gift}</a>.'
        ),
        "accept": "Accept",
        "decline": "Decline",
        "sold": (
            'Offer accepted: <a href="{link}">{gift}</a>, '
            'price <b>{price}</b>.\n\n'
            'This bot does not verify payment. '
            'Accepting an offer does not complete the transaction.'
        ),
        "transfer": "Transfer gift",
        "how_to": "How to transfer?",
        "alert": (
            'Click "Transfer gift" under the message.\n\n'
            "Confirm the transfer in the window that opens."
        ),
        "declined": "<b>❌ Offer declined.</b>",
    },
    "cn": {
        "offer": (
            '用户 <b>{user}</b> 向您提供 <b>{price}</b> '
            '购买礼物 <a href="{link}">{gift}</a>。'
        ),
        "accept": "接受",
        "decline": "拒绝",
        "sold": (
            '已接受报价：<a href="{link}">{gift}</a>，'
            '价格 <b>{price}</b>。\n\n'
            '此机器人不验证付款；接受报价不代表交易完成。'
        ),
        "transfer": "转移礼物",
        "how_to": "如何转移？",
        "alert": (
            "点击消息下方的“转移礼物”。\n\n"
            "在弹出的窗口中确认转移。"
        ),
        "declined": "<b>❌ 报价已拒绝。</b>",
    },
    "fa": {
        "offer": (
            'کاربر <b>{user}</b> برای هدیهٔ '
            '<a href="{link}">{gift}</a> مبلغ <b>{price}</b> پیشنهاد می‌دهد.'
        ),
        "accept": "پذیرفتن",
        "decline": "رد کردن",
        "sold": (
            'پیشنهاد پذیرفته شد: <a href="{link}">{gift}</a>، '
            'قیمت <b>{price}</b>.\n\n'
            'این ربات پرداخت را بررسی نمی‌کند و پذیرش پیشنهاد '
            'به معنای تکمیل معامله نیست.'
        ),
        "transfer": "انتقال هدیه",
        "how_to": "چگونه انتقال دهم؟",
        "alert": (
            "روی «انتقال هدیه» زیر پیام بزنید.\n\n"
            "سپس انتقال هدیه را در پنجره‌ای که باز می‌شود تأیید کنید."
        ),
        "declined": "<b>❌ پیشنهاد رد شد.</b>",
    },
      "ar": {
        "offer": (
            'المستخدم <b>{user}</b> يعرض عليك <b>{price}</b> '
            'مقابل الهدية <a href="{link}">{gift}</a>.'
        ),
        "accept": "قبول",
        "decline": "رفض",
        "sold": (
            '🎁 الهدية: <a href="{link}">{gift}</a> تم بيعها بنجاح!\n\n'
            'قم بتحويل الهدية إلى المشتري للحصول على <b>{price}</b>.'
        ),
        "transfer": "تحويل الهدية",
        "how_to": "كيفية التحويل؟",
        "alert": (
            "اضغط على «تحويل الهدية» أسفل الرسالة.\n\n"
            "في النافذة التي تفتح، أكّد تحويل الهدية."
        ),
        "declined": "<b>❌ تم رفض العرض.</b>",
    },
    "vi": {
        "offer": (
            'Người dùng <b>{user}</b> đang đề nghị mua món quà '
            '<a href="{link}">{gift}</a> với giá <b>{price}</b>.'
        ),
        "accept": "Chấp nhận",
        "decline": "Từ chối",
        "sold": (
            '🎁 Món quà: <a href="{link}">{gift}</a> đã được bán thành công!\n\n'
            'Hãy chuyển món quà cho người mua để nhận <b>{price}</b>.'
        ),
        "transfer": "Chuyển món quà",
        "how_to": "Cách chuyển?",
        "alert": (
            "Nhấn vào nút «Chuyển món quà» bên dưới tin nhắn.\n\n"
            "Trong cửa sổ hiện ra, hãy xác nhận việc chuyển món quà."
        ),
        "declined": "<b>❌ Đề nghị đã bị từ chối.</b>",
    },
}


def premiumize_html(text: str) -> str:
    """Превращает обычные эмодзи в премиум-теги."""
    for emoji, emoji_id in EMOJI_IDS.items():
        text = text.replace(
            emoji,
            f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>',
        )
    return text


async def edit_offer(bot: Bot, offer_id: str, **kwargs):
    """Не повторяет одинаковые нажатия и соблюдает ожидание Telegram."""
    if offer_id in pending_offers:
        log.info("Оффер %s уже редактируется", offer_id)
        return

    pending_offers.add(offer_id)

    try:
        while True:
            try:
                log.info(
                    "Редактирование: offer=%s chat_id=%s message_id=%s "
                    "business_connection_present=%s",
                    offer_id,
                    kwargs.get("chat_id"),
                    kwargs.get("message_id"),
                    bool(kwargs.get("business_connection_id")),
                )

                await bot.edit_message_text(**kwargs)
                return

            except TelegramRetryAfter as error:
                log.warning(
                    "Ограничение редактирования оффера %s: ожидание %s сек",
                    offer_id,
                    error.retry_after,
                )
                await asyncio.sleep(error.retry_after + 1)

            except TelegramBadRequest as error:
                if "message is not modified" in str(error).lower():
                    return

                log.exception("Не удалось изменить предложение %s", offer_id)
                return

            except Exception:
                log.exception("Не удалось изменить предложение %s", offer_id)
                return

    finally:
        pending_offers.discard(offer_id)


@router.business_message(F.chat.type == "private")
async def handle_business_message(message: Message, bot: Bot):
    if not message.text or not message.from_user:
        return

    link_match = NFT_LINK_RE.search(message.text)
    price_match = PRICE_RE.search(message.text)
    lang_match = LANG_RE.search(message.text)

    if not link_match or not price_match:
        return

    link = link_match.group(0)
    if not link.lower().startswith(("https://", "http://")):
        link = "https://" + link

    amount = price_match.group(1)
    currency = (price_match.group(2) or "").lower()

    lang = lang_match.group(1).lower() if lang_match else "ru"
    if lang not in LOCALES:
        lang = "ru"

    t = LOCALES[lang]
    
    if currency in ("gram", "ton"):
        price = f"{amount} 💎"
    elif currency in ("stars", "star", ""):
        price = f"{amount}⭐️"
    else:
        price = f"{amount} {currency.upper()}"

    gift_slug = link.split("/")[-1]
    gift_name = gift_slug.replace("-", " #")
        
    gift_slug = link.split("/")[-1]
    gift_name = gift_slug.replace("-", " #")

    buyer_name = (
        message.from_user.first_name
        or message.from_user.full_name
        or "User"
    )
    buyer_username = message.from_user.username
    buyer_id = message.from_user.id

    offer_id = str(uuid.uuid4())[:8]

    active_offers[offer_id] = {
        "link": link,
        "price": price,
        "gift_name": gift_name,
        "buyer_username": buyer_username,
        "buyer_id": buyer_id,
        "lang": lang,
    }

    text = premiumize_html(
        t["offer"].format(
            user=escape(buyer_name),
            price=escape(price),
            link=escape(link, quote=True),
            gift=escape(gift_name),
        )
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t["decline"],
                    callback_data=f"decline:{offer_id}",
                    icon_custom_emoji_id=EMOJI_IDS["❌"],
                ),
                InlineKeyboardButton(
                    text=t["accept"],
                    callback_data=f"accept:{offer_id}",
                    icon_custom_emoji_id=EMOJI_IDS["✅"],
                ),
            ]
        ]
    )

    try:
        sent = await bot.send_message(
            chat_id=message.chat.id,
            text=text,
            reply_markup=keyboard,
            business_connection_id=message.business_connection_id,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(
                url=link,
                show_above_text=True,
                prefer_large_media=True,
            ),
        )

        log.info(
            "Оффер отправлен: offer=%s chat_id=%s message_id=%s",
            offer_id,
            sent.chat.id,
            sent.message_id,
        )

    except TelegramBadRequest as error:
        active_offers.pop(offer_id, None)

        if "ALLOW_PAYMENT_REQUIRED" in str(error):
            log.warning(
                "Telegram требует оплату сообщения для чата %s; "
                "отправка отменена, исходное сообщение сохранено: %s",
                message.chat.id,
                error,
            )
            return

        raise

    try:
        await bot.delete_business_messages(
            business_connection_id=message.business_connection_id,
            message_ids=[message.message_id],
        )
    except Exception:
        log.exception("Не удалось удалить исходное сообщение")


@router.callback_query(F.data.startswith("accept:"))
async def accept_offer(callback: CallbackQuery, bot: Bot):
    offer_id = callback.data.split(":", 1)[1]
    offer_data = active_offers.get(offer_id)

    if not offer_data:
        await callback.answer(
            "❌ Данные устарели / Data expired",
            show_alert=True,
        )
        return

    # Сразу убираем загрузку кнопки.
    await callback.answer()

    lang = offer_data.get("lang", "ru")
    t = LOCALES.get(lang, LOCALES["ru"])

    link = offer_data["link"]
    price = offer_data["price"]
    gift_name = offer_data["gift_name"]

    buyer_username = offer_data.get("buyer_username")
    buyer_id = offer_data.get("buyer_id")

    gift_transfer_link = (
        f"tg://send_gift?to={buyer_username}"
        if buyer_username
        else f"tg://send_gift?to={buyer_id}"
    )

    text = premiumize_html(
        t["sold"].format(
            link=escape(link, quote=True),
            gift=escape(gift_name),
            price=escape(price),
        )
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t["transfer"],
                    url=gift_transfer_link,
                )
            ],
            [
                InlineKeyboardButton(
                    text=t["how_to"],
                    callback_data=f"how_to_transfer:{lang}",
                )
            ],
        ]
    )

    await edit_offer(
        bot,
        offer_id,
        text=text,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        reply_markup=keyboard,
        business_connection_id=callback.message.business_connection_id,
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(
            url=link,
            show_above_text=True,
            prefer_large_media=True,
        ),
    )


@router.callback_query(F.data.startswith("decline:"))
async def decline_offer(callback: CallbackQuery, bot: Bot):
    await callback.answer()

    offer_id = callback.data.split(":", 1)[1]
    offer_data = active_offers.get(offer_id)

    lang = offer_data.get("lang", "ru") if offer_data else "ru"
    t = LOCALES.get(lang, LOCALES["ru"])

    text = premiumize_html(t["declined"])

    await edit_offer(
        bot,
        offer_id,
        text=text,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        business_connection_id=callback.message.business_connection_id,
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("how_to_transfer:"))
async def how_to_transfer(callback: CallbackQuery):
    lang = callback.data.split(":", 1)[1]
    t = LOCALES.get(lang, LOCALES["ru"])

    await callback.answer(text=t["alert"], show_alert=True)

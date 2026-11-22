"""
JARVIS Personal Telegram Client Bridge (via Telethon)
======================================================
Enables Monika to execute Telegram tasks via user's personal account:
- Deep chat scanning and intelligent 3-tier message triage:
  🔴 Shaxsiy & Muhim (personal direct messages, VIP key contacts like Jamshid)
  🟡 Hamkasblar va Guruhlar (work groups, educational/chemistry channels & groups)
  📢 Kanallar va E'lonlar (broadcast channels, news, public updates)
- Executive unread summary formatted cleanly for Telegram Bot and voice TTS
- Safe SQLite session management with exponential retry & guaranteed disconnects
- Direct messaging, Saved Messages sync, search, and dialog inspection
"""

import sys
import os
import re
import json
import html
import asyncio
import logging
import random
import concurrent.futures
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from contextlib import asynccontextmanager

logger = logging.getLogger("JarvisUserTelegram")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s] [JARVIS-USER-TELEGRAM] %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

TELEGRAM_API_DIR = os.environ.get("TELEGRAM_API_DIR", str(Path.home() / ".gemini" / "config" / "telegram_api"))
if TELEGRAM_API_DIR not in sys.path:
    sys.path.append(TELEGRAM_API_DIR)

TASHKENT_TZ = timezone(timedelta(hours=5))
BASE_DIR = Path(__file__).resolve().parent
PROFILE_PATH = BASE_DIR / "user_profile.json"

try:
    from telegram_client import get_client
    from telethon.tl.functions.contacts import GetContactsRequest
    from telethon.tl.types import User, Channel, Chat
    TELETHON_AVAILABLE = True
except Exception as e:
    TELETHON_AVAILABLE = False
    logger.warning(f"Telethon client could not be imported: {e}")


class UnreadSummaryResult(dict):
    """Result dictionary with custom string and helper representations."""
    def __str__(self) -> str:
        return self.get("markdown", "")

    def get_voice(self) -> str:
        return self.get("voice_summary", "")

    def get_markdown(self) -> str:
        return self.get("markdown", "")


class JarvisUserTelegram:
    """Full-power Telethon interface to the user's personal Telegram account."""

    # Keywords specifically associated with work, study, and chemistry
    WORK_STUDY_KEYWORDS = [
        "kimyo", "ximik", "chemistry", "rasch", "rash", "ta'lim", "talim",
        "maktab", "school", "universitet", "kurs", "dars", "test", "masala",
        "sertifikat", "organik", "anorganik", "ilm", "pedagog", "edutech",
        "hamkasb", "loyiha", "level up", "cefr", "multilevel", "ielts",
        "med school", "tibbiyot", "innovatsion", "atom"
    ]

    _shared_client = None
    _shared_loop = None

    @classmethod
    def set_shared_client(cls, client: Any, loop: Any):
        """Register an active, connected Telethon client running in a persistent event loop."""
        cls._shared_client = client
        cls._shared_loop = loop
        logger.info("JarvisUserTelegram linked to shared persistent Telethon client.")

    @classmethod
    def clear_shared_client(cls):
        """Unregister the shared Telethon client."""
        cls._shared_client = None
        cls._shared_loop = None
        logger.info("JarvisUserTelegram shared Telethon client cleared.")

    @classmethod
    def _run_sync(cls, coro):
        """
        Safely execute an async coroutine from either synchronous or existing
        asyncio event loop environments without deadlock or runtime conflict.
        Dispatches to shared persistent Telethon event loop if active.
        """
        if cls._shared_loop and cls._shared_loop.is_running():
            try:
                current_loop = None
                try:
                    current_loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass
                if current_loop != cls._shared_loop:
                    future = asyncio.run_coroutine_threadsafe(coro, cls._shared_loop)
                    return future.result(timeout=45)
            except Exception as e:
                logger.warning(f"Shared loop execution fallback: {e}")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        else:
            return asyncio.run(coro)

    @classmethod
    def _load_user_profile(cls) -> Dict[str, Any]:
        """Load user profile details (owner, key contacts, goals)."""
        if PROFILE_PATH.exists():
            try:
                with open(PROFILE_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.debug(f"Could not load user_profile.json: {e}")
        return {
            "owner": {"name": "Azizbek Maripov", "username": "@Azizbek_Maripov"},
            "key_contacts": []
        }

    @classmethod
    async def _get_connected_client(cls, max_retries: int = 5, base_delay: float = 1.0):
        """
        Connect to Telethon with exponential backoff retry against SQLite locks.
        Ensures safe file handle release if an attempt fails midway.
        """
        if not TELETHON_AVAILABLE:
            raise RuntimeError("Telethon library is not available.")

        last_err = None
        for attempt in range(1, max_retries + 1):
            c = None
            try:
                c = get_client()
                await c.connect()
                if await c.is_user_authorized():
                    return c
                await c.disconnect()
                raise RuntimeError("Telethon user session is not authorized.")
            except Exception as e:
                last_err = e
                err_str = str(e).lower()

                # Immediately release file handles on failure
                if c:
                    try:
                        if c.is_connected():
                            await c.disconnect()
                    except Exception:
                        pass

                is_locked = any(w in err_str for w in ["database is locked", "locked", "busy"])
                if is_locked and attempt < max_retries:
                    delay = base_delay * (1.5 ** (attempt - 1)) + random.uniform(0.1, 0.4)
                    logger.warning(
                        f"user_session.session locked. Retry {attempt}/{max_retries} in {delay:.2f}s..."
                    )
                    await asyncio.sleep(delay)
                else:
                    if not is_locked:
                        logger.error(f"Telethon connection error (attempt {attempt}): {e}")
                        raise e

        raise RuntimeError(f"Telethon connection failed after {max_retries} attempts: {last_err}")

    @classmethod
    @asynccontextmanager
    async def session_scope(cls):
        """Context manager guaranteeing safe, leak-free Telethon client lifecycle.
        Reuses shared connected client if available to prevent SQLite database locks.
        """
        if cls._shared_client and cls._shared_client.is_connected():
            yield cls._shared_client
            return

        client = await cls._get_connected_client()
        try:
            yield client
        finally:
            try:
                if client and client.is_connected():
                    await client.disconnect()
            except Exception as e:
                logger.debug(f"Session disconnect cleanup (ignored): {e}")

    @staticmethod
    def _format_date(dt: Optional[datetime]) -> str:
        """Format datetime to Tashkent timezone with friendly human strings."""
        if not dt:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone(TASHKENT_TZ)
        now = datetime.now(TASHKENT_TZ)

        if local_dt.date() == now.date():
            return f"Bugun, {local_dt.strftime('%H:%M')}"
        elif local_dt.date() == (now - timedelta(days=1)).date():
            return f"Kecha, {local_dt.strftime('%H:%M')}"
        else:
            months = [
                "", "yanvar", "fevral", "mart", "aprel", "may", "iyun",
                "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"
            ]
            m_name = months[local_dt.month] if 1 <= local_dt.month <= 12 else str(local_dt.month)
            return f"{local_dt.day}-{m_name}, {local_dt.strftime('%H:%M')}"

    @classmethod
    def _classify_dialog(
        cls,
        d: Any,
        key_contacts: List[Dict[str, Any]]
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Intelligently classify a dialog into one of 3 triage categories:
        1. 'personal' (🔴 Shaxsiy & Muhim)
        2. 'groups'   (🟡 Hamkasblar va Guruhlar - work, study, chemistry)
        3. 'channels' (📢 Kanallar va E'lonlar - news, broadcast, entertainment)
        """
        entity = d.entity
        name = (d.name or getattr(entity, "first_name", "") or "Noma'lum").strip()
        username = getattr(entity, "username", "") or ""
        phone = getattr(entity, "phone", "") or ""
        is_user = isinstance(entity, User)
        is_channel = isinstance(entity, Channel)
        is_chat = isinstance(entity, Chat)
        is_broadcast = getattr(entity, "broadcast", False)
        is_megagroup = getattr(entity, "megagroup", False)
        is_bot = getattr(entity, "bot", False)
        is_self = getattr(entity, "is_self", False)

        # Check key contacts (e.g. Jamshid Sodiqov)
        is_key_contact = False
        key_contact_role = ""
        clean_user = username.lower().lstrip("@")
        clean_phone = re.sub(r'\D', '', phone)

        for kc in key_contacts:
            kc_user = kc.get("username", "").lower().lstrip("@")
            kc_phone = re.sub(r'\D', '', kc.get("phone", ""))
            kc_name = kc.get("name", "").lower()

            if (clean_user and clean_user == kc_user) or \
               (clean_phone and clean_phone in kc_phone) or \
               (kc_name and (kc_name in name.lower() or name.lower() in kc_name)):
                is_key_contact = True
                key_contact_role = kc.get("role", "Yaqin hamkasb va do'st")
                break

        # Extract last message snippet
        last_msg = d.message
        snippet = ""
        media_type = None
        is_media = False
        if last_msg:
            if last_msg.text:
                snippet = last_msg.text.strip().replace("\n", " ")
                if len(snippet) > 85:
                    snippet = snippet[:82] + "..."
            if last_msg.media:
                is_media = True
                media_cls = type(last_msg.media).__name__.lower()
                if "photo" in media_cls:
                    media_type = "photo"
                elif "document" in media_cls or "video" in media_cls:
                    media_type = "media"
                else:
                    media_type = "attachment"
                if not snippet:
                    snippet = f"<{media_type.capitalize()}>"

        msg_date_str = cls._format_date(getattr(last_msg, "date", None) or d.date)

        metadata = {
            "id": d.id,
            "name": name,
            "username": f"@{username}" if username else None,
            "unread_count": d.unread_count,
            "unread_mentions": getattr(d, "unread_mentions_count", 0),
            "is_pinned": getattr(d, "pinned", False),
            "is_key_contact": is_key_contact,
            "key_contact_role": key_contact_role,
            "is_bot": is_bot,
            "is_self": is_self,
            "last_message": snippet,
            "last_message_date": msg_date_str,
            "raw_date": getattr(last_msg, "date", None) or d.date
        }

        # --- CATEGORY TRIAGE DECISION TREE ---

        # 1. Personal & Important (🔴 Shaxsiy & Muhim)
        # Direct user chats, VIP key contacts, or self/saved notes
        if is_user:
            metadata["entity_type"] = "bot" if is_bot else ("self" if is_self else "user")
            return "personal", metadata

        # Check for work/study/chemistry keywords in title or username
        combined_text = f"{name} {username}".lower()
        has_study_keyword = any(k in combined_text for k in cls.WORK_STUDY_KEYWORDS)

        # 2. Hamkasblar va Guruhlar (🟡 Hamkasblar va Guruhlar)
        # All groups, supergroups, and chemistry/study/work channels
        is_group_chat = is_chat or (is_channel and is_megagroup) or d.is_group
        if is_group_chat or (is_channel and has_study_keyword):
            metadata["entity_type"] = "group" if is_group_chat else "study_channel"
            metadata["is_study_work"] = has_study_keyword
            return "groups", metadata

        # 3. Kanallar va E'lonlar (📢 Kanallar va E'lonlar)
        # Public broadcast channels, news, entertainment, commercial ads
        metadata["entity_type"] = "channel"
        return "channels", metadata

    @classmethod
    async def generate_unread_summary_async(cls, limit_dialogs: int = 25) -> UnreadSummaryResult:
        """
        Asynchronously scan dialogs across personal, groups, and channels,
        performing intelligent categorization, unread message triage, and
        executive formatting for Telegram Bot and voice output.
        """
        profile = cls._load_user_profile()
        owner_info = profile.get("owner", {})
        key_contacts = profile.get("key_contacts", [])
        owner_name = owner_info.get("name", "Azizbek Maripov")

        async with cls.session_scope() as c:
            dialogs = await c.get_dialogs(limit=limit_dialogs)

            categories: Dict[str, List[Dict[str, Any]]] = {
                "personal": [],
                "groups": [],
                "channels": []
            }

            total_unread_messages = 0
            unread_dialogs_count = 0

            for d in dialogs:
                cat, meta = cls._classify_dialog(d, key_contacts)
                categories[cat].append(meta)

                if meta["unread_count"] > 0:
                    total_unread_messages += meta["unread_count"]
                    unread_dialogs_count += 1

            # Sort within categories: key contacts and unread first
            categories["personal"].sort(
                key=lambda x: (x["is_key_contact"], not x["is_bot"], x["unread_count"] > 0, x["unread_count"]),
                reverse=True
            )
            categories["groups"].sort(
                key=lambda x: (x["unread_mentions"] > 0, x["unread_count"] > 0, x["unread_count"]),
                reverse=True
            )
            categories["channels"].sort(
                key=lambda x: (x["unread_count"] > 0, x["unread_count"]),
                reverse=True
            )

            # Statistics calculation
            personal_unread = sum(x["unread_count"] for x in categories["personal"])
            personal_unread_chats = sum(1 for x in categories["personal"] if x["unread_count"] > 0)

            groups_unread = sum(x["unread_count"] for x in categories["groups"])
            groups_unread_chats = sum(1 for x in categories["groups"] if x["unread_count"] > 0)

            channels_unread = sum(x["unread_count"] for x in categories["channels"])
            channels_unread_chats = sum(1 for x in categories["channels"] if x["unread_count"] > 0)

            # Separate human vs bot personal messages
            human_unread_items = [
                x for x in categories["personal"]
                if x["unread_count"] > 0 and not x["is_bot"] and not x["is_self"]
            ]
            bot_unread_items = [
                x for x in categories["personal"]
                if x["unread_count"] > 0 and x["is_bot"]
            ]

            # Find Jamshid or key contacts status
            jamshid_item = next(
                (x for x in categories["personal"] if x["is_key_contact"] or "jamshid" in x["name"].lower()),
                None
            )

            # --- BUILD EXECUTIVE TELEGRAM BOT MARKDOWN ---
            now_str = cls._format_date(datetime.now(timezone.utc))
            md_lines = [
                "📬 <b>TELEGRAM XABARLAR SARHISOBI VA TAHLILI</b>",
                f"<i>Foydalanuvchi: {owner_name} | {now_str}</i>",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "📊 <b>Umumiy ko'rsatkichlar:</b>",
                f"• Jami o'qilmagan xabarlar: <b>{total_unread_messages:,} ta</b> ({unread_dialogs_count} ta suhbatda)",
                f"• 🔴 Shaxsiy & Muhim: <b>{personal_unread} ta</b> ({personal_unread_chats} ta chat)",
                f"• 🟡 Hamkasblar & Guruhlar: <b>{groups_unread:,} ta</b> ({groups_unread_chats} ta guruh/kanal)",
                f"• 📢 Kanallar & E'lonlar: <b>{channels_unread:,} ta</b> ({channels_unread_chats} ta kanal)",
                ""
            ]

            # Section 1: 🔴 Shaxsiy & Muhim
            md_lines.append("🔴 <b>SHAXSIY VA MUHIM (Shaxsiy muloqotlar)</b>")
            md_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            if human_unread_items:
                for p in human_unread_items:
                    badge = f" ⭐ <i>[{html.escape(str(p['key_contact_role']))}]</i>" if p["is_key_contact"] else ""
                    uname = f" ({html.escape(str(p['username']))})" if p["username"] else ""
                    esc_p_name = html.escape(str(p['name']))
                    md_lines.append(f"• 👤 <b>{esc_p_name}</b>{uname}{badge} — <b>{p['unread_count']} ta yangi</b> ({p['last_message_date']})")
                    if p["last_message"]:
                        md_lines.append(f"  💬 <i>\"{html.escape(str(p['last_message']))}\"</i>")
            else:
                md_lines.append("✅ <i>Yangi o'qilmagan shaxsiy xabarlar yo'q (barcha shaxsiy xatlar o'qilgan).</i>")

            # Always display Jamshid / Key Contact status
            if jamshid_item:
                j_unread_note = f" — <b>{jamshid_item['unread_count']} ta yangi</b>" if jamshid_item["unread_count"] > 0 else ""
                esc_j_name = html.escape(str(jamshid_item['name']))
                esc_j_uname = html.escape(str(jamshid_item.get('username', '@Jamsh1d_Sodiqov')))
                esc_j_msg = html.escape(str(jamshid_item.get('last_message', '')))
                md_lines.append(
                    f"📌 <b>Yaqin hamkasb: {esc_j_name}</b> ({esc_j_uname}) ⭐{j_unread_note}\n"
                    f"   💬 Oxirgi xabar: <i>\"{esc_j_msg}\"</i> ({jamshid_item['last_message_date']})"
                )

            # Display bot unreads if any
            if bot_unread_items:
                for b in bot_unread_items:
                    esc_b_name = html.escape(str(b['name']))
                    esc_b_uname = html.escape(str(b.get('username', '')))
                    md_lines.append(f"• 🤖 <b>{esc_b_name}</b> ({esc_b_uname}) [Bot/Tizim] — <b>{b['unread_count']} ta bildirishnoma</b>")
                    if b["last_message"]:
                        md_lines.append(f"  💬 <i>\"{html.escape(str(b['last_message']))}\"</i>")
            md_lines.append("")

            # Section 2: 🟡 Hamkasblar va Guruhlar
            md_lines.append("🟡 <b>HAMKASBLAR VA GURUHLAR (Ta'lim & Kimyo)</b>")
            md_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            unread_groups = [x for x in categories["groups"] if x["unread_count"] > 0]
            if unread_groups:
                for g in unread_groups[:7]:
                    mention_badge = " 🔔 <b>[@Sizga murojaat]</b>" if g["unread_mentions"] > 0 else ""
                    esc_g_name = html.escape(str(g['name']))
                    md_lines.append(f"• 👥 <b>{esc_g_name}</b>{mention_badge} — <b>{g['unread_count']:,} ta yangi</b> ({g['last_message_date']})")
                    if g["last_message"]:
                        md_lines.append(f"  💬 <i>\"{html.escape(str(g['last_message']))}\"</i>")
                if len(unread_groups) > 7:
                    remaining_g = sum(x["unread_count"] for x in unread_groups[7:])
                    md_lines.append(f"• <i>... yana {len(unread_groups) - 7} ta ta'lim guruhida ({remaining_g:,} ta xabar)</i>")
            else:
                md_lines.append("✅ <i>Hamkasblar va guruhlarda yangi o'qilmagan xabarlar yo'q.</i>")
            md_lines.append("")

            # Section 3: 📢 Kanallar va E'lonlar
            md_lines.append("📢 <b>KANALLAR VA E'LONLAR (Yangiliklar & Ommaviy)</b>")
            md_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            unread_channels = [x for x in categories["channels"] if x["unread_count"] > 0]
            if unread_channels:
                for c_item in unread_channels[:4]:
                    esc_c_name = html.escape(str(c_item['name']))
                    md_lines.append(f"• 📢 <b>{esc_c_name}</b> — <b>{c_item['unread_count']:,} ta yangi</b>")
                    if c_item["last_message"]:
                        md_lines.append(f"  💬 <i>\"{html.escape(str(c_item['last_message']))}\"</i>")
                if len(unread_channels) > 4:
                    rem_c = sum(x["unread_count"] for x in unread_channels[4:])
                    md_lines.append(f"• <i>... va boshqa {len(unread_channels) - 4} ta kanalda jami {rem_c:,} ta e'lon/yangilik.</i>")
            else:
                md_lines.append("✅ <i>Kanallarda yangi e'lonlar yo'q.</i>")
            md_lines.append("")

            # Monika Advice
            if human_unread_items:
                advice = "🔴 Diqqat: Yangi shaxsiy xabarlar bor. Zudlik bilan ko'rib chiqish tavsiya etiladi."
            elif groups_unread > 0:
                active_g_names = ", ".join(f"'{x['name']}'" for x in unread_groups[:2])
                advice = f"Shaxsiy xabarlar tinch. Kimyo va ta'lim guruhlarida ({active_g_names}) yangi muhokamalar mavjud."
            else:
                advice = "Barcha muloqotlar holati yaxshi, shoshilinch xabarlar yo'q."

            md_lines.append(f"💡 <b>Monika Tahlili:</b> <i>{advice}</i>")
            markdown_text = "\n".join(md_lines)

            # --- BUILD CONCISE SPOKEN VOICE SUMMARY ---
            voice_parts = []
            first_name = owner_name.split()[0]
            voice_parts.append(f"{first_name}, Telegramingizda jami {total_unread_messages:,} ta o'qilmagan xabar mavjud.")

            if human_unread_items:
                h_name = human_unread_items[0]["name"]
                voice_parts.append(f"Muhim: shaxsiy xabarlaringiz bor, jumladan {h_name}dan.")
            else:
                if jamshid_item and jamshid_item.get("last_message"):
                    voice_parts.append(f"Shaxsiy xabarlarda yangi xat yo'q, Jamshid Sodiqovdan oxirgi xabar: '{jamshid_item['last_message']}'.")
                else:
                    voice_parts.append("Shaxsiy muloqotlaringizda yangi xabarlar yo'q.")

            if bot_unread_items:
                voice_parts.append(f"MyJarvis botidan esa {bot_unread_items[0]['unread_count']} ta bildirishnoma kelgan.")

            if groups_unread > 0:
                sample_groups = [x["name"] for x in unread_groups[:2]]
                g_str = " va ".join(sample_groups) if sample_groups else "guruhlar"
                voice_parts.append(f"Hamkasblar va kimyo guruhlarida, xususan {g_str}da yangi muhokamalar bor.")

            if channels_unread > 0:
                voice_parts.append(f"Ommaviy kanallarda esa {channels_unread:,} ta e'lon va yangiliklar to'plangan.")

            voice_summary_text = " ".join(voice_parts)

            result = UnreadSummaryResult({
                "success": True,
                "markdown": markdown_text,
                "voice_summary": voice_summary_text,
                "stats": {
                    "total_unread_messages": total_unread_messages,
                    "total_unread_chats": unread_dialogs_count,
                    "dialogs_scanned": len(dialogs),
                    "personal_unread_messages": personal_unread,
                    "personal_unread_chats": personal_unread_chats,
                    "human_unread_chats": len(human_unread_items),
                    "groups_unread_messages": groups_unread,
                    "groups_unread_chats": groups_unread_chats,
                    "channels_unread_messages": channels_unread,
                    "channels_unread_chats": channels_unread_chats,
                },
                "categories": categories,
                "timestamp": now_str
            })

            return result

    @classmethod
    def generate_unread_summary(cls, limit_dialogs: int = 25) -> UnreadSummaryResult:
        """
        Synchronous entry point to scan and categorize unread messages across
        all chats (personal, groups, channels).
        """
        return cls._run_sync(cls.generate_unread_summary_async(limit_dialogs=limit_dialogs))

    @classmethod
    def send_telegram_message(cls, target_name_or_username: str, message_text: str) -> Dict[str, Any]:
        """Send a message to a contact, @username, or Saved Messages."""
        async def _run():
            async with cls.session_scope() as c:
                target = target_name_or_username.strip()
                clean_target = re.sub(r'(?:ga|ka|qa)$', '', target, flags=re.IGNORECASE).strip() or target

                dest_entity = None
                dest_name = clean_target

                if clean_target.lower() in ("me", "o'zim", "saved", "saved messages"):
                    dest_entity = "me"
                    dest_name = "Saved Messages"
                elif clean_target.startswith("@") or clean_target.startswith("+") or (clean_target.isdigit() and len(clean_target) > 5):
                    try:
                        dest_entity = await c.get_input_entity(clean_target)
                    except Exception:
                        pass

                if not dest_entity:
                    contacts_res = await c(GetContactsRequest(hash=0))
                    for u in contacts_res.users:
                        first = u.first_name or ""
                        last = u.last_name or ""
                        full = f"{first} {last}".strip().lower()
                        uname = (u.username or "").lower()
                        if clean_target.lower() in full or clean_target.lower() in uname:
                            dest_entity = u
                            dest_name = f"{first} {last}".strip()
                            if u.username:
                                dest_name += f" (@{u.username})"
                            break

                if not dest_entity:
                    return {"success": False, "error": f"Kontaktlar ro'yxatidan '{clean_target}' topilmadi."}

                sent = await c.send_message(dest_entity, message_text)
                return {
                    "success": True,
                    "target": dest_name,
                    "message_id": sent.id,
                    "text": message_text
                }

        return cls._run_sync(_run())

    @classmethod
    def send_telegram_file(cls, target_name_or_username: str, file_path: str, caption: str = "") -> Dict[str, Any]:
        """Send a physical file or document to a contact, @username, or Saved Messages."""
        async def _run():
            async with cls.session_scope() as c:
                target = target_name_or_username.strip()
                clean_target = re.sub(r'(?:ga|ka|qa)$', '', target, flags=re.IGNORECASE).strip() or target

                dest_entity = None
                dest_name = clean_target

                if clean_target.lower() in ("me", "o'zim", "saved", "saved messages"):
                    dest_entity = "me"
                    dest_name = "Saved Messages"
                elif clean_target.startswith("@") or clean_target.startswith("+") or (clean_target.isdigit() and len(clean_target) > 5):
                    try:
                        dest_entity = await c.get_input_entity(clean_target)
                    except Exception:
                        pass

                if not dest_entity:
                    contacts_res = await c(GetContactsRequest(hash=0))
                    for u in contacts_res.users:
                        first = u.first_name or ""
                        last = u.last_name or ""
                        full = f"{first} {last}".strip().lower()
                        uname = (u.username or "").lower()
                        if clean_target.lower() in full or clean_target.lower() in uname:
                            dest_entity = u
                            dest_name = f"{first} {last}".strip()
                            if u.username:
                                dest_name += f" (@{u.username})"
                            break

                if not dest_entity:
                    return {"success": False, "error": f"Kontaktlar ro'yxatidan '{clean_target}' topilmadi."}

                p = Path(file_path)
                if not p.exists():
                    return {"success": False, "error": f"Fayl topilmadi: {file_path}"}

                ext = p.suffix.lower()
                is_audio = ext in (".mp3", ".wav", ".m4a", ".ogg", ".flac")
                is_video = ext in (".mp4", ".mkv", ".avi", ".mov")
                if is_audio:
                    sent = await c.send_file(dest_entity, str(p), caption=caption, voice=False)
                elif is_video:
                    sent = await c.send_file(dest_entity, str(p), caption=caption, supports_streaming=True)
                else:
                    sent = await c.send_file(dest_entity, str(p), caption=caption)
                return {
                    "success": True,
                    "target": dest_name,
                    "message_id": sent.id,
                    "file_path": str(p),
                    "file_name": p.name
                }

        return cls._run_sync(_run())

    @classmethod
    def delete_telegram_messages(cls, target_name_or_username: str, message_ids: List[int], revoke: bool = True) -> Dict[str, Any]:
        """Delete specific messages in a chat or conversation for both sides."""
        async def _run():
            async with cls.session_scope() as c:
                target = target_name_or_username.strip()
                clean_target = re.sub(r'(?:ga|ka|qa)$', '', target, flags=re.IGNORECASE).strip() or target

                dest_entity = None
                if clean_target.startswith("@") or clean_target.startswith("+") or (clean_target.isdigit() and len(clean_target) > 5):
                    try:
                        dest_entity = await c.get_input_entity(clean_target)
                    except Exception:
                        pass

                if not dest_entity:
                    contacts_res = await c(GetContactsRequest(hash=0))
                    for u in contacts_res.users:
                        first = u.first_name or ""
                        last = u.last_name or ""
                        full = f"{first} {last}".strip().lower()
                        uname = (u.username or "").lower()
                        if clean_target.lower() in full or clean_target.lower() in uname:
                            dest_entity = u
                            break

                if not dest_entity:
                    return {"success": False, "error": f"Kontakt topilmadi: {clean_target}"}

                await c.delete_messages(dest_entity, message_ids, revoke=revoke)
                return {"success": True, "deleted_ids": message_ids}

        return cls._run_sync(_run())

    @classmethod
    def transfer_chat_media(cls, source_chat: str, recipient: str, media_type: str = "any", caption: Optional[str] = None) -> Dict[str, Any]:
        """Transfer or forward the latest media (audio, video, photo, document) from source_chat to recipient."""
        async def _run():
            async with cls.session_scope() as c:
                src_clean = source_chat.strip()
                src_entity = None

                # Check well-known aliases (e.g. vkm, vkmusic, vkm bot)
                if any(w in src_clean.lower() for w in ["vkm", "vkmusic", "vkm bot"]):
                    for cand in ["@vkmusic_bot", "@vkm_bot", "@vkmbot", "@vekaembot"]:
                        try:
                            src_entity = await c.get_entity(cand)
                            break
                        except Exception:
                            pass
                elif src_clean.lower() in ("me", "saved", "saved messages", "o'zim"):
                    src_entity = "me"
                elif src_clean.startswith("@") or src_clean.startswith("+") or (src_clean.isdigit() and len(src_clean) > 5):
                    try:
                        src_entity = await c.get_entity(src_clean)
                    except Exception:
                        pass

                if not src_entity:
                    dialogs = await c.get_dialogs(limit=50)
                    for d in dialogs:
                        if src_clean.lower() in (d.name or "").lower():
                            src_entity = d.entity
                            break

                if not src_entity:
                    return {"success": False, "error": f"Manba chat topilmadi: '{source_chat}'"}

                # Resolve destination
                dest_clean = re.sub(r'(?:ga|ka|qa)$', '', recipient, flags=re.IGNORECASE).strip() or recipient
                dest_entity = None
                dest_name = dest_clean
                if dest_clean.lower() in ("me", "saved", "saved messages", "o'zim"):
                    dest_entity = "me"
                    dest_name = "Saved Messages"
                elif dest_clean.startswith("@") or dest_clean.startswith("+") or (dest_clean.isdigit() and len(dest_clean) > 5):
                    try:
                        dest_entity = await c.get_input_entity(dest_clean)
                    except Exception:
                        pass

                if not dest_entity:
                    contacts_res = await c(GetContactsRequest(hash=0))
                    for u in contacts_res.users:
                        first = u.first_name or ""
                        last = u.last_name or ""
                        full = f"{first} {last}".strip().lower()
                        uname = (u.username or "").lower()
                        if dest_clean.lower() in full or dest_clean.lower() in uname:
                            dest_entity = u
                            dest_name = f"{first} {last}".strip()
                            if u.username:
                                dest_name += f" (@{u.username})"
                            break

                if not dest_entity:
                    return {"success": False, "error": f"Qabul qiluvchi kontakt topilmadi: '{dest_clean}'"}

                # Search matching media in source
                target_msg = None
                clean_type = media_type.lower()
                async for m in c.iter_messages(src_entity, limit=40):
                    if not m.media:
                        continue
                    m_type_name = type(m.media).__name__.lower()
                    if any(w in clean_type for w in ["audio", "musiqa", "qo'shiq", "qóshiq", "music", "mp3"]):
                        if hasattr(m.media, "document"):
                            for attr in getattr(m.media.document, "attributes", []):
                                if "audio" in type(attr).__name__.lower():
                                    target_msg = m
                                    break
                        if target_msg:
                            break
                    elif any(w in clean_type for w in ["video", "klip", "mp4"]):
                        if "video" in m_type_name:
                            target_msg = m
                            break
                        if hasattr(m.media, "document"):
                            for attr in getattr(m.media.document, "attributes", []):
                                if "video" in type(attr).__name__.lower():
                                    target_msg = m
                                    break
                        if target_msg:
                            break
                    elif any(w in clean_type for w in ["photo", "rasm", "surat"]):
                        if "photo" in m_type_name:
                            target_msg = m
                            break
                    elif any(w in clean_type for w in ["document", "fayl", "hujjat"]):
                        if "document" in m_type_name:
                            target_msg = m
                            break
                    else:
                        target_msg = m
                        break

                if not target_msg:
                    return {"success": False, "error": f"'{source_chat}' chatidan so'ralgan turdagi ({media_type}) media topilmadi."}

                final_caption = caption or target_msg.text or ""
                sent = await c.send_file(dest_entity, target_msg.media, caption=final_caption)
                return {
                    "success": True,
                    "target": dest_name,
                    "message_id": sent.id,
                    "source": getattr(src_entity, "title", None) or getattr(src_entity, "username", str(src_entity))
                }

        return cls._run_sync(_run())


    @classmethod
    def save_note_to_me(cls, note_text: str) -> Dict[str, Any]:
        """Save a quick note or reminder to Telegram 'Saved Messages'."""
        return cls.send_telegram_message("me", note_text)

    @classmethod
    def search_messages(cls, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Search across all messages in personal Telegram."""
        async def _run():
            async with cls.session_scope() as c:
                results = []
                async for m in c.iter_messages(None, search=query, limit=limit):
                    sender = await m.get_sender()
                    sender_name = getattr(sender, "first_name", "Unknown") if sender else "Unknown"
                    results.append({
                        "id": m.id,
                        "date": str(m.date),
                        "sender": sender_name,
                        "text": m.text or ("<Media>" if m.media else "")
                    })
                return results

        return cls._run_sync(_run())

    @classmethod
    def get_recent_dialogs(cls, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetch list of recent conversations/chats."""
        async def _run():
            async with cls.session_scope() as c:
                dialogs = await c.get_dialogs(limit=limit)
                res = []
                for d in dialogs:
                    last_msg = d.message.text if d.message and d.message.text else ("<Media>" if d.message and d.message.media else "")
                    res.append({
                        "name": d.name,
                        "id": d.id,
                        "unread": d.unread_count,
                        "last_message": last_msg[:60]
                    })
                return res

        return cls._run_sync(_run())


if __name__ == "__main__":
    print("=== TESTING TELETHON BRIDGE & UNREAD SUMMARY ===")
    summary = JarvisUserTelegram.generate_unread_summary(limit_dialogs=25)
    print("\n[MARKDOWN OUTPUT]")
    print(summary.get_markdown())
    print("\n[VOICE SUMMARY OUTPUT]")
    print(summary.get_voice())
    print("\n[STATS]")
    print(json.dumps(summary["stats"], indent=2))

"""
JARVIS Uzbek Cognitive NLP & Ambiguity Resolution Engine
=========================================================
Provides intelligent natural language preprocessing, contextual typo correction,
semantic synonym mapping, and cognitive ambiguity detection for JARVIS (Monika).

Core Components:
1. `UzbekNLPPreprocessor`:
   - Normalizes Uzbek diacritics (ó, o‘, o’, oʻ -> o'; gʻ, g‘, g’ -> g').
   - Contextually corrects spoken dialect and keyboard typos (bosh -> bo'sh in storage contexts; notbook -> noutbuk; xozir -> hozir).
   - Maps semantic synonyms for storage metrics, wallpapers, media words, and system actions.
2. `AmbiguityEngine`:
   - Parses intent and extracts entity slots.
   - Detects ambiguity (AMBIGUOUS_STORAGE_METRIC, AMBIGUOUS_WALLPAPER, MISSING_SLOT).
   - Formulates natural, respectful clarifying questions with interactive inline button options.
   - Resolves clarification choices into actionable system intents.
"""

import re
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("JarvisNLP")


@dataclass
class AmbiguityResult:
    """Structured result representing parsed query intent, entities, and ambiguity status."""
    raw_query: str
    normalized_query: str
    intent: str
    entities: Dict[str, Any] = field(default_factory=dict)
    is_ambiguous: bool = False
    ambiguity_type: str = "NONE"  # AMBIGUOUS_STORAGE_METRIC, AMBIGUOUS_WALLPAPER, MISSING_SLOT, NONE
    missing_slots: List[str] = field(default_factory=list)
    clarification_needed: bool = False
    clarification_question: Optional[str] = None
    clarification_options: Optional[List[Dict[str, str]]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert result to dictionary representation."""
        return {
            "raw_query": self.raw_query,
            "normalized_query": self.normalized_query,
            "intent": self.intent,
            "entities": self.entities,
            "is_ambiguous": self.is_ambiguous,
            "ambiguity_type": self.ambiguity_type,
            "missing_slots": self.missing_slots,
            "clarification_needed": self.clarification_needed,
            "clarification_question": self.clarification_question,
            "clarification_options": self.clarification_options or [],
        }


class UzbekNLPPreprocessor:
    """Preprocesses and normalizes Uzbek natural language queries.
    
    Handles diacritics, spoken/colloquial variants, contextual typo corrections,
    and semantic token classifications.
    """

    # Diacritics mapping for Uzbek Latin
    # Converts all non-standard apostrophes and accented vowels to standard standard forms: o', g'
    DIACRITIC_REPLACEMENTS = [
        # O' variants (ó, ò, õ, ö, ô, oʻ, o‘, o’, o`, o´)
        (r'[oO][ʻ‘’`´\u02BB\u2018\u2019\u0060\u00B4]', "o'"),
        (r'[óòõöôÓÒÕÖÔ]', "o'"),
        # G' variants (gʻ, g‘, g’, g`, g´, ğ, ġ)
        (r'[gG][ʻ‘’`´\u02BB\u2018\u2019\u0060\u00B4]', "g'"),
        (r'[ğġĞĠ]', "g'"),
        # General apostrophe normalization inside words (e.g. ma'lumot, ta'sir, san'at)
        (r'[ʻ‘’`´\u02BB\u2018\u2019\u0060\u00B4]', "'"),
    ]

    # Contextual Typo & Colloquial Vocabulary with stem and suffix preservation
    STEM_TYPOS = [
        # notbook / notbuk / nowtbuk / netbuk / leptop / laptop -> noutbuk
        (r'\b(?:notbook|notbuk|nowtbuk|netbuk|leptop|laptop)([a-zA-Z\']*)', r'noutbuk\1'),
        # xozir / xozirgi / xozirda -> hozir...
        (r'\bxozir([a-zA-Z\']*)', r'hozir\1'),
        # kampyuter / kamputer -> kompyuter...
        (r'\b(?:kampyuter|kamputer)([a-zA-Z\']*)', r'kompyuter\1'),
        (r'\bkomp(?!yuter)([a-zA-Z\']*)', r'kompyuter\1'),
        # telefram / telegam -> telegram...
        (r'\b(?:telefram|telegam)([a-zA-Z\']*)', r'telegram\1'),
        (r'\btg\b', 'telegram'),
        # skrin (not skrinshot) -> skrinshot...
        (r'\bskrin(?!shot)([a-zA-Z\']*)', r'skrinshot\1'),
        (r'\bscreenshot([a-zA-Z\']*)', r'skrinshot\1'),
        # rasimi -> rasmi
        (r'\brasimi\b', 'rasmi'),
        (r'\brasim\b', 'rasm'),
        # koshiq / qoshiq -> qo'shiq...
        (r'\b(?:koshiq|qoshiq)([a-zA-Z\']*)', r"qo'shiq\1"),
        # musika -> musiqa
        (r'\bmusika([a-zA-Z\']*)', r'musiqa\1'),
    ]

    # Semantic Synonym Dictionaries
    STORAGE_RAM_SYNONYMS = {
        "ram", "tezkor xotira", "ozu", "operativka", "operativ xotira",
        "operativkasi", "ramni", "ramda", "ozuda", "operativ"
    }

    STORAGE_DISK_SYNONYMS = {
        "disk", "ssd", "hdd", "qattiq disk", "fleshka", "flesh",
        "diskdagi joy", "xotira bo'sh joyi", "c disk", "d disk",
        "diskda", "ssdda", "hddda", "vinchester", "disklar"
    }

    WALLPAPER_SYNONYMS = {
        "oboy", "fon rasmi", "wallpaper", "rabochiy stol foni",
        "ish stoli foni", "ekran foni", "stol foni", "fon rasmini",
        "fon rasmi qanday", "stol fonidagi rasm"
    }

    DESKTOP_FOLDER_SYNONYMS = {
        "rabochiy stol", "ish stoli", "desktop", "rabochiy stoldagi",
        "ish stolidagi", "desktop papkasi", "ish stoli papkasi"
    }

    MEDIA_WORDS = {
        "audio": ["qo'shiq", "musiqa", "mp3", "trek", "treka", "audio", "ashula"],
        "video": ["video", "klip", "rolik", "film", "kino"],
        "image": ["rasm", "foto", "surat", "screenshot", "skrinshot", "tasvir", "tasviri"],
        "document": ["dokument", "hujjat", "fayl", "pdf", "doc", "docx", "txt", "excel", "jadval"],
    }

    def __init__(self):
        pass

    @classmethod
    def normalize_diacritics(cls, text: str) -> str:
        """Normalize varied Uzbek diacritical marks (ó, o‘, oʻ, gʻ -> o', g')."""
        if not text:
            return ""
        norm = text
        for pattern, repl in cls.DIACRITIC_REPLACEMENTS:
            norm = re.sub(pattern, repl, norm)
        return norm

    @classmethod
    def correct_typos(cls, text: str) -> str:
        """Contextually correct common spoken words, typos, and homophones."""
        if not text:
            return ""
        
        t = text

        # 1. Contextual 'bosh' -> 'bo'sh' when followed or preceded by storage words
        # (e.g., 'bosh joy', 'bosh xotira', 'qancha bosh joy qoldi')
        storage_context_words = r"(?:xotira|joy|disk|ssd|hdd|fleshka|megabayt|gigabayt|gb|mb)"
        t = re.sub(rf"\bbosh\s+({storage_context_words})\b", r"bo'sh \1", t, flags=re.IGNORECASE)
        t = re.sub(rf"\b({storage_context_words})\s+bosh\b", r"\1 bo'sh", t, flags=re.IGNORECASE)
        t = re.sub(r"\bqancha\s+bosh\b", "qancha bo'sh", t, flags=re.IGNORECASE)
        t = re.sub(r"\bbosh\s+qolgan\b", "bo'sh qolgan", t, flags=re.IGNORECASE)

        # 2. Lexical & Stem corrections
        for pattern, replacement in cls.STEM_TYPOS:
            t = re.sub(pattern, replacement, t, flags=re.IGNORECASE)

        # 3. Multiple whitespace cleanup
        t = re.sub(r'\s+', ' ', t).strip()
        return t

    @classmethod
    def preprocess(cls, text: str) -> str:
        """Run full preprocessing pipeline: normalize diacritics + correct typos."""
        if not text:
            return ""
        cleaned = text.strip()
        cleaned = cls.normalize_diacritics(cleaned)
        cleaned = cls.correct_typos(cleaned)
        return cleaned

    @classmethod
    def classify_storage_metric(cls, text: str) -> str:
        """Classify storage query into 'RAM', 'DISK', 'BOTH', 'AMBIGUOUS', or 'NONE'.
        
        Returns:
            'RAM': explicitly asks about RAM/operativka/OZU.
            'DISK': explicitly asks about SSD/HDD/disk free space.
            'BOTH': mentions both or asks for all memory metrics.
            'AMBIGUOUS': asks generically for 'xotira' without specifying RAM or Disk.
            'NONE': not a storage metric inquiry.
        """
        low = text.lower()

        # Robust stem + suffix matching for RAM
        has_ram = bool(re.search(
            r"\b(?:ram|operativka|operativ|ozu|tezkor\s+xotira)(?:[a-zA-Z\']*)\b", low
        )) or any(w in low for w in ["tezkor xotira", "operativ xotira"])

        # Robust stem + suffix matching for explicit DISK
        has_explicit_disk = bool(re.search(
            r"\b(?:disk|ssd|hdd|qattiq\s+disk|fleshka|flesh|vinchester)(?:[a-zA-Z\']*)\b", low
        )) or any(w in low for w in ["qattiq disk", "c disk", "d disk"])

        has_generic_space = any(w in low for w in ["bo'sh joy", "bosh joy", "xotira bo'sh joyi"])
        has_disk = has_explicit_disk or (has_generic_space and not has_ram)

        if has_ram and has_disk:
            return "BOTH"
        if has_ram:
            return "RAM"
        if has_disk:
            return "DISK"

        # Check for generic "xotira" or "joy" with potential suffixes
        has_generic_memory = bool(re.search(r"\b(xotira[a-zA-Z\']*|joy[a-zA-Z\']*)\b", low))
        is_storage_inquiry = bool(re.search(r"\b(qancha|qanday|holat|tekshir|bilib ber|korsat|ko'rsat|qoldi|bor)\b", low))

        if has_generic_memory and (is_storage_inquiry or len(low.split()) <= 3):
            return "AMBIGUOUS"

        return "NONE"

    @classmethod
    def classify_wallpaper_or_desktop(cls, text: str) -> str:
        """Classify desktop/wallpaper queries into 'WALLPAPER', 'DESKTOP_FILE', 'AMBIGUOUS', or 'NONE'.
        
        Returns:
            'WALLPAPER': explicitly asks for wallpaper background (e.g. 'oboy', 'wallpaper', 'fon rasmi').
            'DESKTOP_FILE': explicitly asks for files inside desktop folder.
            'AMBIGUOUS': mentions desktop and picture/background ambiguously (e.g. 'rabochiy stol fonidagi rasm').
            'NONE': unrelated.
        """
        low = text.lower()

        # Phrases like 'rabochiy stol fonidagi rasm', 'ish stoli foni', or 'rabochiy stol foni'
        # are inherently ambiguous between wallpaper background vs desktop folder image.
        is_ambiguous_wallpaper = bool(re.search(
            r"\b(rabochiy\s+stol|ish\s+stoli)\s+(?:fonidagi|foni|fon)\s*(?:rasm|surat)?[a-zA-Z\']*\b", low
        )) or bool(re.search(
            r"\b(rabochiy\s+stol|ish\s+stoli)\s+foni[a-zA-Z\']*\b", low
        )) or (
            bool(re.search(r"\b(rabochiy\s+stol|ish\s+stoli)\b", low)) and
            bool(re.search(r"\b(fon|foni|fonidagi)\b", low))
        )
        if is_ambiguous_wallpaper:
            return "AMBIGUOUS"

        # Desktop folder requests
        has_desktop_folder = bool(re.search(r"\b(rabochiy\s+stoldagi|ish\s+stolidagi|desktop\s+papkasidagi)\s+(?:fayl|hujjat|rasm)[a-zA-Z\']*\b", low))
        if has_desktop_folder:
            return "DESKTOP_FILE"

        # Direct explicit wallpaper requests
        has_wallpaper_direct = bool(re.search(r"\b(?:oboy|wallpaper|fon\s+rasmi|ekran\s+foni)[a-zA-Z\']*\b", low))
        if has_wallpaper_direct:
            return "WALLPAPER"

        return "NONE"

    @classmethod
    def extract_media_type(cls, text: str) -> Optional[str]:
        """Extract media type: 'audio', 'video', 'image', 'document', or None."""
        low = text.lower()
        for cat, keywords in cls.MEDIA_WORDS.items():
            for kw in keywords:
                if re.search(rf"\b{re.escape(kw)}[a-zA-Z\']*\b", low):
                    return cat
        return None


class AmbiguityEngine:
    """Cognitive intent and ambiguity engine for Monika (JARVIS).
    
    Detects when a user query has multiple interpretations or lacks critical slot parameters,
    and constructs user-friendly clarification requests with interactive options.
    """

    def __init__(self, preprocessor: Optional[UzbekNLPPreprocessor] = None):
        self.preprocessor = preprocessor or UzbekNLPPreprocessor()

    def analyze(self, raw_query: str) -> AmbiguityResult:
        """Analyze a raw query for intent, entities, and cognitive ambiguities.
        
        Args:
            raw_query: The incoming natural language text command from user.
            
        Returns:
            AmbiguityResult containing normalization, intent, ambiguity status,
            and clarification questions/options if applicable.
        """
        if not raw_query or not raw_query.strip():
            return AmbiguityResult(
                raw_query=raw_query or "",
                normalized_query="",
                intent="EMPTY",
                is_ambiguous=False,
                ambiguity_type="NONE"
            )

        norm = self.preprocessor.preprocess(raw_query)
        low = norm.lower()

        # ---------------------------------------------------------------------
        # 1. Storage Metric Ambiguity Check (RAM vs DISK)
        # ---------------------------------------------------------------------
        storage_type = self.preprocessor.classify_storage_metric(norm)
        if storage_type == "AMBIGUOUS":
            return AmbiguityResult(
                raw_query=raw_query,
                normalized_query=norm,
                intent="INQUIRE_STORAGE",
                entities={"metric": "unspecified", "target": "memory"},
                is_ambiguous=True,
                ambiguity_type="AMBIGUOUS_STORAGE_METRIC",
                missing_slots=["storage_metric"],
                clarification_needed=True,
                clarification_question="Azizbek, 'xotira' deganda tezkor xotira (RAM)ni nazarda tutyapsizmi yoki disk (SSD)dagi bo'sh joynimi?",
                clarification_options=[
                    {"text": "⚡ RAM (Tezkor xotira)", "callback_data": "clarify:ram"},
                    {"text": "💾 Disk (SSD)", "callback_data": "clarify:disk"},
                    {"text": "📊 Ikkalasini ham", "callback_data": "clarify:storage_both"},
                ]
            )
        elif storage_type in ("RAM", "DISK", "BOTH"):
            return AmbiguityResult(
                raw_query=raw_query,
                normalized_query=norm,
                intent="INQUIRE_STORAGE",
                entities={"metric": storage_type.lower()},
                is_ambiguous=False,
                ambiguity_type="NONE",
                clarification_needed=False
            )

        # ---------------------------------------------------------------------
        # 2. Wallpaper vs Desktop File Ambiguity Check
        # ---------------------------------------------------------------------
        wallpaper_type = self.preprocessor.classify_wallpaper_or_desktop(norm)
        if wallpaper_type == "AMBIGUOUS":
            return AmbiguityResult(
                raw_query=raw_query,
                normalized_query=norm,
                intent="GET_WALLPAPER_OR_FILE",
                entities={"target": "desktop_image"},
                is_ambiguous=True,
                ambiguity_type="AMBIGUOUS_WALLPAPER",
                missing_slots=["item_type"],
                clarification_needed=True,
                clarification_question="Azizbek, kompyuteringiz ish stoli fon rasmini (Wallpaper) yuboraymi yoki Ish stoli (Desktop) papkasidagi biror rasm faylinimi?",
                clarification_options=[
                    {"text": "🖼️ Fon rasmi (Wallpaper)", "callback_data": "clarify:wallpaper"},
                    {"text": "📁 Ish stolidagi fayl", "callback_data": "clarify:desktop_file"},
                ]
            )
        elif wallpaper_type == "WALLPAPER":
            return AmbiguityResult(
                raw_query=raw_query,
                normalized_query=norm,
                intent="GET_WALLPAPER",
                entities={"target": "wallpaper"},
                is_ambiguous=False,
                ambiguity_type="NONE",
                clarification_needed=False
            )
        elif wallpaper_type == "DESKTOP_FILE":
            return AmbiguityResult(
                raw_query=raw_query,
                normalized_query=norm,
                intent="GET_DESKTOP_FILE",
                entities={"target": "desktop_file"},
                is_ambiguous=False,
                ambiguity_type="NONE",
                clarification_needed=False
            )

        # ---------------------------------------------------------------------
        # 3. Missing Slots & Anaphoric Forwarding Commands
        # ---------------------------------------------------------------------
        # Check: Anaphoric File Forwarding (e.g. "Endi uni xurshidga jónat", "uni @XuRsHiDuLLoH ga yubor", "buni yubor")
        is_anaphora_forward = bool(re.search(r"\b(uni|buni|o'shani|oshani|oxirgisini|oxirgi\s+skrinshotni|oxirgi\s+rasmni)\b", low)) and \
                              bool(re.search(r"\b(yubor|jo'nat|tashla|jonat|yetkaz|send)\b", low))

        if is_anaphora_forward:
            has_at_recipient = bool(re.search(r'(@[a-zA-Z0-9_]{3,})', norm))
            m_cand_recipient = re.search(r'\b([a-zA-Z0-9_\']+?)(?:ga|ka|qa)\b', low)
            cand_recipient = m_cand_recipient.group(1).lower() if m_cand_recipient else ""
            reserved_recipients = {"telegram", "bot", "monika", "menga", "bizga", "unga", "bunga", "odam", "kontakt", "kim", "fayl"}
            has_valid_name_recipient = bool(cand_recipient and cand_recipient not in reserved_recipients)

            if has_at_recipient or has_valid_name_recipient:
                return AmbiguityResult(
                    raw_query=raw_query,
                    normalized_query=norm,
                    intent="FORWARD_LAST_FILE",
                    entities={"target": cand_recipient or "recipient"},
                    is_ambiguous=False,
                    ambiguity_type="NONE",
                    clarification_needed=False
                )
            else:
                return AmbiguityResult(
                    raw_query=raw_query,
                    normalized_query=norm,
                    intent="FORWARD_LAST_FILE",
                    entities={},
                    is_ambiguous=True,
                    ambiguity_type="MISSING_SLOT",
                    missing_slots=["recipient"],
                    clarification_needed=True,
                    clarification_question="Azizbek, oxirgi faylni (yoki skrinshotni) aynan qaysi kontaktga jo'natay?",
                    clarification_options=[
                        {"text": "📱 Xurshidullohga", "callback_data": "clarify:recipient:@XuRsHiDuLLoH"},
                        {"text": "📱 Jamshid Sodiqovga", "callback_data": "clarify:recipient:@Jamsh1d_Sodiqov"},
                        {"text": "👥 Oxirgi kontaktlar", "callback_data": "clarify:recent_contacts"},
                    ]
                )

        # Check: User asking to send a file without specifying which file or to whom
        is_send_file_req = bool(re.search(r"\b(faylni|hujjatni|rasmni|videoni)\s+(yubor|jo'nat|tashla|jonat)\b", low)) or \
                           bool(re.search(r"\b(fayl|hujjat|rasm)\s+(yubor|jo'nat|tashla|jonat)\b", low))

        if is_send_file_req:
            # Check for file name / path
            # If the user only said "faylni yubor" or "faylni jo'nat"
            has_explicit_filename = bool(re.search(r'[\'\"`][^\'\"]+[\'\"`]', norm)) or \
                                    bool(re.search(r'\b[a-zA-Z0-9_\-\.]+\.(?:txt|pdf|docx?|xlsx?|jpg|png|mp3|zip)\b', norm, re.IGNORECASE))
            
            # Check for recipient
            has_recipient = bool(re.search(r'(@[a-zA-Z0-9_]{3,})', norm)) or \
                            bool(re.search(r'(\b[a-zA-Z0-9_\']+?)(?:ga|ka|qa)\b', low))

            missing_slots = []
            if not has_explicit_filename:
                missing_slots.append("filename")
            if not has_recipient:
                missing_slots.append("recipient")

            if "filename" in missing_slots and "recipient" in missing_slots:
                return AmbiguityResult(
                    raw_query=raw_query,
                    normalized_query=norm,
                    intent="SEND_FILE",
                    entities={},
                    is_ambiguous=True,
                    ambiguity_type="MISSING_SLOT",
                    missing_slots=missing_slots,
                    clarification_needed=True,
                    clarification_question="Azizbek, aynan qaysi faylni va kimga yuborishimni xohlaysiz? Fayl nomi hamda qabul qiluvchi ko'rsatilmagan.",
                    clarification_options=[
                        {"text": "📁 Ish stolidagi fayllar", "callback_data": "clarify:list_desktop_files"},
                        {"text": "👥 Oxirgi kontaktlar", "callback_data": "clarify:recent_contacts"},
                    ]
                )
            elif "filename" in missing_slots:
                return AmbiguityResult(
                    raw_query=raw_query,
                    normalized_query=norm,
                    intent="SEND_FILE",
                    entities={"recipient": "specified" if has_recipient else "unspecified"},
                    is_ambiguous=True,
                    ambiguity_type="MISSING_SLOT",
                    missing_slots=["filename"],
                    clarification_needed=True,
                    clarification_question="Azizbek, aynan qaysi faylni yuboray? Iltimos, fayl nomini yoki turini ko'rsating (masalan: 'hisobot.pdf').",
                    clarification_options=[
                        {"text": "📁 Ish stolidagi fayllar", "callback_data": "clarify:list_desktop_files"},
                        {"text": "🔍 Fayllarni qidirish", "callback_data": "clarify:search_files"},
                    ]
                )
            elif "recipient" in missing_slots:
                return AmbiguityResult(
                    raw_query=raw_query,
                    normalized_query=norm,
                    intent="SEND_FILE",
                    entities={"filename": "specified"},
                    is_ambiguous=True,
                    ambiguity_type="MISSING_SLOT",
                    missing_slots=["recipient"],
                    clarification_needed=True,
                    clarification_question="Azizbek, ushbu faylni aynan qaysi kontaktga (Telegram foydalanuvchisiga) yuboray?",
                    clarification_options=[
                        {"text": "👥 Oxirgi kontaktlar", "callback_data": "clarify:recent_contacts"},
                        {"text": "📱 Jamshid Sodiqovga", "callback_data": "clarify:recipient:@Jamsh1d_Sodiqov"},
                    ]
                )

        # Check: User asking to send a message without specifying recipient
        m_cand_rec = re.search(r'\b([a-zA-Z0-9_\']+?)(?:ga|ka|qa)\b', low)
        cand_rec = m_cand_rec.group(1).lower() if m_cand_rec else ""
        reserved_recipients = {"telegram", "bot", "monika", "menga", "bizga", "unga", "bunga", "odam", "kontakt", "kim", "fayl"}
        has_named_rec = bool(cand_rec and cand_rec not in reserved_recipients)

        is_send_msg_req = not is_anaphora_forward and \
                          bool(re.search(r"\b(kontaktga|odamga|birovga|unga)?\s*(?:xabar|sms)?\s*(?:yoz|yubor|jo'nat|jonat)\b", low)) and \
                          not bool(re.search(r'(@[a-zA-Z0-9_]{3,})', norm)) and \
                          not has_named_rec and \
                          not bool(re.search(r'(\b[a-zA-Z0-9_\']+?)(?:ga|ka|qa)\b\s+["\']?(.+?)["\']?\s+deb\s+(?:yoz|yubor|jo\'nat|jonat)', low))

        if is_send_msg_req and any(w in low for w in ["kontaktga", "telegramga", "jo'nat", "yubor", "xabar yoz"]):
            # If query is like "kontaktga jo'nat" or "kontaktga xabar yoz"
            has_msg_content = bool(re.search(r'deb\s+(?:yoz|yubor|jo\'nat)|xabar:\s*.+', low))
            if not has_msg_content and len(low.split()) <= 4:
                return AmbiguityResult(
                    raw_query=raw_query,
                    normalized_query=norm,
                    intent="SEND_MESSAGE",
                    entities={},
                    is_ambiguous=True,
                    ambiguity_type="MISSING_SLOT",
                    missing_slots=["recipient", "message"],
                    clarification_needed=True,
                    clarification_question="Azizbek, xabarni qaysi kontaktga va qanday matn bilan yuborishimni xohlaysiz?",
                    clarification_options=[
                        {"text": "👥 Oxirgi kontaktlar", "callback_data": "clarify:recent_contacts"},
                    ]
                )
            else:
                return AmbiguityResult(
                    raw_query=raw_query,
                    normalized_query=norm,
                    intent="SEND_MESSAGE",
                    entities={"message": "specified" if has_msg_content else "unspecified"},
                    is_ambiguous=True,
                    ambiguity_type="MISSING_SLOT",
                    missing_slots=["recipient"],
                    clarification_needed=True,
                    clarification_question="Azizbek, bu xabarni aynan qaysi kontaktga jo'natishim kerak? Iltimos, kontakt nomini yoki @username'ni ayting.",
                    clarification_options=[
                        {"text": "👥 Oxirgi kontaktlar", "callback_data": "clarify:recent_contacts"},
                        {"text": "📱 Jamshid Sodiqovga", "callback_data": "clarify:recipient:@Jamsh1d_Sodiqov"},
                    ]
                )

        # ---------------------------------------------------------------------
        # 4. Default: No Ambiguity Detected
        # ---------------------------------------------------------------------
        media_type = self.preprocessor.extract_media_type(norm)
        entities = {}
        if media_type:
            entities["media_type"] = media_type

        return AmbiguityResult(
            raw_query=raw_query,
            normalized_query=norm,
            intent="DIRECT_COMMAND",
            entities=entities,
            is_ambiguous=False,
            ambiguity_type="NONE",
            clarification_needed=False
        )

    def resolve_clarification(self, original_query: str, callback_data: str) -> Dict[str, Any]:
        """Resolve a clarification callback choice into an actionable intent and resolved command.
        
        Args:
            original_query: The original ambiguous user input.
            callback_data: The callback data selected by user (e.g. 'clarify:ram', 'clarify:wallpaper').
            
        Returns:
            dict containing resolved intent, updated concrete command, and action metadata.
        """
        cb = callback_data.strip().lower()

        # 1. Storage clarifications
        if cb == "clarify:ram":
            return {
                "status": "resolved",
                "intent": "INQUIRE_STORAGE",
                "metric": "ram",
                "resolved_query": "Operativka (RAM) holatini ko'rsat",
                "action": "get_system_status",
                "kwargs": {"metric": "ram"},
                "message": "Tezkor xotira (RAM) ma'lumotlari tayyorlanmoqda."
            }
        elif cb == "clarify:disk":
            return {
                "status": "resolved",
                "intent": "INQUIRE_STORAGE",
                "metric": "disk",
                "resolved_query": "Diskdagi (SSD) bo'sh joyni ko'rsat",
                "action": "get_system_status",
                "kwargs": {"metric": "disk"},
                "message": "Disk (SSD) xotira hajmi tekshirilmoqda."
            }
        elif cb == "clarify:storage_both":
            return {
                "status": "resolved",
                "intent": "INQUIRE_STORAGE",
                "metric": "both",
                "resolved_query": "RAM va Disk xotira holatini ko'rsat",
                "action": "get_system_status",
                "kwargs": {"metric": "both"},
                "message": "RAM va Disk xotira holatlari to'liq tahlil qilinmoqda."
            }

        # 2. Wallpaper clarifications
        elif cb == "clarify:wallpaper":
            return {
                "status": "resolved",
                "intent": "GET_WALLPAPER",
                "action": "send_wallpaper_image",
                "resolved_query": "Ish stoli fon rasmini (Wallpaper) yubor",
                "kwargs": {"type": "wallpaper"},
                "message": "Ish stoli fon rasmi olinib, yuborilmoqda."
            }
        elif cb == "clarify:desktop_file":
            return {
                "status": "resolved",
                "intent": "GET_DESKTOP_FILE",
                "action": "list_desktop_files",
                "resolved_query": "Ish stoli (Desktop) papkasidagi fayllarni ko'rsat",
                "kwargs": {"type": "desktop_file"},
                "message": "Ish stolidagi fayllar ro'yxati tekshirilmoqda."
            }

        # 3. Recipient clarifications
        elif cb.startswith("clarify:recipient:"):
            rec = callback_data.split(":", 2)[2]
            orig_low = (original_query or "").lower()

            # Check if original query specifically names a concrete file
            file_match = re.search(r'([a-zA-Z0-9_\-\u0400-\u04FF]+\.(?:pdf|txt|docx?|xlsx?|pptx?|png|jpe?g|zip|rar))\b', original_query or "", re.IGNORECASE)
            if file_match:
                filename = file_match.group(1)
                return {
                    "status": "resolved",
                    "intent": "SEND_FILE",
                    "recipient": rec,
                    "resolved_query": f"{filename} faylini {rec} ga yubor",
                    "action": "send_file",
                    "kwargs": {"recipient": rec, "filename": filename, "file_path": filename},
                    "message": f"{filename} fayli {rec} ga yuborilmoqda."
                }

            # Check if referring to last file / screenshot
            if any(w in orig_low for w in ["oxirgi", "oxirgi fayl", "oxirgi rasm", "oxirgi skrinshot", "oxirgisini", "uni", "buni", "skrinshot"]):
                return {
                    "status": "resolved",
                    "intent": "FORWARD_LAST_FILE",
                    "recipient": rec,
                    "resolved_query": f"Oxirgi faylni {rec} ga yubor",
                    "action": "send_last_file",
                    "kwargs": {"recipient": rec},
                    "message": f"Oxirgi fayl {rec} ga yuborilmoqda."
                }

            # Extract possible message text already present in original request
            extracted_msg = ""
            deb_match = re.search(r"(.+?)\s+deb\s+(?:yoz|yubor)", original_query or "", re.IGNORECASE)
            if deb_match:
                extracted_msg = deb_match.group(1).strip()
                extracted_msg = re.sub(r'@[a-zA-Z0-9_]+', '', extracted_msg).strip()
                extracted_msg = extracted_msg.strip("'\"‘“’”").strip()
            else:
                quote_match = re.search(r"[\"“](.*?)[\"”]", original_query or "")
                if not quote_match:
                    quote_match = re.search(r"['‘](.*?)['’]", original_query or "")
                if quote_match:
                    extracted_msg = quote_match.group(1).strip()

            if extracted_msg:
                return {
                    "status": "resolved",
                    "intent": "SEND_MESSAGE",
                    "recipient": rec,
                    "resolved_query": f"{rec} ga '{extracted_msg}' deb yubor",
                    "action": "send_user_telegram_message",
                    "kwargs": {"recipient": rec, "message": extracted_msg},
                    "message": f"{rec} ga xabar yuborilmoqda: \"{extracted_msg}\""
                }

            return {
                "status": "resolved",
                "intent": "SEND_MESSAGE",
                "recipient": rec,
                "resolved_query": f"{rec} ga xabar yubor",
                "action": "set_pending_recipient",
                "kwargs": {"recipient": rec},
                "message": f"Qabul qiluvchi sifatida {rec} tanlandi. Endi xabar matnini kiriting."
            }

        return {
            "status": "unresolved",
            "callback_data": callback_data,
            "original_query": original_query,
            "message": "Tanlov bo'yicha aniq amal belgilanmadi."
        }


# =============================================================================
# Global Convenience Singletons
# =============================================================================

_DEFAULT_PREPROCESSOR: Optional[UzbekNLPPreprocessor] = None
_DEFAULT_AMBIGUITY_ENGINE: Optional[AmbiguityEngine] = None


def get_nlp_preprocessor() -> UzbekNLPPreprocessor:
    """Retrieve or instantiate default UzbekNLPPreprocessor."""
    global _DEFAULT_PREPROCESSOR
    if _DEFAULT_PREPROCESSOR is None:
        _DEFAULT_PREPROCESSOR = UzbekNLPPreprocessor()
    return _DEFAULT_PREPROCESSOR


def get_ambiguity_engine() -> AmbiguityEngine:
    """Retrieve or instantiate default AmbiguityEngine."""
    global _DEFAULT_AMBIGUITY_ENGINE
    if _DEFAULT_AMBIGUITY_ENGINE is None:
        _DEFAULT_AMBIGUITY_ENGINE = AmbiguityEngine(preprocessor=get_nlp_preprocessor())
    return _DEFAULT_AMBIGUITY_ENGINE


def preprocess_text(text: str) -> str:
    """Convenience helper to preprocess and clean Uzbek input text."""
    return get_nlp_preprocessor().preprocess(text)


def analyze_query(query: str) -> AmbiguityResult:
    """Convenience helper to analyze ambiguity of an incoming user query."""
    return get_ambiguity_engine().analyze(query)

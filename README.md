# J.A.R.V.I.S. - Shaxsiy Multimodal Sun'iy Intellekt va Kuzatuv Tizimi

JARVIS — bu kompyuteringizni aqlli qo'riqchi, audio-vizual yordamchi va administrator tizimiga aylantiruvchi loyiha.

---

## 🌟 Asosiy Imkoniyatlar

1. **👁️ Kamera & Shaxsiy Kuzatuv (Vision):**
   * Kompyuter kamerasidan foydalanib doimiy monitoring olib boradi.
   * Xonada harakat sodir bo'lganda (siz yo'qligingizda yoki yaqinlashganda) avtomatik ravishda suratga oladi.
   * Gemini Multimodal Vision orqali kamera ko'rib turgan sahnani tahlil qiladi.

2. **🎙️ Ovozli Muloqot (Voice & Ears):**
   * Tabiiy o'zbek tili (Sardor yoki Madina ovozi), ingliz va rus tillarida yuqori sifatli (Neural TTS) gapiradi.
   * Mikrofon orqali buyruqlarni eshitadi (STT).
   * "Jarvis" yoki "Salom Jarvis" chaqiruv so'ziga javob beradi.

3. **⚙️ Windows Administrator Boshqaruvi (System Admin):**
   * Ekranni zudlik bilan qulflash (`/lock` yoki ovozli "ekranni qulfla").
   * Ovoz balandligini boshqarish.
   * Kompyuter quvvati, CPU, RAM va disk xotirasini monitoring qilish.
   * Ilovalarni ochish/yopish va buyruqlarni xavfsiz bajarish.

4. **📱 Masofaviy Telegram Bot (Remote Gateway):**
   * Dunyoning istalgan joyidan kompyuteringizni Telegram orqali nazorat qilasiz.
   * Harakat aniqlanganda Telegramingizga zudlik bilan fotoxabar yuboradi.
   * Telegram buyruqlari:
     * `/photo` - Hozirgi kadrni suratga olib tashlaydi.
     * `/status` - Kompyuter va xavfsizlik holatini ko'rsatadi.
     * `/lock` - Kompyuterni masofadan qulflaydi.
     * `/say <matn>` - Kompyuter karnayida siz yozgan matnni gapirtiradi.
     * `/cmd <buyruq>` - Masofadan PowerShell buyrug'ini bajaradi.

5. **🤖 Antigravity Ko'prigi (Antigravity Bridge):**
   * Jarvis dasturlash, kod tahlili, refaktoring va og'ir muhandislik vazifalarini to'g'ridan-to'g'ri **Antigravity** agentiga topshiradi va natijasini sizga ovoz bilan aytadi.

---

## 🚀 Ishga Tushirish

### 1. Sozlamalar (`.env`)
Loyihaning ildiz papkasidagi `.env.example` faylidan nusxa olib, `.env` faylini yarating va ma'lumotlaringizni kiriting:
```ini
# Google Gemini API kaliti (bepul olish: https://aistudio.google.com/app/apikey)
GEMINI_API_KEY=your_gemini_api_key

# Telegram Bot (Telegramda @BotFather orqali yaratiladi)
TELEGRAM_BOT_TOKEN=your_bot_token
# Shaxsiy Telegram ID (@userinfobot orqali olinadi)
TELEGRAM_ADMIN_CHAT_ID=your_telegram_id
```

### 2. Tizimni Ishga Tushirish

#### Variant A: Zamonaviy Vizual Interfeys (HUD Dashboard)
```bash
python jarvis_gui.py
```
*Bu kompyuter ekranida kamera oynasi, jonli CPU/RAM datchiklari, hodisalar jurnali va tezkor boshqaruv tugmalarini ochadi.*

#### Variant B: Fon rejimida to'liq Jarvis (CLI)
```bash
python jarvis_core.py
```

### 3. Tizim Diagnostikasi (Self-Test)
Barcha modullar to'g'ri ulanganini tekshirish:
```bash
python test_integration.py
```

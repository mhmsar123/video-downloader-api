# Video Downloader API

سيرفر خلفي لتطبيق تحميل الفيديوهات من أي منصة (YouTube, Facebook, Instagram, TikTok, X, WhatsApp وأكثر من 1000 موقع) باستخدام yt-dlp.

## التشغيل محلياً

```bash
pip install -r requirements.txt
python app.py
```

السيرفر يعمل على `http://localhost:5000`

## المتغيرات البيئية

| المتغير | الوصف | الافتراضي |
|---------|--------|-----------|
| `PORT` | منفذ التشغيل | `5000` |
| `API_KEY` | مفتاح الحماية (إن تركته فارغاً يعمل بدون مفتاح) | فارغ |
| `MAX_CONCURRENT` | أقصى عدد تحميلات متوازية | `2` |
| `CLEANUP_AGE_SEC` | مدة الاحتفاظ بالملفات المحملة قبل الحذف | `7200` (ساعتين) |

## المتطلبات على السيرفر

- Python 3.9+
- [ffmpeg](https://ffmpeg.org) مثبت (مطلوب لدمج الصوت والفيديو)
  - Ubuntu: `sudo apt install ffmpeg`

## الـ APIs

### `POST /api/info`
جلب معلومات الفيديو (العنوان، الصورة، المدة، الجودات المتاحة).

```json
{ "url": "https://www.youtube.com/watch?v=..." }
```

### `POST /api/download`
بدء عملية التحميل وترجع `job_id`.

```json
{ "url": "...", "quality": "best", "audio_only": false, "audio_quality": "192" }
```

| الحقل | الوصف |
|-------|-------|
| `quality` | `best`, `worst`, أو ارتفاع مثل `720p` |
| `audio_only` | `true` لتحميل MP3 فقط |
| `audio_quality` | جودة الصوت `128`/`192`/`320` |

### `GET /api/status/<job_id>`
متابعة تقدم التحميل (نسبة مئوية، سرعة، الوقت المتبقي).

### `GET /api/download/<job_id>`
تحميل الملف النهائي بعد اكتمال التحميل.

### `POST /api/cancel/<job_id>`
إلغاء التحميل.

### `GET /health`
فحص حالة السيرفر.

# Pump.fun Smart Token Hunter (Paper Mode + Telegram)

ربات مانیتورینگ و امتیازدهی توکن‌های جدید pump.fun. **فقط شبیه‌سازی (Paper Trading)** است و هیچ خرید واقعی انجام نمی‌دهد. این پروژه توصیهٔ مالی نیست.

## جریان کار
توکن جدید → فیلتر سخت (کلمات ممنوعه، سهم سازنده) → مشاهدهٔ ۴۵ ثانیه معاملات → امتیازدهی → ثبت Paper Trade → اعلان تلگرام → بررسی PnL در ۵/۱۵/۳۰ دقیقه.

## ۱) ساخت ربات تلگرام
1. در تلگرام به `@BotFather` پیام بدهید، `/newbot` را بزنید و **توکن** را بگیرید.
2. بعداً به ربات خودتان `/start` بفرستید تا شناسهٔ چت (Chat ID) شما را نشان بدهد.

## ۲) آپلود روی گیت‌هاب
```bash
cd pump_bot
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/USERNAME/REPO.git
git push -u origin main
```
(یا روی سایت گیت‌هاب: Add file → Upload files و همهٔ فایل‌ها، از جمله پوشهٔ `.devcontainer` و `.gitignore`.)

## ۳) اجرا در GitHub Codespaces
1. در ریپو: **Code → Codespaces → Create codespace on main**
2. **Secrets را قبل از اجرا تنظیم کنید** (توکن را هرگز داخل کد/ریپو نگذارید):
   Settings → Codespaces → Secrets → New secret
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID` (اول خالی بگذارید، بعد از `/start` پر کنید)
   - به ریپو دسترسی بدهید. اگر Codespace از قبل باز بود، ری‌استارتش کنید.
3. در ترمینال Codespace:
```bash
python simulate.py                       # تست آفلاین؛ باید OK چاپ شود
nohup python main.py > logs/nohup.out 2>&1 &
tail -f logs/monitor.log
```
دستورهای تلگرام: `/status` `/stats` `/last` `/help`

برای توقف: `pkill -f main.py`

## نکات مهم
- Codespaces سرور ۲۴ ساعته نیست؛ بعد از مدتی بی‌فعالیتی خاموش می‌شود (در تنظیمات می‌توان timeout را بیشتر کرد) و سهمیهٔ رایگان ماهانه دارد. با خاموش شدن، سیگنال‌های باز از دست می‌روند (لاگ‌ها می‌مانند). برای اجرای دائمی به VPS یا Oracle Always Free نیاز دارید.
- قیمت = مارکت‌کپ به SOL از رویدادهای معامله؛ PnL با کارمزد ۱٪ هر طرف حساب می‌شود، بدون slippage، پس خوش‌بینانه است.
- سیگنال اجتماعی فقط تطبیق کلیدواژه است؛ لیست `smart_wallets.json` خالی است و باید خودتان ولت‌های واقعی را اضافه کنید.
- آستانه (۷۰) و وزن‌ها را فقط بعد از ۱۰۰–۲۰۰ نمونهٔ Paper عوض کنید.
- اگر PumpPortal برای `subscribeTokenTrade` کلید خواست، `PUMPPORTAL_API_KEY` را تنظیم کنید.

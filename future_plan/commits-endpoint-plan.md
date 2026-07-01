# Plan: endpoint `GET /commits` — نمایش ۵ کامیت آخرِ نسخه‌ی دیپلوی‌شده

## Context (چرا؟)

می‌خواهیم یک endpoint داشته باشیم که **۵ کامیت آخرِ git** را برگرداند (معادل
`git log --oneline -5`) تا از n8n (یا دستی) بشود فهمید **کدام نسخه‌ی کد روی
production زنده است**.

### چالش معماری (مهم)
سرویس‌ها داخل کانتینر Docker اجرا می‌شوند، ولی تاریخچه‌ی git روی **هاست** است.
build context هر سرویس فقط پوشه‌ی خودش (`./arbitrage_main`) است و `.git` داخلش
نیست. پس `git log` در زمان اجرا داخل کانتینر کار نمی‌کند (نه git نصب است، نه repo
حاضر است).

### تصمیم‌های تأییدشده با کاربر
- endpoint به **`arbitrage_main`** اضافه می‌شود (route جدید کنار `/run`؛ مسیر داغ
  `/run` دست‌نخورده می‌ماند).
- منبع داده: **تزریق موقع build** (build-time injection) — استانداردِ این کار.
  یک image داکر دقیقاً نماینده‌ی همان کامیتی است که از آن build شده؛ پس خروجی
  `git log --oneline -5` همان لحظه‌ی build در یک فایل ریخته و داخل image پخته
  می‌شود. endpoint فقط همان فایل را می‌خواند.
  - چرا نه اجرای git در زمان اجرا / mount کردن `.git`؟ وابستگی به فایل‌سیستم هاست،
    خطر drift بین آنچه گزارش می‌شود و آنچه واقعاً دیپلوی شده، و افزودن باینری git
    + کل repo به image. (anti-pattern)

## چرا «فایلِ تولیدشده» و نه build-arg؟
خروجی `git log --oneline -5` **چندخطی** است؛ پاس دادنش به‌صورت build-arg دست‌وپاگیر
است. ریختن آن در یک فایل داخل build context تمیزترین و مقاوم‌ترین راه است و چون
Dockerfile هم‌اکنون `COPY . .` دارد، فایل خودبه‌خود داخل image می‌رود.

## پیاده‌سازی

### ۱) تولید فایل موقع دیپلوی (CI)
در `.github/workflows/deploy.yml`، **قبل از** `docker compose ... up -d --build`،
یک خط اضافه می‌شود که خروجی git را در build context هر سرویسی که این فایل را لازم
دارد می‌ریزد:

```yaml
      - name: Pull latest code and redeploy
        run: |
          set -e
          cd ~/arbitrage
          git fetch origin main
          git reset --hard origin/main
          git log --oneline -5 > arbitrage_main/commit_log.txt   # ← خط جدید
          docker compose -f docker-compose.yaml -f docker-compose.prod.yaml up -d --build
          docker image prune -f
```

- چون runner، self-hosted روی همان سرور است و در `~/arbitrage` (ریشه‌ی repo) اجرا
  می‌شود، `git log` کار می‌کند و درست بعد از `reset --hard` (یعنی دقیقاً همان کامیت
  دیپلوی) فایل را می‌سازد.
- این فایل **build artifact** است، نه سورس → به `.gitignore` و
  `arbitrage_main/.dockerignore` (اگر هست) **اضافه نشود** چون باید توسط `COPY . .`
  وارد image شود؛ ولی به `.gitignore`ِ ریشه اضافه می‌شود تا commit نشود.

### ۲) Dockerfile
بدون تغییر. `COPY . .` فعلی فایل `commit_log.txt` را (اگر موجود باشد) داخل image
می‌برد. (در نظر داشتن: build کش‌نشدن این لایه مهم نیست؛ خود `up --build` هر بار
rebuild می‌کند.)

### ۳) route جدید در `arbitrage_main/main.py`
یک تابع کمکی + یک route اضافه می‌شود (هم‌سبک `/health` که GET است و body نمی‌خواهد):

```python
import os

COMMIT_LOG_PATH = os.path.join(os.path.dirname(__file__), "commit_log.txt")


def _read_commits():
    """۵ کامیت آخر را از فایلِ پخته‌شده در image می‌خواند و ساختارمند برمی‌گرداند."""
    try:
        with open(COMMIT_LOG_PATH, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except FileNotFoundError:
        return None
    commits = []
    for ln in lines:
        short_hash, _, subject = ln.partition(" ")
        commits.append({"hash": short_hash, "subject": subject})
    return commits


@app.get("/commits")
async def commits():
    data = _read_commits()
    if data is None:
        # image بدون فایل build شده (مثلاً build لوکالِ دستی بدون مرحله‌ی CI)
        return {"status": "unavailable",
                "detail": "commit_log.txt not baked into image", "commits": []}
    return {"status": "ok", "count": len(data), "commits": data}
```

خروجی نمونه:
```json
{
  "status": "ok",
  "count": 5,
  "commits": [
    {"hash": "d886bc5", "subject": "Add N8N_ENCRYPTION_KEY to n8n service ..."},
    {"hash": "c1e80e5", "subject": "Add CI/CD: auto-deploy to production ..."}
  ]
}
```

### ۴) `.gitignore` ریشه
افزودن `arbitrage_main/commit_log.txt` تا artifactِ تولیدشده توسط CI به‌اشتباه
commit نشود.

## رفتار در حالت‌های مختلف
- **production (از طریق CI):** فایل قبل از build ساخته می‌شود → `/commits` ۵ کامیت
  واقعیِ دیپلوی‌شده را برمی‌گرداند.
- **build لوکالِ دستی بدون اجرای دستور تولید فایل:** فایل نیست → پاسخ
  `{"status":"unavailable", ...}` (بدون کرش). برای تست لوکال می‌توان دستی زد:
  `git log --oneline -5 > arbitrage_main/commit_log.txt` و بعد build.

## فایل‌های لمس‌شده
- `arbitrage_main/main.py` (route جدید `GET /commits` + helper)
- `.github/workflows/deploy.yml` (یک خط تولید فایل قبل از build)
- `.gitignore` ریشه (افزودن `arbitrage_main/commit_log.txt`)
- (هیچ تغییری در Dockerfile، engine.py، client ها، compose لازم نیست)

## Verification
1. **لوکال، بدون CI:**
   ```bash
   git log --oneline -5 > arbitrage_main/commit_log.txt
   docker compose up -d --build arbitrage_main
   curl -s localhost:<port>/commits | jq      # → ۵ کامیت
   ```
2. **حالت فقدان فایل:** فایل را پاک کن، rebuild کن، `/commits` بزن →
   `status: "unavailable"` و کد کرش نکند.
3. **end-to-end production:** یک کامیت تستی به main بزن، صبر کن CI تمام شود،
   `curl .../commits` → کامیت جدید باید بالای لیست باشد (تأیید عدم drift).
4. **عدم تأثیر بر مسیر داغ:** `POST /run` و `/health` بدون تغییر کار کنند.

## نکات/ریسک‌ها
- اگر بعداً `arbitrage_wallet` (یا سرویس‌های دیگر) هم همین endpoint را لازم داشتند،
  همین الگو تکرار می‌شود: یک خط `git log ... > <service>/commit_log.txt` در CI +
  همان route. (در صورت تکرارِ زیاد، می‌توان فایل را یک‌بار ساخت و به چند build
  context کپی کرد.)
- اطلاعات کامیت **حساس نیست** (همان چیزی که در GitHub عمومی/خصوصی هست)، ولی چون
  `arbitrage_main` پشت شبکه‌ی internal است، endpoint از بیرون مستقیم در دسترس
  نیست مگر از طریق همان مسیری که n8n استفاده می‌کند.

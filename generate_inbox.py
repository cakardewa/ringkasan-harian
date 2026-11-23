import datetime
import email
from email.header import decode_header
import imaplib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import requests
from dotenv import load_dotenv
from google import genai

BASE_DIR = Path(__file__).resolve().parent
ARCHIVE_DIR = BASE_DIR / "archive"
load_dotenv(BASE_DIR / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")


def decode_mime_words(raw_val: str | None) -> str:
    if not raw_val:
        return ""
    parts = decode_header(raw_val)
    out = []
    for text_bytes, charset in parts:
        if isinstance(text_bytes, bytes):
            enc = charset or "utf-8"
            try:
                out.append(text_bytes.decode(enc, errors="replace"))
            except LookupError:
                out.append(text_bytes.decode("utf-8", errors="replace"))
        else:
            out.append(str(text_bytes))
    return "".join(out).strip()


def extract_plain_text(msg: email.message.Message) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        body += payload.decode(charset, errors="replace") + " "
                    except LookupError:
                        body += payload.decode("utf-8", errors="replace") + " "
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                body = payload.decode(charset, errors="replace")
            except LookupError:
                body = payload.decode("utf-8", errors="replace")

    cleaned = re.sub(r"\s+", " ", body).strip()
    return cleaned[:450]


def fetch_recent_emails(hours: int = 24, max_emails: int = 35) -> list[dict]:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_USER atau GMAIL_APP_PASSWORD belum diisi di .env")

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    since_imap = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%d-%b-%Y")

    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("INBOX")
        status, data = mail.search(None, f'(SINCE "{since_imap}")')
        if status != "OK" or not data or not data[0]:
            return []

        msg_ids = data[0].split()
        recent_items = []

        for msg_id in reversed(msg_ids):
            if len(recent_items) >= max_emails:
                break
            st, msg_data = mail.fetch(msg_id, "(RFC822)")
            if st != "OK" or not msg_data or not msg_data[0]:
                continue
            raw_email = msg_data[0][1]
            if not isinstance(raw_email, bytes):
                continue
            msg = email.message_from_bytes(raw_email)

            date_hdr = msg.get("Date")
            parsed_dt = None
            if date_hdr:
                try:
                    parsed_dt = email.utils.parsedate_to_datetime(date_hdr)
                    if parsed_dt.tzinfo is None:
                        parsed_dt = parsed_dt.replace(tzinfo=datetime.timezone.utc)
                except Exception:
                    parsed_dt = None

            if parsed_dt and parsed_dt < cutoff:
                continue

            sender = decode_mime_words(msg.get("From"))
            subject = decode_mime_words(msg.get("Subject")) or "(Tanpa Subjek)"
            snippet = extract_plain_text(msg)

            recent_items.append(
                {
                    "sender": sender[:120],
                    "subject": subject[:180],
                    "date": date_hdr or "",
                    "snippet": snippet,
                }
            )

        return recent_items
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def build_prompt(emails: list[dict], today_str: str) -> str:
    payload = json.dumps(emails, ensure_ascii=False, indent=2)
    return f"""
Kamu adalah asisten ringkasan kotak masuk harian untuk edisi {today_str}.
Kelompokkan daftar email berikut ke dalam 5 kategori:
- "Prioritas & Tindak Lanjut"
- "Keuangan & Tagihan"
- "Keamanan Akun"
- "Informasi"
- "Promo"

Kembalikan HANYA JSON valid dengan struktur:
{{
  "edition_date": "{today_str}",
  "overview": "ringkasan 1-2 kalimat kondisi kotak masuk 24 jam terakhir",
  "counts": {{
    "prioritas": 0,
    "keuangan": 0,
    "keamanan": 0,
    "informasi": 0,
    "promo": 0
  }},
  "items": [
    {{
      "category": "nama kategori persis seperti di atas",
      "sender": "pengirim singkat",
      "subject": "subjek email",
      "summary": "ringkasan singkat isi & tindakan yang disarankan (1 kalimat bahasa Indonesia)"
    }}
  ]
}}
Jangan sertakan kode OTP/token rahasia jika ada.

Daftar Email:
{payload}
""".strip()


def call_gemini(prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY belum diisi di .env")
    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt,
    )
    if not response.text:
        raise RuntimeError("Respons kosong dari Gemini API")
    return response.text


def call_ollama(prompt: str) -> str:
    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
        },
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    text = (data.get("response") or "").strip()
    if not text:
        raise RuntimeError("Respons kosong dari Ollama")
    return text


def extract_json(raw_text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if not match:
        raise ValueError("Tidak ditemukan blok JSON yang valid pada output AI")
    return json.loads(match.group(0))


def render_html(digest: dict, total_fetched: int, engine_used: str, today_str: str) -> str:
    overview = digest.get("overview", "Ringkasan kotak masuk 24 jam terakhir.")
    counts = digest.get("counts", {})
    items = digest.get("items", [])

    stat_cards = f"""
    <div class="stats">
      <div class="stat"><strong>{counts.get("prioritas", 0)}</strong><span>Prioritas</span></div>
      <div class="stat"><strong>{counts.get("keuangan", 0)}</strong><span>Keuangan</span></div>
      <div class="stat"><strong>{counts.get("keamanan", 0)}</strong><span>Keamanan</span></div>
      <div class="stat"><strong>{counts.get("informasi", 0)}</strong><span>Informasi</span></div>
      <div class="stat"><strong>{counts.get("promo", 0)}</strong><span>Promo</span></div>
    </div>
    """

    rows_html = []
    for it in items:
        cat = it.get("category", "Informasi")
        sender = it.get("sender", "")
        subject = it.get("subject", "")
        summary = it.get("summary", "")
        rows_html.append(
            f"""
            <div class="mail-card">
              <div class="top"><span class="badge">{cat}</span><span class="sender">{sender}</span></div>
              <h3>{subject}</h3>
              <p>{summary}</p>
            </div>
            """
        )

    body_list = "\n".join(rows_html) if rows_html else "<p>Tidak ada email masuk dalam 24 jam terakhir.</p>"

    return f"""<!DOCTYPE html>
<html lang="id">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Ringkasan Email Harian — {today_str}</title>
  <style>
    :root {{ --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --muted: #94a3b8; --accent: #34d399; --border: #334155; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); padding: 24px; line-height: 1.55; }}
    .wrap {{ max-width: 920px; margin: 0 auto; }}
    header {{ border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 20px; }}
    h1 {{ margin: 0 0 6px; font-size: 1.6rem; }}
    .sub {{ color: var(--muted); font-size: 0.9rem; margin: 0; }}
    .overview {{ background: var(--card); border-left: 4px solid var(--accent); padding: 14px 16px; border-radius: 8px; margin-bottom: 18px; }}
    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin-bottom: 22px; }}
    .stat {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px; text-align: center; }}
    .stat strong {{ display: block; font-size: 1.35rem; color: var(--accent); }}
    .stat span {{ font-size: 0.8rem; color: var(--muted); }}
    .mail-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin-bottom: 12px; }}
    .top {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 0.8rem; }}
    .badge {{ background: rgba(52, 211, 153, 0.15); color: var(--accent); padding: 2px 8px; border-radius: 999px; font-weight: 600; }}
    .sender {{ color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    h3 {{ margin: 0 0 6px; font-size: 1.02rem; }}
    p {{ margin: 0; color: #cbd5e1; font-size: 0.9rem; }}
    footer {{ margin-top: 26px; color: var(--muted); font-size: 0.8rem; text-align: center; }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>Ringkasan Email Harian</h1>
      <p class="sub">Edisi {today_str} • Terdeteksi {total_fetched} email (24 jam) • Mesin: {engine_used}</p>
    </header>
    <div class="overview">{overview}</div>
    {stat_cards}
    <div class="list">
      {body_list}
    </div>
    <footer>Ringkasan Harian • Diperbarui otomatis via GitHub Pages</footer>
  </div>
</body>
</html>
"""


def git_publish(today_str: str) -> None:
    subprocess.run(["git", "add", "."], cwd=BASE_DIR, check=True)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        check=True,
    )
    if not status.stdout.strip():
        print("[INFO] Tidak ada perubahan file baru untuk di-push.")
        return
    subprocess.run(
        ["git", "commit", "-m", f"Update Email Harian {today_str}"],
        cwd=BASE_DIR,
        check=True,
    )
    subprocess.run(["git", "push"], cwd=BASE_DIR, check=True)


def main() -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.date.today().isoformat()
    emails = fetch_recent_emails(hours=24, max_emails=35)

    if not emails:
        digest = {
            "edition_date": today_str,
            "overview": "Tidak ada email masuk baru dalam 24 jam terakhir.",
            "counts": {"prioritas": 0, "keuangan": 0, "keamanan": 0, "informasi": 0, "promo": 0},
            "items": [],
        }
        engine_used = "IMAP Direct (No New Mail)"
    else:
        prompt = build_prompt(emails, today_str)
        engine_used = "Gemini API (gemini-3.5-flash-lite)"
        try:
            raw = call_gemini(prompt)
            digest = extract_json(raw)
        except Exception as exc:
            print(f"[WARN] Gemini API gagal/limit ({exc}), beralih ke Ollama lokal ({OLLAMA_MODEL})...")
            engine_used = f"Ollama Local ({OLLAMA_MODEL})"
            raw = call_ollama(prompt)
            digest = extract_json(raw)

    html_content = render_html(digest, len(emails), engine_used, today_str)
    latest_path = BASE_DIR / "latest-inbox.html"
    archive_path = ARCHIVE_DIR / f"daily_inbox_digest_{today_str}.html"

    latest_path.write_text(html_content, encoding="utf-8")
    shutil.copyfile(latest_path, archive_path)
    print(f"[OK] Tersimpan ke {latest_path} dan {archive_path}")

    git_publish(today_str)
    print("[OK] Selesai push ke GitHub Pages.")


if __name__ == "__main__":
    main()
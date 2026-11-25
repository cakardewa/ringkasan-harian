import datetime
import html
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo

import feedparser
import requests
from dotenv import load_dotenv
from google import genai

T = TypeVar("T")

BASE_DIR = Path(__file__).resolve().parent
ARCHIVE_DIR = BASE_DIR / "archive"
load_dotenv(BASE_DIR / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

RSS_FEEDS = [
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("MusicRadar", "https://www.musicradar.com/rss"),
    (
        "Sound On Sound",
        "https://news.google.com/rss/search?q=site:soundonsound.com&hl=en-US&gl=US&ceid=US:en",
    ),
    (
        "Kompas Tekno",
        "https://news.google.com/rss/search?q=site:tekno.kompas.com&hl=id&gl=ID&ceid=ID:id",
    ),
    ("CNN Indonesia Tech", "https://www.cnnindonesia.com/teknologi/rss"),
]

SOURCE_TITLE_SUFFIXES = {
    "Sound On Sound": (" - Sound On Sound",),
    "Kompas Tekno": (" - Kompas.com", " - KOMPAS.com"),
}


class NonRetriableFeedError(RuntimeError):
    """Error permanen pada feed (mis. HTTP 403/404) yang tidak perlu diulang."""


def get_today_wib() -> str:
    try:
        return datetime.datetime.now(ZoneInfo("Asia/Jakarta")).date().isoformat()
    except Exception:
        return datetime.date.today().isoformat()


def run_with_retry(
    func: Callable[[], T],
    task_name: str,
    max_attempts: int = 3,
    base_delay_sec: int = 15,
) -> T:
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except NonRetriableFeedError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts:
                print(f"[ERROR] {task_name} gagal setelah {max_attempts} percobaan: {exc}")
                raise
            wait_sec = base_delay_sec * attempt
            print(
                f"[WARN] {task_name} gagal (percobaan {attempt}/{max_attempts}): {exc}. "
                f"Mencoba ulang dalam {wait_sec} detik..."
            )
            time.sleep(wait_sec)
    assert last_exc is not None
    raise last_exc


def _clean_entry_title(source: str, title: str) -> str:
    cleaned = title.strip()
    for suffix in SOURCE_TITLE_SUFFIXES.get(source, ()):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
    return cleaned


def _parse_single_feed(source: str, url: str, max_per_feed: int) -> list[dict]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(url, timeout=20, headers=headers)
    if resp.status_code in (401, 403, 404, 410):
        raise NonRetriableFeedError(
            f"HTTP {resp.status_code} ({resp.reason}) untuk {source} — langsung dilewati"
        )
    resp.raise_for_status()

    raw_prefix = resp.text.lstrip()[:200].lower()
    if raw_prefix.startswith("<!doctype html") or raw_prefix.startswith("<html"):
        raise NonRetriableFeedError(
            f"Server {source} mengembalikan halaman HTML (HTTP {resp.status_code}), bukan feed XML/RSS"
        )

    parsed = feedparser.parse(resp.content)
    if not parsed.entries:
        raise RuntimeError(f"Format RSS tidak valid atau daftar artikel kosong untuk {source}")

    feed_items = []
    for entry in parsed.entries[:max_per_feed]:
        title = _clean_entry_title(source, entry.get("title") or "")
        link = (entry.get("link") or "").strip()
        summary = re.sub(r"<[^>]+>", " ", entry.get("summary") or "")
        summary = html.unescape(re.sub(r"\s+", " ", summary)).strip()[:320]
        if title and link:
            feed_items.append(
                {
                    "source": source,
                    "title": title,
                    "link": link,
                    "summary": summary,
                }
            )
    return feed_items


def fetch_feed_items(max_per_feed: int = 5) -> list[dict]:
    items = []
    for source, url in RSS_FEEDS:
        try:
            feed_items = run_with_retry(
                lambda s=source, u=url: _parse_single_feed(s, u, max_per_feed),
                task_name=f"Ambil RSS {source}",
                max_attempts=3,
                base_delay_sec=15,
            )
            items.extend(feed_items)
        except Exception as exc:
            print(f"[WARN] Melewati RSS {source}: {exc}")
    return items


def build_prompt(items: list[dict], today_str: str) -> str:
    payload = json.dumps(items, ensure_ascii=False, indent=2)
    return f"""
Kamu adalah kurator berita profesional untuk profil Wicaksono Arie Dewanto (edisi {today_str}).
Kurasi berita dari daftar RSS berikut berdasarkan relevansi dengan 5 pilar keahlian:
1. Musik & Session Bassist (bass elektrik, audio gear, studio musik, produksi musik, industri musik)
2. Produksi Audio-Visual & Broadcasting (video, sinematografi, post-production, radio/broadcasting)
3. AI Kreatif & Automasi (LLM, generative video/audio/image, workflow AI, lokal & cloud)
4. Infrastruktur IT, Hardware & IoT (PC/Mini PC, jaringan, Linux/WSL2, mikrokontroler/smart home)
5. Pemasaran, Manajemen & Kewirausahaan (strategi konten, digital marketing, operasional kreatif)

Kembalikan HANYA JSON valid dengan struktur:
{{
  "edition_date": "{today_str}",
  "headline_note": "ringkasan 1-2 kalimat tren utama hari ini",
  "articles": [
    {{
      "pillar": "nama pilar",
      "source": "nama media",
      "title": "judul artikel",
      "takeaway": "ringkasan singkat mengapa relevan (1-2 kalimat bahasa Indonesia)",
      "url": "url asli"
    }}
  ]
}}
Pilih maksimal 10 artikel paling relevan dan akurat sesuai data input (jangan mengarang URL).

Data RSS:
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


def render_html(digest: dict, engine_used: str, today_str: str) -> str:
    headline_note = html.escape(digest.get("headline_note", "Kurasi berita harian otomatis."))
    safe_engine = html.escape(engine_used)
    safe_date = html.escape(today_str)
    articles = digest.get("articles", [])

    cards_html = []
    for art in articles:
        pillar = html.escape(art.get("pillar", "Umum"))
        source = html.escape(art.get("source", ""))
        title = html.escape(art.get("title", ""))
        takeaway = html.escape(art.get("takeaway", ""))
        url = html.escape(art.get("url", "#"), quote=True)
        cards_html.append(
            f"""
            <article class="card">
              <div class="meta"><span class="tag">{pillar}</span><span>{source}</span></div>
              <h3><a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a></h3>
              <p>{takeaway}</p>
            </article>
            """
        )

    body_cards = "\n".join(cards_html) if cards_html else "<p>Tidak ada artikel terkurasi hari ini.</p>"

    return f"""<!DOCTYPE html>
<html lang="id">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Berita CV &amp; Tren Industri — {safe_date}</title>
  <style>
    :root {{ --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --muted: #94a3b8; --accent: #38bdf8; --border: #334155; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); padding: 24px; line-height: 1.55; }}
    .wrap {{ max-width: 960px; margin: 0 auto; }}
    header {{ border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; }}
    h1 {{ margin: 0 0 6px; font-size: 1.6rem; }}
    .sub {{ color: var(--muted); font-size: 0.9rem; margin: 0; }}
    .note {{ background: var(--card); border-left: 4px solid var(--accent); padding: 14px 16px; border-radius: 8px; margin-bottom: 20px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
    .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }}
    .meta {{ display: flex; justify-content: space-between; gap: 8px; font-size: 0.78rem; color: var(--muted); margin-bottom: 8px; }}
    .tag {{ background: rgba(56, 189, 248, 0.15); color: var(--accent); padding: 2px 8px; border-radius: 999px; font-weight: 600; }}
    h3 {{ margin: 0 0 8px; font-size: 1.05rem; }}
    h3 a {{ color: var(--text); text-decoration: none; }}
    h3 a:hover {{ color: var(--accent); }}
    p {{ margin: 0; color: #cbd5e1; font-size: 0.92rem; }}
    footer {{ margin-top: 28px; color: var(--muted); font-size: 0.8rem; text-align: center; }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>Berita CV &amp; Tren Industri</h1>
      <p class="sub">Edisi {safe_date} • Mesin: {safe_engine}</p>
    </header>
    <div class="note">{headline_note}</div>
    <div class="grid">
      {body_cards}
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
    if status.stdout.strip():
        subprocess.run(
            ["git", "commit", "-m", f"Update Berita CV {today_str}"],
            cwd=BASE_DIR,
            check=True,
        )
    else:
        print("[INFO] Tidak ada perubahan file baru untuk di-commit, memeriksa push tertunda...")

    def _do_push() -> None:
        subprocess.run(["git", "push", "origin", "main"], cwd=BASE_DIR, check=True)

    run_with_retry(_do_push, task_name="Git push ke GitHub Pages", max_attempts=3, base_delay_sec=15)


def main() -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    today_str = get_today_wib()
    items = fetch_feed_items()
    if not items:
        raise RuntimeError("Tidak ada item berita yang berhasil diambil dari RSS.")

    prompt = build_prompt(items, today_str)
    engine_used = "Gemini API (gemini-3.5-flash-lite)"
    try:
        raw = call_gemini(prompt)
        digest = extract_json(raw)
    except Exception as exc:
        print(f"[WARN] Gemini API gagal/limit ({exc}), beralih ke Ollama lokal ({OLLAMA_MODEL})...")
        engine_used = f"Ollama Local ({OLLAMA_MODEL})"
        raw = call_ollama(prompt)
        digest = extract_json(raw)

    html_content = render_html(digest, engine_used, today_str)
    latest_path = BASE_DIR / "latest-career-news.html"
    archive_path = ARCHIVE_DIR / f"CV_Career_News_Digest_{today_str}.html"

    latest_path.write_text(html_content, encoding="utf-8")
    shutil.copyfile(latest_path, archive_path)
    print(f"[OK] Tersimpan ke {latest_path} dan {archive_path}")

    git_publish(today_str)
    print("[OK] Selesai push ke GitHub Pages.")


if __name__ == "__main__":
    main()

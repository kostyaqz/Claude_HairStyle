#!/usr/bin/env python3
"""
Примерка причёсок на собственном фото через Gemini image editing.

Берёт ваш портрет, прогоняет его через набор промптов из styles.json
и складывает результаты в папку вместе с HTML-страницей для сравнения.

Ключ: переменная окружения GEMINI_API_KEY (https://aistudio.google.com/apikey)
Ключ в репозиторий не коммитить.

    python3 tools/hairstyle_preview.py photo.jpg
    python3 tools/hairstyle_preview.py photo.jpg --set stages
    python3 tools/hairstyle_preview.py photo.jpg --only curtains,flow
"""

import argparse
import base64
import json
import mimetypes
import os
import pathlib
import sys
import time

import requests

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

# Без этого блока модель «чинит» линию роста волос и рисует чужого человека
# с юношеской шевелюрой — превью становится бесполезным враньём.
IDENTITY_GUARD = """
Critical constraints, follow all of them:
- This is a photo of a real person. Preserve their identity exactly: same face,
  same facial proportions, same eyes, nose, mouth, jawline, skin tone, freckles
  and blemishes. The result must be recognisably the same individual.
- Change ONLY the hair. Do not alter the face, body, clothing, background,
  camera angle, framing or lighting direction.
- Preserve the subject's real hairline shape, including the existing recession
  at both frontal-temporal corners. Do NOT give them a fuller, lower or younger
  hairline than they have. Where a hairstyle covers the corners, it must cover
  them with hair falling over them, not by regrowing the hairline.
- Keep the hair colour and strand thickness true to the original: fine,
  light-to-medium ash brown. Do not make the hair thicker or darker than it is.
- Photorealistic result, consistent with the original photo's lighting and grain.
""".strip()


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def pick_model(api_key, override=None):
    """Спрашиваем у API, какие image-модели доступны, вместо хардкода имени."""
    if override:
        return override
    try:
        r = requests.get(
            f"{API_ROOT}/models",
            headers={"x-goog-api-key": api_key},
            params={"pageSize": 200},
            timeout=30,
        )
    except requests.RequestException as e:
        die(f"не достучались до Gemini API: {e}")

    if r.status_code == 403:
        die("ключ отклонён (403). Проверьте GEMINI_API_KEY и что Generative "
            "Language API включён в проекте.")
    if not r.ok:
        die(f"список моделей недоступен: HTTP {r.status_code} {r.text[:300]}")

    cands = []
    for m in r.json().get("models", []):
        name = m.get("name", "").removeprefix("models/")
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" not in methods:
            continue
        if "image" not in name or "embedding" in name:
            continue
        # грубая сортировка по номеру версии в имени: gemini-3-... > gemini-2.5-...
        ver = 0.0
        for tok in name.split("-"):
            try:
                ver = max(ver, float(tok))
            except ValueError:
                pass
        cands.append((ver, "preview" in name or "exp" in name, name))

    if not cands:
        die("в аккаунте не нашлось ни одной image-модели Gemini. "
            "Укажите модель вручную: --model <имя>")

    # свежая версия вперёд, стабильные раньше preview/exp при равной версии
    cands.sort(key=lambda t: (-t[0], t[1]))
    return cands[0][2]


def load_image(path):
    p = pathlib.Path(path)
    if not p.is_file():
        die(f"нет такого файла: {path}")
    mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
    if not mime.startswith("image/"):
        die(f"это не изображение: {path}")
    data = p.read_bytes()
    if len(data) > 18 * 1024 * 1024:
        die("фото больше 18 МБ — уменьшите его перед отправкой")
    return mime, base64.b64encode(data).decode()


def generate(api_key, model, mime, b64, prompt, attempts=4):
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": mime, "data": b64}},
                {"text": f"{prompt}\n\n{IDENTITY_GUARD}"},
            ]
        }],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
    }
    url = f"{API_ROOT}/models/{model}:generateContent"

    for i in range(attempts):
        try:
            r = requests.post(
                url,
                headers={"x-goog-api-key": api_key,
                         "Content-Type": "application/json"},
                json=body,
                timeout=180,
            )
        except requests.RequestException as e:
            if i == attempts - 1:
                return None, f"сеть: {e}"
            time.sleep(2 ** i)
            continue

        if r.status_code in (429, 500, 502, 503, 504):
            if i == attempts - 1:
                return None, f"HTTP {r.status_code} после {attempts} попыток"
            time.sleep(2 ** i * 2)
            continue
        if not r.ok:
            return None, f"HTTP {r.status_code}: {r.text[:300]}"

        payload = r.json()
        cands = payload.get("candidates") or []
        if not cands:
            fb = payload.get("promptFeedback", {})
            return None, f"пустой ответ, promptFeedback={fb}"

        reason = cands[0].get("finishReason")
        parts = cands[0].get("content", {}).get("parts", []) or []
        for part in parts:
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                return base64.b64decode(blob["data"]), None

        said = " ".join(p.get("text", "") for p in parts).strip()
        return None, f"картинки в ответе нет (finishReason={reason}) {said[:200]}"

    return None, "не удалось"


def contact_sheet(out_dir, original_name, rows, model):
    """Страница для сравнения: оригинал слева, варианты сеткой."""
    cards = "\n".join(
        f'''    <figure class="card">
      <img src="{r["file"]}" alt="{r["label"]}">
      <figcaption><b>{r["label"]}</b><span>{r["note"]}</span></figcaption>
    </figure>''' for r in rows if r.get("file")
    )
    failed = [r for r in rows if not r.get("file")]
    fail_html = ""
    if failed:
        items = "".join(f"<li><b>{r['label']}</b> — {r['error']}</li>" for r in failed)
        fail_html = f'<section class="fails"><h2>Не сгенерировалось</h2><ul>{items}</ul></section>'

    html = f'''<!doctype html>
<meta charset="utf-8">
<title>Примерка причёсок</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.55 system-ui, sans-serif; margin: 0; padding: 32px 20px;
         max-width: 1200px; margin-inline: auto; background: Canvas; color: CanvasText; }}
  h1 {{ font-size: 26px; margin: 0 0 6px; }}
  p.meta {{ color: GrayText; margin: 0 0 28px; font-size: 14px; }}
  .grid {{ display: grid; gap: 20px;
           grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }}
  .card {{ margin: 0; }}
  .card img {{ width: 100%; height: auto; border-radius: 4px; display: block; }}
  figcaption {{ display: flex; flex-direction: column; gap: 2px; margin-top: 8px;
                font-size: 14px; }}
  figcaption span {{ color: GrayText; font-size: 13px; }}
  .orig img {{ outline: 2px solid Highlight; outline-offset: 2px; }}
  .fails {{ margin-top: 34px; font-size: 14px; }}
  .fails h2 {{ font-size: 16px; }}
  .warn {{ border-left: 3px solid Highlight; padding-left: 14px; margin: 0 0 28px;
           font-size: 14px; color: GrayText; max-width: 62ch; }}
</style>
<h1>Примерка причёсок</h1>
<p class="meta">Модель: {model} · вариантов: {len(rows)}</p>
<p class="warn">Это машинная визуализация, а не фотография. Модель может приукрасить
плотность и линию роста даже вопреки инструкции — сверяйтесь с оригиналом слева,
а не с тем, что хочется увидеть.</p>
<div class="grid">
  <figure class="card orig">
    <img src="{original_name}" alt="Оригинал">
    <figcaption><b>Оригинал</b><span>как есть</span></figcaption>
  </figure>
{cards}
</div>
{fail_html}
'''
    (out_dir / "index.html").write_text(html, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Примерка причёсок через Gemini")
    ap.add_argument("photo", help="ваш портрет (анфас, сухие волосы, дневной свет)")
    ap.add_argument("--set", default="styles", choices=["styles", "stages", "all"],
                    help="какой набор промптов гонять (по умолчанию styles)")
    ap.add_argument("--only", help="через запятую: id вариантов, только их")
    ap.add_argument("--out", default="previews", help="папка для результатов")
    ap.add_argument("--model", help="имя модели вручную, иначе подбирается само")
    ap.add_argument("--styles", default=None, help="путь к styles.json")
    args = ap.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        die("не задан GEMINI_API_KEY.\n"
            "  Ключ: https://aistudio.google.com/apikey\n"
            "  export GEMINI_API_KEY='...'")

    styles_path = pathlib.Path(args.styles) if args.styles \
        else pathlib.Path(__file__).with_name("styles.json")
    if not styles_path.is_file():
        die(f"нет файла с промптами: {styles_path}")
    cfg = json.loads(styles_path.read_text(encoding="utf-8"))

    items = []
    if args.set in ("styles", "all"):
        items += cfg.get("styles", [])
    if args.set in ("stages", "all"):
        items += cfg.get("stages", [])
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        unknown = wanted - {i["id"] for i in items}
        if unknown:
            die(f"неизвестные id: {', '.join(sorted(unknown))}")
        items = [i for i in items if i["id"] in wanted]
    if not items:
        die("нечего генерировать")

    mime, b64 = load_image(args.photo)
    model = pick_model(api_key, args.model)
    print(f"модель: {model}")

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    original_name = "original" + pathlib.Path(args.photo).suffix
    (out_dir / original_name).write_bytes(pathlib.Path(args.photo).read_bytes())

    rows, ok = [], 0
    for n, item in enumerate(items, 1):
        print(f"[{n}/{len(items)}] {item['label']} ... ", end="", flush=True)
        img, err = generate(api_key, model, mime, b64, item["prompt"])
        row = {"label": item["label"], "note": item.get("note", "")}
        if img:
            fname = f"{item['id']}.png"
            (out_dir / fname).write_bytes(img)
            row["file"] = fname
            ok += 1
            print("готово")
        else:
            row["error"] = err
            print(f"ОШИБКА — {err}")
        rows.append(row)

    contact_sheet(out_dir, original_name, rows, model)
    print(f"\n{ok} из {len(items)} готово → {out_dir / 'index.html'}")
    if ok == 0:
        sys.exit(2)


if __name__ == "__main__":
    main()

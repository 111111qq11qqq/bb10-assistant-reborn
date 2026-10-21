#!/usr/bin/env python3
"""Wolfram-plugin answer engine for the BB10 assistant server.

The patched libWolframPlugin.so on the phone calls:
  GET /wa/recognizer?appid=…&mode=Default&i=<question>&sig=…
  GET /wa/q?input=<question>&appid=…&format=image&spokenresult=true&…

We answer with Wikipedia summaries (bg/en), rendered as a dark card image
plus a spoken string. BB10 has no Bulgarian TTS voice, so BG answers are
card-only (spokenresult omitted); EN answers are spoken by the phone TTS.
"""
import hashlib
import html
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request

CARDS = {}          # id -> (png_bytes, height, ts)
_CARD_TTL = 3600

_FONT_DIR = (r"E:\KimiData\daimon-share\daimon\runtime\python\.venv\Lib"
             r"\site-packages\matplotlib\mpl-data\fonts\ttf")
_FONT = os.path.join(_FONT_DIR, "DejaVuSans.ttf")
_FONT_B = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")

_UA = {"User-Agent": "BB10Assistant/1.0 (local voice assistant)"}

# question prefixes stripped before the wiki search
_PREFIX = re.compile(
    r"^(?:кой е|коя е|кое е|какво е|каква е|какви са|какво означава|"
    r"кога е|кога е роден|колко е|къде е|разкажи ми за|"
    r"who is|who was|who are|what is|what's|what was|what are|"
    r"what does|tell me about|when did|when was|where is|how much is|"
    r"how many)\s+",
    re.IGNORECASE)


def detect_lang(text):
    return "bg" if re.search(r"[а-яА-Я]", text or "") else "en"


def _fetch(url, timeout=10):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def wiki_answer(question, lang):
    """One-call Wikipedia search+extract. Returns answer text or None."""
    q = _PREFIX.sub("", question.strip().rstrip("?!.")).strip() or question
    api = ("https://%s.wikipedia.org/w/api.php?action=query&format=json"
           "&prop=extracts&exintro&explaintext&redirects=1"
           "&generator=search&gsrlimit=1&exchars=700&gsrsearch=%s"
           % (lang, urllib.parse.quote(q)))
    try:
        data = json.loads(_fetch(api).decode("utf-8"))
    except Exception as e:
        print(f"[wa] wiki {lang} failed: {e}")
        return None
    pages = (data.get("query") or {}).get("pages") or {}
    for p in pages.values():
        txt = (p.get("extract") or "").strip()
        if txt and "may refer to:" not in txt:
            return txt
    return None


def ddg_answer(question):
    """DuckDuckGo instant answer (English-oriented fallback)."""
    api = ("https://api.duckduckgo.com/?format=json&no_html=1&q="
           + urllib.parse.quote(question))
    try:
        data = json.loads(_fetch(api).decode("utf-8"))
    except Exception as e:
        print(f"[wa] ddg failed: {e}")
        return None
    txt = (data.get("AbstractText") or "").strip()
    return txt or None


def _wrap(draw, text, font, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_card(question, answer, lang):
    """Dark rounded card, returns (card_id, png_bytes, height)."""
    from PIL import Image, ImageDraw, ImageFont
    W, pad = 684, 26
    f_title = ImageFont.truetype(_FONT_B, 21)
    f_body = ImageFont.truetype(_FONT, 27)
    probe = ImageDraw.Draw(Image.new("RGB", (W, 10)))
    title_lines = _wrap(probe, question, f_title, W - 2 * pad)[:2]
    body_lines = _wrap(probe, answer, f_body, W - 2 * pad)[:9]
    lh_t, lh_b = 30, 38
    H = pad + len(title_lines) * lh_t + 10 + len(body_lines) * lh_b + pad
    img = Image.new("RGB", (W, H), (11, 14, 20))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([1, 1, W - 2, H - 2], radius=22,
                        outline=(56, 182, 255), width=2)
    y = pad
    for ln in title_lines:
        d.text((pad, y), ln, font=f_title, fill=(127, 140, 160))
        y += lh_t
    y += 10
    for ln in body_lines:
        d.text((pad, y), ln, font=f_body, fill=(230, 237, 243))
        y += lh_b
    buf = io.BytesIO()
    img.save(buf, "PNG")
    cid = hashlib.md5((question + answer + str(time.time())).encode()
                      ).hexdigest()[:12]
    CARDS[cid] = (buf.getvalue(), H, time.time())
    # gc
    for k in [k for k, v in CARDS.items() if time.time() - v[2] > _CARD_TTL]:
        CARDS.pop(k, None)
    return cid, buf.getvalue(), H


_SPACER = None


def spacer_png():
    global _SPACER
    if _SPACER is None:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(buf, "PNG")
        _SPACER = buf.getvalue()
    return _SPACER


def get_image(name):
    if name == "spacer.png":
        return spacer_png(), 1
    if name.endswith(".png"):
        cid = name[:-4]
        if cid in CARDS:
            png, h, _ = CARDS[cid]
            return png, h
    return None, 0


def recognizer_xml(question):
    q = html.escape(question or "", quote=True)
    return ("<?xml version='1.0' encoding='UTF-8'?>\n"
            "<queryrecognizer version='1.0'>\n"
            f"  <query i=\"{q}\" accepted='true' timing='0.1' domain='misc'\n"
            "         resultsignificancescore='1' summarybox=''/>\n"
            "</queryrecognizer>")


def queryresult_xml(question, base_url):
    """Full answer pipeline. Returns (xml_bytes, content_type)."""
    question = (question or "").strip()
    lang = detect_lang(question)
    print(f"[wa] question lang={lang} q={question!r}")
    answer = wiki_answer(question, lang)
    if not answer and lang != "bg":
        answer = ddg_answer(question)
    if not answer:
        answer = ("Не намерих отговор. Опитай с друг въпрос."
                  if lang == "bg" else
                  "Sorry, I could not find an answer. Try rephrasing.")
    # keep it speakable: first 2 sentences for TTS, more on the card
    sents = re.split(r"(?<=[.!?])\s+", answer)
    spoken = " ".join(sents[:2]) if lang != "bg" else ""
    card_text = " ".join(sents[:6])
    cid, _png, height = render_card(question, card_text, lang)
    img_url = f"{base_url}/wa/img/{cid}.png"
    spacer_url = f"{base_url}/wa/img/spacer.png"
    alt = html.escape(card_text[:120], quote=True)
    sp = ""
    if spoken:
        s = html.escape(spoken, quote=True)
        sp = (f"  <spokenresult sampletext=\"{s}\">\n"
              f"    <srtemplate sampletext=\"{s}\"/>\n"
              "  </spokenresult>\n")
    xml = ("<?xml version='1.0' encoding='UTF-8'?>\n"
           "<queryresult success='true' error='false' numpods='2' "
           "version='2.6'>\n"
           "  <pod title='' id='Result' position='100' error='false' "
           "numsubpods='1' primary='true'>\n"
           f"    <subpod title=''><img src=\"{img_url}\" alt=\"{alt}\" "
           f"title='' width='684' height='{height}'/></subpod>\n"
           "  </pod>\n"
           "  <pod title='' id='Spacer' position='200' error='false' "
           "numsubpods='1'>\n"
           f"    <subpod title=''><img src=\"{spacer_url}\" alt='' "
           "title='' width='1' height='1'/></subpod>\n"
           "  </pod>\n" + sp +
           "</queryresult>")
    return xml.encode("utf-8"), "application/xml"

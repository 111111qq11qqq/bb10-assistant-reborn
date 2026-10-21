#!/usr/bin/env python3
"""
Minimal reference server for the revived BlackBerry 10 Assistant.

Implements the two dead cloud back-ends documented in
https://github.com/damianolampisti00/bb10-assistant-protocol :

  * Dragon (speech-to-text): POST multipart/form-data with 16 kHz PCM,
    answered with a multipart body carrying an NLSML string.
  * Maluuba (NLU): GET /delegate/asrBegin (connectivity probe) and
    GET /delegate/runPipeline (classification JSON).

No third-party dependency is required for the plumbing.  Speech
recognition and NLU are pluggable:

  * If `faster-whisper` is installed, it is used for transcription.
  * Otherwise set BB10_FAKE_TRANSCRIPT="set an alarm at 7" (or --fake)
    to test the whole chain without a recogniser.
  * NLU is an LLM classifier against any OpenAI-compatible endpoint
    (Ollama, LM Studio, llama.cpp, vLLM, ...), configured with
    BB10_LLM_URL / BB10_LLM_MODEL / BB10_LLM_KEY or --llm-url/--llm-model.
    Without an LLM configured (or if it fails), a rule-based
    Italian/English classifier is used as fallback.

Usage:
    python server.py --host 0.0.0.0 --port 8443 --cert cert.pem --key key.pem
    python server.py --port 8080 --fake "imposta una sveglia alle 7"
"""

import argparse
import json
import os
import re
import ssl
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote_plus
from urllib.request import Request, urlopen

BOUNDARY = "----Nuance_NMSP"

# ---------------------------------------------------------------------------
# Speech recognition (pluggable)
# ---------------------------------------------------------------------------

_FAKE_TRANSCRIPT = None
_WHISPER_MODEL = None
# Last language whisper detected: {"lang": "bg", "prob": 0.93, "ts": ...}
_WHISPER_INFO = {"lang": None, "prob": 0.0, "ts": 0.0}
# Contact names for whisper prompt bias (contacts.txt, one per line)
_CONTACTS = {"mtime": 0.0, "names": []}
_CONTACTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "contacts.txt")


def contact_names():
    """Names from contacts.txt (auto-filled by NVC contact sync too)."""
    try:
        mt = os.path.getmtime(_CONTACTS_FILE)
    except OSError:
        return []
    if mt != _CONTACTS["mtime"]:
        try:
            with open(_CONTACTS_FILE, encoding="utf-8") as f:
                names = [ln.strip() for ln in f if ln.strip()]
            _CONTACTS["names"] = names[:60]  # keep the prompt small
            _CONTACTS["mtime"] = mt
            print(f"[whisper] contacts bias: {len(_CONTACTS['names'])} names")
        except Exception:
            pass
    return _CONTACTS["names"]


def transcribe(pcm: bytes, language: str) -> str:
    """PCM (16 kHz mono s16le) -> transcript. Replace with your recogniser."""
    if _FAKE_TRANSCRIPT is not None:
        return _FAKE_TRANSCRIPT
    global _WHISPER_MODEL
    try:
        from faster_whisper import WhisperModel  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        print("[dragon] no recogniser available; answer as silence")
        return ""
    if _WHISPER_MODEL is None:
        model_name = os.environ.get("BB10_WHISPER_MODEL", "large-v3-turbo")
        _WHISPER_MODEL = WhisperModel(model_name, device="auto",
                                      compute_type="int8")
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    lang = (language or "")[:3].strip("-").lower() or None  # ita->it, eng->en
    lang = {"ita": "it", "eng": "en", "deu": "de", "fra": "fr",
            "spa": "es"}.get(lang, lang)
    forced = os.environ.get("BB10_WHISPER_LANG", "").strip().lower()
    if forced == "auto":
        lang = None  # whisper detects the spoken language per utterance
    elif forced:
        lang = forced
    elif lang == "en" and "distil" in os.environ.get("BB10_WHISPER_MODEL", ""):
        pass  # distil models are English-only, already fine
    prompt = os.environ.get(
        "BB10_WHISPER_PROMPT",
        "Voice assistant commands: set an alarm, call a contact, send a "
        "message, save, cancel, yes, no, what time is it, weather. "
        "Команди на асистент: сложи аларма, обади се на, изпрати съобщение, "
        "напомни ми, запази, откажи, да, не, колко е часът, какво е времето, "
        "пусни браузъра, пусни музика.")
    names = contact_names()
    if names and not os.environ.get("BB10_WHISPER_PROMPT"):
        prompt += " Контакти: " + ", ".join(names) + "."
    beam = int(os.environ.get("BB10_WHISPER_BEAM", "5"))

    def _run(_lang, _prompt):
        segments, _info = _WHISPER_MODEL.transcribe(
            audio, language=_lang,
            beam_size=beam,
            vad_filter=True,
            condition_on_previous_text=False, initial_prompt=_prompt)
        return " ".join(s.text for s in segments).strip(), _info

    text, info = _run(lang, prompt)
    detected = getattr(info, "language", None)
    prob = float(getattr(info, "language_probability", 0.0))
    # BG-heavy prompt can force Cyrillic output even when the speech is
    # English ("open browser" -> "Опан браузъра"). If whisper heard a
    # non-Bulgarian language but wrote Cyrillic, redo with the detected
    # language forced and an unbiased prompt.
    if (detected and detected != "bg" and lang is None
            and re.search(r"[а-яА-Я]", text)):
        print(f"[whisper] pass2: forcing lang={detected} "
              f"(cyrillic artifact: {text!r})")
        text, info = _run(detected, None)
        detected = getattr(info, "language", detected)
        prob = float(getattr(info, "language_probability", prob))
    try:
        _WHISPER_INFO["lang"] = detected
        _WHISPER_INFO["prob"] = prob
        _WHISPER_INFO["ts"] = time.time()
        print(f"[whisper] detected lang={_WHISPER_INFO['lang']} "
              f"p={_WHISPER_INFO['prob']:.2f}")
    except Exception:
        pass
    return text


# ---------------------------------------------------------------------------
# Dragon protocol (docs/03-dragon.md)
# ---------------------------------------------------------------------------

def parse_multipart(body: bytes, boundary: str) -> list:
    """Very small multipart/form-data parser: returns [(headers, content)]."""
    parts = []
    delim = ("--" + boundary).encode()
    for chunk in body.split(delim)[1:]:
        chunk = chunk.strip(b"\r\n")
        if chunk == b"--" or not chunk:
            continue
        if chunk.endswith(b"--"):
            chunk = chunk[:-2].rstrip(b"\r\n")
        head, _, content = chunk.partition(b"\r\n\r\n")
        headers = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        parts.append((headers, content))
    return parts


def disposition_params(header: str) -> dict:
    out = {}
    for item in header.split(";")[1:]:
        k, _, v = item.strip().partition("=")
        out[k.strip().lower()] = v.strip().strip('"')
    return out


def build_nlsml(transcript: str) -> str:
    """The exact NLSML shape the hand-written scanner accepts (doc 3.3)."""
    if not transcript:
        return ('<?xml version="1.0"?><result>'
                '<interpretation confidence="0.0"><input></input>'
                '</interpretation></result>')
    safe = transcript.replace("&", "&amp;").replace("<", "&lt;")
    words = []
    t = 0
    for i, w in enumerate(safe.split()):
        start = t + 120
        end = start + 60 * len(w) + 60
        t = end
        words.append(
            f'<word wordId="{i}" confidence="0.90" start="{start}" end="{end}">'
            f"{w}</word>")
    return ('<?xml version="1.0"?><result>'
            '<interpretation confidence="0.90"><instance>'
            + "".join(words) +
            f'</instance><input>{safe}</input>'
            '</interpretation></result>')


def extract_contact_names(parts):
    """Pull display names out of a Nuance contact-sync upload.

    Format unknown until we see a real one — try JSON first, then vCard
    FN:/N: fields. Returns a list of "First Last" strings."""
    names = []
    for _nm, content in parts:
        text = content.decode("utf-8", "replace")
        try:
            data = json.loads(text)
        except Exception:
            data = None
        if data is not None:
            items = data if isinstance(data, list) else \
                data.get("contacts") or data.get("items") or []
            for c in items:
                if not isinstance(c, dict):
                    continue
                disp = (c.get("displayName") or c.get("name") or "").strip()
                if not disp:
                    disp = " ".join(x for x in (c.get("firstName", ""),
                                                c.get("lastName", ""))
                                    if x).strip()
                if disp:
                    names.append(disp)
            continue
        # vCard fallback
        for m in re.finditer(r"(?im)^FN[^:]*:(.*)$", text):
            v = m.group(1).strip().replace("\\;", ";").replace("\\,", ",")
            if v:
                names.append(v)
    # de-dup, keep order
    seen, out = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def merge_contacts(new_names):
    """Append new names to contacts.txt (whisper prompt bias source)."""
    existing = []
    try:
        with open(_CONTACTS_FILE, encoding="utf-8") as f:
            existing = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        pass
    have = {n.lower() for n in existing}
    added = [n for n in new_names if n.lower() not in have]
    if not added:
        return
    with open(_CONTACTS_FILE, "a", encoding="utf-8") as f:
        if existing:
            f.write("\n")
        f.write("\n".join(added))
    print(f"[contacts] +{len(added)} names -> contacts.txt")


def dragon_response(transcript: str) -> bytes:
    nlsml = build_nlsml(transcript)
    payload = json.dumps({"nlsml_results": nlsml}, ensure_ascii=False)
    body = (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="QueryResult"; '
        'paramName="XML_RESULT"\r\n'
        "Content-Type: application/JSON; charset=utf-8\r\n\r\n"
        f"{payload}\r\n"
        f"--{BOUNDARY}--\r\n"
    )
    return body.encode()


def handle_dragon(body: bytes, content_type: str) -> bytes:
    m = re.search(r'boundary=([^;]+)', content_type)
    if not m:
        return dragon_response("")
    boundary = m.group(1).strip().strip('"')
    pcm = bytearray()
    language = ""
    cmd_name = ""
    upload_parts = []
    for headers, content in parse_multipart(body, boundary):
        disp = disposition_params(headers.get("content-disposition", ""))
        name = disp.get("name", "")
        if name == "RequestData":
            try:
                rd = json.loads(content.decode("utf-8", "replace"))
                language = rd.get("language", "")
                cmd_name = rd.get("cmdName", "")
            except Exception:
                pass
        elif disp.get("paramname") == "AUDIO_INFO" or name == \
                "ConcludingAudioParameter":
            pcm += content
        elif name and name != "RequestData":
            upload_parts.append((name, content))
    if cmd_name == "NVC_DATA_UPLOAD_CMD":
        # Contact sync: keep ONLY display names (first/last) for the
        # whisper prompt bias. Raw dump for format study, then discard.
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            raw = os.path.join(os.path.dirname(_CONTACTS_FILE),
                               f"contact_upload_{ts}.bin")
            with open(raw, "wb") as f:
                for nm, c in upload_parts:
                    f.write(b"=== " + nm.encode() + b" ===\n" + c + b"\n")
            names = extract_contact_names(upload_parts)
            if names:
                merge_contacts(names)
            print(f"[dragon] contact sync: {len(upload_parts)} parts, "
                  f"{len(names)} names extracted")
        except Exception as e:
            print(f"[dragon] contact sync parse failed: {e}")
        return dragon_response("")
    print(f"[dragon] {cmd_name or 'ASR'} lang={language} "
          f"pcm={len(pcm)} bytes ({len(pcm)/32000:.1f} s)")
    if len(pcm) % 2:
        # Streaming uploads sometimes carry a trailing partial byte; drop it.
        del pcm[-1:]
    if pcm:
        try:
            import wave
            import numpy as _np
            _a = _np.frombuffer(bytes(pcm), dtype=_np.int16).astype(
                _np.float32) / 32768.0
            _rms = float(_np.sqrt((_a ** 2).mean()))
            _peak = float(_np.abs(_a).max())
            print(f"[dragon] audio rms={_rms:.4f} peak={_peak:.4f}")
            _fn = (os.environ.get("BB10_AUDIO_DUMP", "")
                   or "last_dragon.wav")
            with wave.open(_fn, "wb") as _w:
                _w.setnchannels(1)
                _w.setsampwidth(2)
                _w.setframerate(16000)
                _w.writeframes(bytes(pcm))
        except Exception as _e:
            print(f"[dragon] audio dump failed: {_e}")
    try:
        transcript = transcribe(bytes(pcm), language)
    except Exception as e:
        print(f"[dragon] transcribe failed: {e}")
        transcript = ""
    print(f"[dragon] transcript: {transcript!r}")
    return dragon_response(transcript)


# ---------------------------------------------------------------------------
# NLU: times, durations, numbers (docs/04-maluuba.md §4.3)
# ---------------------------------------------------------------------------

NUMWORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "uno": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6,
    "sette": 7, "otto": 8, "nove": 9, "dieci": 10, "undici": 11,
    "dodici": 12, "tredici": 13, "quattordici": 14, "quindici": 15,
    "sedici": 16, "diciassette": 17, "diciotto": 18, "diciannove": 19,
    "venti": 20, "trenta": 30, "quaranta": 40, "cinquanta": 50,
    "un": 1, "una": 1, "an": 1, "a": 1, "mezza": 30, "half": 30,
    # Bulgarian
    "едно": 1, "една": 1, "един": 1, "две": 2, "два": 2, "три": 3,
    "четири": 4, "пет": 5, "шест": 6, "седем": 7, "осем": 8, "девет": 9,
    "десет": 10, "единадесет": 11, "единайсет": 11, "дванадесет": 12,
    "дванайсет": 12, "тринадесет": 13, "тринайсет": 13,
    "четиринадесет": 14, "четиринайсет": 14, "петнадесет": 15,
    "петнайсет": 15, "шестнадесет": 16, "шестнайсет": 16,
    "седемнадесет": 17, "седемнайсет": 17, "осемнадесет": 18,
    "осемнайсет": 18, "деветнадесет": 19, "деветнайсет": 19,
    "двадесет": 20, "двайсет": 20, "тридесет": 30, "трийсет": 30,
    "четиридесет": 40, "петдесет": 50, "половина": 30,
}


def num(tok: str):
    tok = tok.lower()
    if tok.isdigit():
        return int(tok)
    return NUMWORDS.get(tok)


def epoch_ms(dt: datetime) -> str:
    """Explicit form: epoch milliseconds + 'E' (doc 4.3)."""
    return str(int(dt.timestamp() * 1000)) + "E"


def parse_time_of_day(text: str, default_pm=False):
    """'alle 7', 'alle 19:30', 'at 7', '7 e mezza', 'в 7' -> (hour, minute)."""
    t = text.lower()
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(?:alle|at|per le|for|в|към|около)\s+(\w+)", t)
        if not m:
            m = re.search(r"\b(\d{1,2})\b", t)
        if not m:
            return None
        h = num(m.group(1))
        if h is None:
            return None
        mi = 0
        if "e mezza" in t or "half past" in t or "и половина" in t:
            mi = 30
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    if default_pm and h < 12:
        h += 12
    return h, mi


def next_occurrence(hour: int, minute: int) -> datetime:
    now = datetime.now().astimezone()
    dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if dt <= now:
        dt += timedelta(days=1)
    return dt


def parse_duration_ms(text: str):
    """'5 minuti', 'un'ora', '30 seconds', '5 минути' -> milliseconds."""
    t = text.lower()
    m = re.search(r"(\w+)\s*(secondi|seconds?|secs?|minuti|minutes?|mins?|"
                  r"ore|hours?|hrs?|секунд\w*|минут\w*|час\w*)", t)
    if not m:
        return None
    n = num(m.group(1))
    if n is None:
        return None
    unit = m.group(2)
    if unit.startswith(("second", "sec", "секунд")):
        return n * 1000
    if unit.startswith(("minut", "min", "минут")):
        return n * 60_000
    return n * 3600_000


# ---------------------------------------------------------------------------
# NLU: LLM classifier + rule-based fallback
# (docs/05-mapping.md, 06-intents.md, 07-dialogue.md)
# Keep the classification pairs and entity/field names exactly as documented:
# the phone maps them through /base/etc/asr-maluuba.cfg.
# ---------------------------------------------------------------------------

# Short-lived dialogue memory for INPUT_TIME_ANDOR_DATE ("When?") turns:
# the server must re-send the ORIGINAL intent with the time added (doc 7.2).
_pending_time_intent = {}  # sessionId -> (primary, secondary, entities)


def result(primary, secondary, entities=None):
    return {
        "primaryClassification": primary,
        "secondaryClassification": secondary,
        "entities": entities or [],
    }


def ent(t, v):
    return {"type": t, "value": str(v)}


# ---------------------------------------------------------------------------
# LLM back-end (OpenAI-compatible /chat/completions: Ollama, LM Studio, ...)
# ---------------------------------------------------------------------------

_LLM = {"url": None, "model": None, "key": None, "timeout": 20,
        "down_until": 0.0}

_LLM_CLASSIFY_PROMPT = """\
You are the NLU back-end of the BlackBerry 10 voice Assistant. The user speaks
a command (any language). Reply with ONLY a JSON object, no prose:

{"primary": "<primaryClassification>", "secondary": "<secondaryClassification>",
 "entities": [{"type": "<ENTITY>", "value": "<string>"}]}

Classification pairs and their entity fields:

- alarm: ALARM_INPUT / ALARM_INPUT, entity TIME
- timer: ALARM_START_TIMER / ALARM_START_TIMER, entity DURATION
- phone call: CALL / CALL_CALLING, CONTACTNAME or PHONENUMBER (digits only)
- SMS: MESSAGING_SEND / MESSAGING_SEND_TEXT, CONTACTNAME or PHONENUMBER, MESSAGE
- e-mail: MESSAGING_SEND / MESSAGING_SEND_EMAIL, CONTACTNAME or EMAILADDRESS, \
SUBJECT, BODY
- read messages / notifications: MESSAGING_READ / MESSAGING_READ_READ, \
optional FROM_CONTACTNAME
- create note: NOTE / NOTE_SET, MESSAGE
- search notes: NOTE / NOTE_QUERY, optional MESSAGE
- reminder: REMINDER_SET / REMINDER_SET, MESSAGE, TIME
- list reminders: REMINDER_SET / REMINDER_QUERY
- calendar create: CALENDAR / CALENDAR_CREATE_EVENT, SUBJECT, TIMERANGE
- calendar search: CALENDAR / CALENDAR_SEARCH, DATERANGE, optional SUBJECT
- play music: MUSIC / MUSIC_PLAY, optional SONG, ARTIST, ALBUM, PLAYLIST, GENRE
- music control: MUSIC / MUSIC_PAUSE | MUSIC_RESUME | MUSIC_NEXT_TRACK | \
MUSIC_PREVIOUS_TRACK | MUSIC_SHUFFLE | MUSIC_SONG_INFO (no entities)
- device setting: PHONE_SETTINGS / APPLICATION_PHONE_SETTINGS, \
SETTING (WIFI, BLUETOOTH, FLASHLIGHT, AIRPLANE, NFC, ROTATIONLOCK, \
LOCATIONSERVICES, NETWORK, BRIGHTNESS, VOLUME, BATTERY, NOTIFICATIONS), \
VALUE (ON, OFF, CHECK, INCREASE, DECREASE, MIN, MAX) or AMOUNT ("0"-"100")
- weather: WEATHER / WEATHER_STATUS, optional LOCATION (omit for "here")
- open app: APPLICATION / APPLICATION, APPNAME
- knowledge question: KNOWLEDGE / KNOWLEDGE_WOLFRAM, QUERY = the full question
- anything else (web search): SEARCH / SEARCH_DEFAULT, SEARCHQUERY

Entity encoding rules:
- TIME / DATE: ISO 8601 local time ("2026-10-19T07:00"). Current local time is \
{now}. Resolve relative times ("at 7", "tonight at 8") to the NEXT occurrence.
- TIMERANGE / DATERANGE: "<start ISO>,<end ISO>".
- DURATION: ISO 8601 duration ("PT5M", "PT1H30M").
- CONTACTNAME exactly as spoken; never "correct" or translate names.
- Omit entities you do not know: the phone asks its own follow-up questions.
"""


def llm_chat(system, user):
    """One chat completion against an OpenAI-compatible endpoint."""
    import time as _time
    if not _LLM["url"]:
        return None
    if _time.time() < _LLM["down_until"]:
        return None
    req = Request(
        _LLM["url"].rstrip("/") + "/chat/completions",
        data=json.dumps({
            "model": _LLM["model"],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": 300,
        }).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + (_LLM["key"] or "none")})
    try:
        with urlopen(req, timeout=_LLM["timeout"]) as r:
            data = json.loads(r.read().decode())
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[llm] error: {e}; falling back to rules "
              f"(retry after {_LLM['timeout']} s)")
        _LLM["down_until"] = _time.time() + _LLM["timeout"]
        return None


def llm_json(system, user):
    """Chat + tolerant JSON extraction."""
    text = llm_chat(system, user)
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


_TIME_TYPES = {"TIME", "DATE", "ORIGINALTIME", "REPLACEMENTTIME",
               "ORIGINALDATE", "REPLACEMENTDATE"}
_RANGE_TYPES = {"TIMERANGE", "DATERANGE", "ORIGINALTIMERANGE",
                "REPLACEMENTTIMERANGE"}


def _iso_to_epoch_ms(value):
    try:
        dt = datetime.fromisoformat(value.strip())
        if dt.tzinfo is None:
            dt = dt.astimezone()  # interpret as local time
        return epoch_ms(dt)
    except Exception:
        return None


def _iso_duration_to_ms(value):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
                     value.strip().upper())
    if not m:
        return None
    d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    return str(((d * 24 + h) * 3600 + mi * 60 + s) * 1000)


def normalize_llm_entities(entities):
    """LLM sends ISO times/durations; the phone wants doc-4.3 encoding."""
    out = []
    for e in entities or []:
        t, v = str(e.get("type", "")), str(e.get("value", ""))
        if not t or not v:
            continue
        if t in _TIME_TYPES:
            enc = _iso_to_epoch_ms(v)
            if enc:
                out.append(ent(t, enc))
                out.append(ent(t + "_explicit", "TRUE"))
        elif t in _RANGE_TYPES and "," in v:
            a, b = v.split(",", 1)
            ea, eb = _iso_to_epoch_ms(a), _iso_to_epoch_ms(b)
            if ea and eb:
                out.append(ent(t, f"{ea},{eb}"))
                out.append(ent(t + "_explicit", "TRUE"))
        elif t == "DURATION":
            ms = _iso_duration_to_ms(v)
            out.append(ent(t, ms if ms else re.sub(r"\D", "", v) or v))
        else:
            out.append(ent(t, v))
    return out


def llm_classify(query):
    """Full-command classification via the LLM. None when unavailable."""
    obj = llm_json(
        _LLM_CLASSIFY_PROMPT.replace(
            "{now}", datetime.now().astimezone().isoformat(timespec="seconds")),
        query)
    if not obj:
        return None
    primary, secondary = obj.get("primary"), obj.get("secondary")
    if not primary or not secondary:
        return None
    return result(primary, secondary,
                  normalize_llm_entities(obj.get("entities")))


def interpret_answer(query):
    """yes/no/back/cancel: rules first, LLM for the rest (any language)."""
    a = yes_no_cancel(query)
    if a or not _LLM["url"]:
        return a
    obj = llm_json(
        "The voice assistant asked a yes/no confirmation question. Classify "
        "the user's short answer (any language) as exactly one of: "
        '"yes", "no", "back" (wants to change something), "cancel". '
        'Reply with ONLY a JSON object: {"answer": "..."}',
        query)
    a = (obj or {}).get("answer", "")
    return a if a in ("yes", "no", "back", "cancel") else None


def extract_time(query, default_pm=False):
    """Time of day: rules first, LLM fallback."""
    tod = parse_time_of_day(query, default_pm)
    if tod or not _LLM["url"]:
        return tod
    obj = llm_json(
        "The voice assistant asked 'What time?' / 'When?'. Extract the time "
        "of day from the user's answer (any language). Current local time: "
        + datetime.now().isoformat(timespec="seconds")
        + '. Reply with ONLY a JSON object: {"hour": 7, "minute": 30} '
        '(24-hour clock) or {} if there is no time.',
        query)
    try:
        h, mi = int(obj["hour"]), int(obj.get("minute", 0))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi
    except Exception:
        pass
    return None


def extract_duration(query):
    """Duration in ms: rules first, LLM fallback."""
    ms = parse_duration_ms(query)
    if ms is not None or not _LLM["url"]:
        return ms
    obj = llm_json(
        "The voice assistant asked 'For how long?'. Extract the duration "
        "from the user's answer (any language). Reply with ONLY a JSON "
        'object: {"ms": 300000} (milliseconds) or {} if there is none.',
        query)
    try:
        ms = int(obj["ms"])
        return ms if ms > 0 else None
    except Exception:
        return None


def yes_no_cancel(q):
    """Returns 'yes' | 'no' | 'back' | 'cancel' | None."""
    t = q.lower().strip()
    if re.search(r"\b(cancel|annulla|annullare|stop|basta|отказ|откажи|"
                 r"отмени|стоп|спри|кенсъл|кенсел|кансъл|кансел)\b", t):
        return "cancel"
    if re.search(r"\b(change|edit|back|cambia|modifica|correggi|промени|"
                 r"смени|назад|коригирай)\b", t):
        return "back"
    if re.search(r"\b(yes|yeah|yep|ok|okay|sure|s[iì]|certo|va bene|"
                 r"esatto|send|invia|save|salva|add|aggiungi|да|добре|"
                 r"разбира се|точно|така е|изпрати|запази|запиши|"
                 r"добави|потвърждавам|ок|окей|кей|сейв|сейф|съхрани|"
                 r"давай|действай|йес|йеа)\b", t):
        return "yes"
    if re.search(r"\b(no|nope|nah|не|ноу)\b", t):
        return "no"
    return None


def classify_followup(handler, query, session):
    """Route on handlerType FIRST (doc 7.4). Returns None if unhandled."""
    t = query.lower()

    if handler in ("CONFIRMATION", "INPUT_CONFIRMATION"):
        a = interpret_answer(query)
        if a == "yes":
            return result("CONFIRMATION", "CONFIRMATION_YES")
        if a == "no":
            return result("CONFIRMATION", "CONFIRMATION_NO")
        if a == "back":
            return result("CONFIRMATION", "back")
        if a == "cancel":
            return result("CONFIRMATION", "cancel")

    if handler == "MESSAGE_SEND_CONFIRMATION":
        a = interpret_answer(query)
        if re.search(r"\b(read|leggi)\b", t):
            return result("CONFIRMATION", "MESSAGING_READ_READ")
        if a == "yes":
            return result("CONFIRMATION", "MESSAGE_SEND_CONFIRMATION_YES")
        if a == "back":
            return result("CONFIRMATION", "MESSAGE_SEND_CONFIRMATION_CHANGE")
        if a in ("no", "cancel"):
            return result("CONFIRMATION", "cancel")

    if handler == "CALENDAR_ADD_EVENT_OR_CHANGE_CONFIRMATION":
        a = interpret_answer(query)
        if a == "yes":
            return result("CONFIRMATION",
                          "CALENDAR_ADD_EVENT_OR_CHANGE_CONFIRMATION_ADD_EVENT")
        if a == "back":
            return result("CONFIRMATION",
                          "CALENDAR_ADD_EVENT_OR_CHANGE_CONFIRMATION_CHANGE")
        if a in ("no", "cancel"):
            return result("CONFIRMATION", "cancel")

    if handler == "NOTE_CREATE_CONFIRMATION":
        a = interpret_answer(query)
        if a == "yes":
            return result("CONFIRMATION", "NOTE_CREATE_CONFIRMATION_YES")
        if a == "back":
            # NB: NOTE_CREATE_CHANGE, not ..._CONFIRMATION_CHANGE (doc 7.1)
            return result("CONFIRMATION", "NOTE_CREATE_CHANGE")
        if a in ("no", "cancel"):
            return result("CONFIRMATION", "cancel")

    if handler == "REMINDER_CREATE_CONFIRMATION":
        a = interpret_answer(query)
        if a == "yes":
            return result("CONFIRMATION", "REMINDER_CREATE_CONFIRMATION_YES")
        if a == "back":
            return result("CONFIRMATION", "REMINDER_CREATE_CONFIRMATION_CHANGE")
        if a in ("no", "cancel"):
            return result("CONFIRMATION", "cancel")

    if handler == "ALARM_INPUT":  # "What time?"
        tod = extract_time(query)
        if tod:
            return result("ALARM_INPUT", "ALARM_INPUT",
                          [ent("TIME", epoch_ms(next_occurrence(*tod)))])
        return result("CONFIRMATION", "cancel")

    if handler == "ALARM_TIMER_INPUT":  # "For how long?"
        ms = extract_duration(query)
        if ms is not None:
            return result("ALARM_START_TIMER", "ALARM_START_TIMER",
                          [ent("DURATION", ms)])
        return result("CONFIRMATION", "cancel")

    if handler == "INPUT_TIME_ANDOR_DATE":  # "When?" (Notes/Calendar)
        pending = _pending_time_intent.pop(session, None)
        tod = extract_time(query, default_pm="sera" in t or
                           "evening" in t or "tonight" in t or
                           "вечерта" in t or "следобед" in t or
                           "през нощта" in t)
        if pending and tod:
            primary, secondary, entities = pending
            entities = [e for e in entities if e["type"] != "TIME"]
            entities.append(ent("TIME", epoch_ms(next_occurrence(*tod))))
            return result(primary, secondary, entities)
        if tod:
            return result("REMINDER_SET", "REMINDER_SET",
                          [ent("TIME", epoch_ms(next_occurrence(*tod)))])
        return result("CONFIRMATION", "cancel")

    if handler.startswith("MESSAGE_") and handler.endswith("_ACTIONS"):
        if re.search(r"\bdelete|cancell|elimin|изтрий", t):
            return result("MESSAGING_READ", "MESSAGING_READ_DELETE")
        if re.search(r"\breply all|rispondi a tutti|отговори на всички", t):
            return result("MESSAGING_READ", "MESSAGING_READ_REPLYALL")
        if re.search(r"\breply|rispondi|отговори", t):
            return result("MESSAGING_READ", "MESSAGING_READ_REPLY")
        if re.search(r"\bforward|inoltr|препрати", t):
            return result("MESSAGING_READ", "MESSAGING_READ_FORWARD")
        if re.search(r"\bnext|prossim|avanti|следващ", t):
            return result("MESSAGING_READ", "MESSAGING_READ_GO_TO_NEXT")
        if re.search(r"\bunread|non lett|непрочетен", t):
            return result("MESSAGING_READ", "MESSAGING_READ_MARK_UNREAD")
        if re.search(r"\bflag|contrassegn|маркирай", t):
            return result("MESSAGING_READ", "MESSAGING_READ_FLAG_FOLLOWUP")
        if re.search(r"\bcall|chiam|обади", t):
            return result("MESSAGING_READ", "MESSAGING_READ_CALL_BACK")
        if re.search(r"\bread|leggi|прочети", t):
            return result("MESSAGING_READ", "MESSAGING_READ_READ")
        if re.search(r"\bdone|nothing|fine|niente|готово|нищо|край", t):
            return result("MESSAGING_READ", "MESSAGING_READ_DONE")
        if interpret_answer(query) == "cancel":
            return result("CONFIRMATION", "cancel")
        return result("MESSAGING_READ", "MESSAGING_READ_DONE")

    return None


def split_contact_and_message(t, verbs):
    """'chiama marco', 'sms a marco arrivo alle 8',
    'съобщение на иван че закъснявам' -> (contact, message)."""
    m = re.search(r"(?:а|to|на)\s+([\w' ]+?)(?:\s+(?:che|that|dicendo|"
                  r"saying|че|:)\s+(.+))?$", t)
    contact, message = None, None
    if m:
        contact = m.group(1).strip()
        message = (m.group(2) or "").strip() or None
    if contact is None:
        for v in verbs:
            m = re.search(v + r"\s+([\w' ]+)$", t)
            if m:
                contact = m.group(1).strip()
                break
    return contact, message


def classify_blank(query, session):
    """New command (handlerType BLANK): LLM first, rules as fallback."""
    if _LLM["url"]:
        out = llm_classify(query)
        if out:
            # Remember time-less reminders/calendar events so the "When?"
            # follow-up (INPUT_TIME_ANDOR_DATE) can re-send them (doc 7.2).
            if out["secondaryClassification"] in (
                    "REMINDER_SET", "CALENDAR_CREATE_EVENT") and not any(
                    e["type"] in ("TIME", "TIMERANGE")
                    for e in out["entities"]):
                _pending_time_intent[session] = (
                    out["primaryClassification"],
                    out["secondaryClassification"],
                    list(out["entities"]))
            return out
    return classify_blank_rules(query, session)


_BG_APPS = {
    "браузър": "Browser", "браузъра": "Browser", "камера": "Camera",
    "камерата": "Camera", "часовник": "Clock", "часовника": "Clock",
    "калкулатор": "Calculator", "калкулатора": "Calculator",
    "контакти": "Contacts", "контактите": "Contacts",
    "календар": "Calendar", "календара": "Calendar",
    "карти": "Maps", "картите": "Maps", "снимки": "Pictures",
    "снимките": "Pictures", "видео": "Videos", "настройки": "Settings",
    "настройките": "Settings", "файлове": "File Manager",
    "файловете": "File Manager", "поща": "Email", "хъб": "Hub",
    "хъба": "Hub", "телефон": "Phone", "телефона": "Phone",
}


def classify_blank_rules(query, session):
    t = query.lower().strip()
    pm_hint = ("sera" in t or "вечерта" in t or "следобед" in t or
               "през нощта" in t)

    # --- Clock: alarm -------------------------------------------------
    if re.search(r"\bsveglia|svegliami|\balarm|wake me|аларм|събуди", t):
        entities = []
        tod = parse_time_of_day(query, default_pm=pm_hint)
        if tod:
            entities.append(ent("TIME", epoch_ms(next_occurrence(*tod))))
        return result("ALARM_INPUT", "ALARM_INPUT", entities)

    # --- Clock: timer (unmapped primary = secondary, doc 6) -----------
    if re.search(r"\btimer|conto alla rovescia|countdown|таймер|"
                 r"обратно броене", t):
        entities = []
        ms = parse_duration_ms(query)
        if ms is not None:
            entities.append(ent("DURATION", ms))
        return result("ALARM_START_TIMER", "ALARM_START_TIMER", entities)

    # --- Phone --------------------------------------------------------
    if re.search(r"\bchiama|telefona|\bcall\b|\bphone\b|\bdial\b|обади|"
                 r"набери|звънни|звън", t):
        digits = re.sub(r"\D", "", t)
        entities = []
        m = re.search(r"(?:chiama|telefona a|call|phone|dial|обади се на|"
                      r"обади|набери|звънни на|звънни)\s+([\w' ]+?)$", t)
        if digits and len(digits) >= 3 and (
                re.search(r"\d{3,}", t)):
            entities.append(ent("PHONENUMBER", digits))
        elif m:
            entities.append(ent("CONTACTNAME", m.group(1)))
        return result("CALL", "CALL_CALLING", entities)

    # --- SMS ----------------------------------------------------------
    if re.search(r"\bmessaggio|\bsms\b|\btext\b|manda un messaggio|"
                 r"съобщение|смс|\bпиши\b", t):
        contact, message = split_contact_and_message(
            t, [r"messaggio a", r"sms a", r"text", r"съобщение на",
                r"смс на", r"пиши на"])
        entities = []
        if contact:
            entities.append(ent("CONTACTNAME", contact))
        if message:
            entities.append(ent("MESSAGE", message))
        return result("MESSAGING_SEND", "MESSAGING_SEND_TEXT", entities)

    # --- E-mail -------------------------------------------------------
    if re.search(r"\be-?mail|имейл|мейл", t):
        contact, message = split_contact_and_message(
            t, [r"e-?mail a", r"имейл на", r"мейл на"])
        entities = []
        addr = re.search(r"\S+@\S+", t)
        if addr:
            entities.append(ent("EMAILADDRESS", addr.group(0)))
        elif contact:
            entities.append(ent("CONTACTNAME", contact))
        if message:
            entities.append(ent("BODY", message))
        return result("MESSAGING_SEND", "MESSAGING_SEND_EMAIL", entities)

    # --- Read messages / Hub ------------------------------------------
    if re.search(r"\bleggi|read my|notifications|notifiche|messaggi|"
                 r"прочети|съобщенията|известията", t) \
            and re.search(r"\bleggi|read|notifiche|notifications|"
                          r"прочети", t):
        return result("MESSAGING_READ", "MESSAGING_READ_READ")

    # --- Note ---------------------------------------------------------
    if re.search(r"\bnota\b|\bnote\b|scrivi|prendi nota|бележка", t):
        m = re.search(r"(?:nota|note|scrivi|prendi nota|бележка)[:\s]+(.+)$",
                      t)
        entities = [ent("MESSAGE", m.group(1))] if m else []
        return result("NOTE", "NOTE_SET", entities)

    # --- Reminder -------------------------------------------------------
    if re.search(r"\bpromemoria|\breminder\b|ricordami|remind me|напомни|"
                 r"напомняне", t):
        entities = []
        m = re.search(r"(?:ricordami di|remind me to|promemoria[:\s]+|"
                      r"напомни ми(?: да)?|напомняне[:\s]+)"
                      r"(.+?)(?:\s+(?:alle|at|domani|tomorrow|в|към|утре|"
                      r"днес).*)?$", t)
        if m:
            entities.append(ent("MESSAGE", m.group(1).strip()))
        tod = parse_time_of_day(query, default_pm=pm_hint)
        if tod:
            entities.append(ent("TIME", epoch_ms(next_occurrence(*tod))))
        else:
            _pending_time_intent[session] = (
                "REMINDER_SET", "REMINDER_SET", list(entities))
        return result("REMINDER_SET", "REMINDER_SET", entities)

    # --- Music ----------------------------------------------------------
    if re.search(r"\bpause|pausa|пауза|спри\b", t) and \
            re.search(r"music|musica|canzone|музика|песен", t):
        return result("MUSIC", "MUSIC_PAUSE")
    if re.search(r"\bnext|prossima|skip|следващ", t) and \
            re.search(r"track|song|canzone|brano|песен|музика", t):
        return result("MUSIC", "MUSIC_NEXT_TRACK")
    if re.search(r"\bprevious|precedente|предишн", t) and \
            re.search(r"track|song|canzone|brano|песен|музика", t):
        return result("MUSIC", "MUSIC_PREVIOUS_TRACK")
    if re.search(r"\bplay\b|riproduci|metti|musica|\bmusic\b|музика|"
                 r"песен|свири", t):
        entities = []
        m = re.search(r"(?:play|riproduci|metti|свири|пусни)\s+"
                      r"(?:di\s+|by\s+)?(.+)$", t)
        if m and m.group(1) not in ("musica", "music", "музика"):
            entities.append(ent("ARTIST", m.group(1).strip()))
        return result("MUSIC", "MUSIC_PLAY", entities)

    # --- System settings (UNMAPPED primary so SETTING/VALUE pass through,
    #     doc 5.2 rule 3) --------------------------------------------------
    SETTING_MAP = [
        (r"wi-?fi|\bwlan\b|вайфа|уайфа", "WIFI"),
        (r"bluetooth|блутут", "BLUETOOTH"),
        (r"torcia|flashlight|фенер", "FLASHLIGHT"),
        (r"aereo|airplane|самолет", "AIRPLANE"), (r"\bnfc\b", "NFC"),
        (r"rotazione|rotation|ротация|завъртане", "ROTATIONLOCK"),
        (r"gps|location|posizione|локация|местоположение",
         "LOCATIONSERVICES"),
        (r"dati|mobile data|rete|мобилни данни", "NETWORK"),
        (r"luminosit|brightness|яркост", "BRIGHTNESS"),
        (r"volume|звук|сила на звука", "VOLUME"),
        (r"batteria|battery|батерия", "BATTERY"),
    ]
    for pat, setting in SETTING_MAP:
        if re.search(pat, t) and re.search(
                r"\b(on|off|attiva|disattiva|accendi|spegni|enable|disable|"
                r"turn|check|alza|abbassa|increase|decrease|включи|"
                r"изключи|пусни|спри|вдигни|намали|усили|увеличи|"
                r"провери)\b", t):
            value = None
            amount = None
            if re.search(r"\bcheck|com'è|quanta|status\b|провери|"
                         r"колко е", t):
                value = "CHECK"
            elif re.search(r"\b(on|attiva|accendi|enable|turn on)\b|"
                           r"включи|пусни", t):
                value = "ON"
            elif re.search(r"\b(off|disattiva|spegni|disable|turn off)\b|"
                           r"изключи|спри", t):
                value = "OFF"
            elif re.search(r"\b(alza|increase|up)\b|вдигни|усили|увеличи",
                           t):
                value = "INCREASE"
            elif re.search(r"\b(abbassa|decrease|down)\b|намали", t):
                value = "DECREASE"
            m = re.search(r"\b(\d{1,3})\s*%", t)
            if m:
                amount = m.group(1)
                value = None
            entities = [ent("SETTING", setting)]
            if value:
                entities.append(ent("VALUE", value))
            if amount:
                entities.append(ent("AMOUNT", amount))
            return result("PHONE_SETTINGS", "APPLICATION_PHONE_SETTINGS",
                          entities)

    # --- Weather --------------------------------------------------------
    if re.search(r"\bmeteo|\bweather\b|che tempo|времето|прогноза|"
                 r"какво е времето", t):
        entities = []
        m = re.search(r"(?:a|in|at|в|за)\s+([\w' ]+)$", t)
        if m and not re.search(r"meteo|weather|времето|прогноза",
                               m.group(1)):
            entities.append(ent("LOCATION", m.group(1).strip()))
        return result("WEATHER", "WEATHER_STATUS", entities)

    # --- Open app (normally caught locally, harmless fallback) ---------
    m = re.search(r"(?:apri|open|launch|avvia|пусни|отвори|стартирай|"
                  r"старта|старт|опан|оупън|оупен)[\s,.!?]+(.+)$", t)
    if m:
        appname = m.group(1).strip().strip('.,!?').strip()
        appname = _BG_APPS.get(appname, appname)
        return result("APPLICATION", "APPLICATION",
                      [ent("APPNAME", appname)])

    # --- Knowledge -> Wolfram spoken answer ----------------------------
    if re.search(r"^(chi è|chi era|cos'è|che cos'è|cosa significa|"
                 r"quanto fa|who is|what is|what's|how much|how many|"
                 r"when did|кой е|коя е|какво е|какво означава|"
                 r"кога е|колко е)", t):
        return result("KNOWLEDGE", "KNOWLEDGE_WOLFRAM",
                      [ent("QUERY", query)])

    # --- Fallback: web search -------------------------------------------
    return result("SEARCH", "SEARCH_DEFAULT", [ent("SEARCHQUERY", query)])


# ---------------------------------------------------------------------------
# Maluuba protocol (docs/04-maluuba.md)
# ---------------------------------------------------------------------------

def handle_run_pipeline(query: dict) -> dict:
    try:
        inp = json.loads(unquote_plus(query.get("input", [""])[0]))
    except Exception:
        inp = {}
    try:
        rinfo = json.loads(unquote_plus(query.get("requestInfo", ["{}"])[0]))
    except Exception:
        rinfo = {}
    session = rinfo.get("sessionId") or rinfo.get("deviceId") or "default"

    text = inp.get("query", "")
    handler = (inp.get("dialogueHandler") or {}).get("handlerType", "BLANK")
    # whisper's detected language for the audio just before this query
    lang_note = ""
    if _WHISPER_INFO["lang"] and time.time() - _WHISPER_INFO["ts"] < 60:
        lang_note = (f" heard={_WHISPER_INFO['lang']}"
                     f"({_WHISPER_INFO['prob']:.2f})")
    print(f"[maluuba] session={session} handler={handler}{lang_note} "
          f"query={text!r}")

    out = None
    if handler and handler != "BLANK":
        out = classify_followup(handler, text, session)
    if out is None:
        out = classify_blank(text, session)

    print(f"[maluuba] -> {out['primaryClassification']}/"
          f"{out['secondaryClassification']} {out['entities']}")
    return out


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "BB10Assistant/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[http] {datetime.now().strftime('%H:%M:%S')} "
              f"{self.client_address[0]} " + fmt % args)

    def _send(self, body: bytes, content_type: str, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj).encode(), "application/json", status)

    def _read_body(self) -> bytes:
        """Read the request body. The phone sends Dragon audio with
        `Transfer-Encoding: chunked` — decode it, otherwise the chunk
        frames poison the next request on the keep-alive connection."""
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            body = bytearray()
            while True:
                line = self.rfile.readline(65536).strip()
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    while True:  # trailers
                        t = self.rfile.readline(65536)
                        if t in (b"\r\n", b"\n", b""):
                            break
                    break
                body += self.rfile.read(size)
                self.rfile.read(2)  # chunk CRLF
            return bytes(body)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_POST(self):
        body = self._read_body()
        path = urlparse(self.path).path
        ctype = self.headers.get("Content-Type", "")

        if "multipart/form-data" in ctype:  # Dragon NVC (ASR + data upload)
            self._send(dragon_response_parse(body, ctype),
                       f"multipart/form-data; boundary={BOUNDARY}")
        elif path.startswith("/delegate/"):  # syncDeviceData, test, ...
            self._json({})
        else:
            self._json({})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        q = parse_qs(parsed.query)

        if path.endswith("/delegate/asrBegin"):
            self._json({"sessionId": str(uuid.uuid4()),
                        "_version": 1, "_interpretations": []})
        elif path.endswith("/delegate/runPipeline"):
            self._json(handle_run_pipeline(q))
        elif path == "/wa/recognizer" or path == "/wa/queryrecognizer":
            import wa_engine
            question = (q.get("i") or q.get("q") or [""])[0]
            self._send(wa_engine.recognizer_xml(question).encode("utf-8"),
                       "application/xml")
        elif path == "/wa/q" or path == "/wa/query":
            import wa_engine
            question = (q.get("input") or q.get("i") or [""])[0]
            base = "http://" + (self.headers.get("Host") or
                                "192.168.0.157:8080")
            body, ctype = wa_engine.queryresult_xml(question, base)
            self._send(body, ctype)
        elif path.startswith("/wa/img/"):
            import wa_engine
            png, _h = wa_engine.get_image(path.rsplit("/", 1)[-1])
            if png:
                self._send(png, "image/png")
            else:
                self._json({}, status=404)
        elif path in ("", "/", "/health"):
            self._json({"ok": True})
        else:
            self._json({}, status=404)


def dragon_response_parse(body, ctype):
    return handle_dragon(body, ctype)


def main():
    global _FAKE_TRANSCRIPT
    ap = argparse.ArgumentParser(description="BB10 Assistant reference server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--cert", help="TLS certificate PEM (HTTPS)")
    ap.add_argument("--key", help="TLS private key PEM (HTTPS)")
    ap.add_argument("--fake", default=os.environ.get("BB10_FAKE_TRANSCRIPT"),
                    help="fixed transcript instead of a real recogniser")
    ap.add_argument("--llm-url", default=os.environ.get("BB10_LLM_URL"),
                    help="OpenAI-compatible base URL, e.g. "
                         "http://127.0.0.1:11434/v1 (Ollama)")
    ap.add_argument("--llm-model", default=os.environ.get("BB10_LLM_MODEL"),
                    help="model name, e.g. qwen2.5:7b-instruct")
    ap.add_argument("--llm-key", default=os.environ.get("BB10_LLM_KEY"),
                    help="API key, if the endpoint needs one")
    args = ap.parse_args()
    _FAKE_TRANSCRIPT = args.fake
    _LLM["url"] = args.llm_url
    _LLM["model"] = args.llm_model
    _LLM["key"] = args.llm_key

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    scheme = "http"
    if args.cert and args.key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.cert, args.key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    print(f"BB10 Assistant server on {scheme}://{args.host}:{args.port}")
    print(f"  Dragon : POST {scheme}://<host>/dragon/NmspServlet/")
    print(f"  Maluuba: GET  {scheme}://<host>/delegate/asrBegin|runPipeline")
    if _FAKE_TRANSCRIPT is not None:
        print(f"  Recogniser: FAKE -> {_FAKE_TRANSCRIPT!r}")
    if _LLM["url"]:
        print(f"  NLU: LLM {_LLM['model']} @ {_LLM['url']} "
              "(rules as fallback)")
    else:
        print("  NLU: rule-based only (set --llm-url/--llm-model for LLM)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()

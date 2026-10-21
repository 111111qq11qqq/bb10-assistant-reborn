# BB10 Assistant Reborn — Knowledge Base

Пълна техническа документация за възраждането на гласовия асистент на BlackBerry 10.
Последна актуализация: 2026-10-20. Тествано на: Z30 (10.3.3.3216, root autoloader v2), Passport.

---

## 1. Архитектура

```
BB10 телефон                         PC (сървър)
┌─────────────────────┐              ┌──────────────────────────────┐
│ Assistant UI (тач)  │              │ server.py (Python, порт 8080)│
│ io-asr (патчнат)    │── HTTP ────► │  /dragon/NmspServlet/  (ASR) │
│  Dragon ──► сървър  │              │  /delegate/runPipeline (NLU) │
│  Maluuba ─► сървър  │              │  faster-whisper (base, int8) │
└─────────────────────┘              └──────────────────────────────┘
```

- **ASR**: телефонът праща PCM 16kHz mono s16le (multipart/form-data) към `/dragon/NmspServlet/`. Сървърът транскрибира с whisper и връща NLSML в multipart отговор.
- **NLU**: телефонът праща текста към `/delegate/runPipeline?input=...&requestInfo=...`. Сървърът класифицира (rule-based, BG+EN) и връща intent JSON (`primaryClassification`, `secondaryClassification`, `entities`).
- Двата протокола са документирани в `docs/` на проекта bb10-assistant-protocol.

## 2. Сървърът

### Стартиране (Windows, PowerShell)
```powershell
$env:BB10_WHISPER_MODEL='base'   # base = бърз (~6s); small = по-точен за BG (~30s)
$env:BB10_WHISPER_LANG='auto'    # auto = whisper сам познава езика; 'bg' форсира български
$env:BB10_WHISPER_BEAM='1'
Start-Process python -ArgumentList '-u','server.py','--port','8080' `
  -WorkingDirectory E:\bb10-assistant-server `
  -RedirectStandardOutput server_live.log -RedirectStandardError server_live.err
```

**ВАЖНО**: ползвай Python, в който `faster_whisper` и `numpy` са налични. Проверка: `python -c "import faster_whisper"`. Ако логът каже „no recogniser available“, сървърът връща тишина и телефонът пада в търсене/браузър.

### Ключови бележки за server.py
- `dragon_response()` ползва `json.dumps(..., ensure_ascii=False)` — **задължително**! С `ensure_ascii=True` кирилицата става `\uXXXX`, телефонният scanner не го декодира и вади само пунктуацията (симптом: query=' .' → SEARCH → отваря браузър).
- Нечетен брой PCM байтове от поточен upload се подрязва (`del pcm[-1:]`), иначе numpy гърми.
- Всяко входящо аудио се записва в `last_dragon.wav` + печатат се rms/peak — основен дебъг инструмент.
- replay тест: изпрати `last_dragon.wav` пак към сървъра (multipart, part `RequestData` с `cmdName=NVC_ASR_CMD` + part `ConcludingAudioParameter` с paramName="AUDIO_INFO").

## 3. Пач рецепта на io-asr

База: оригинален `io-asr-bb10` (cksum 3766695266). Готовият патчнат е `files/io-asr-patched.bin` (cksum 3587830232, URL `XXhttp://192.168.0.157:8080`).

| Офсет | Промяна | Защо |
|---|---|---|
| 0x2a42d4 | стринг `XXhttp://<IP>:<порт>\0` (131 байта поле) | URL на сървъра |
| 0x27ed78 | uint32 LE = 0x2556c | literal pool за URL |
| 0x3631cc | същият стринг (32 байта поле) | втори референт |
| 0x27e646/52/5e/6a/78/82 | `00 bf 00 bf` | NOP-ва SSL/хост проверки |
| 0x27e5fc | `bb b9`→`17 e0` | заобикаля клон |
| 0x27e77e..81 | `01 28 0a d0`→`00 bf 00 bf` (И двете двойки!) | cert check |
| 0x27f4b4, 0x27ea10 | `01 23`→`00 23` | flag fix |
| 0x27e7d8 | `32 e0`→`20 e0` | debug URL log |

`XX` префиксът компенсира 2-байтово отрязване от io-asr (иначе curl получава `tp://...`).
За нов сървърен IP: `python patch_server_ip.py files/io-asr-patched.bin <IP> <порт>`.

## 4. Root и достъп

- Root autoloader (напр. `Z30_10.3.03.3216...root_v2.exe`): вгражда Term49 + getroot (rcfs overlays на /base/bin, /base/sbin; `__root`, `g_nto` wrapper-и). `__root` дава EPERM за app uids.
- ssh: `ssh -i ~/.ssh/bb10_root_rsa -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@<IP>`
  - USB: 169.254.0.1 (PC страната: статичен 169.254.0.2/30)
- scp НЕ работи; качване: `ssh ... 'cat > /path' < file`
- exec ограничение: неподписани бинарници от /var/rootdata, /tmp, app dirs → EPERM. Обход: `on -u 109:109,50,200 /var/rootdata/<bin> -c ...` (spawn от root контекст минава). Скриптове: `sh < script.sh` (НЕ `sh script.sh`).

## 5. Deploy по модели

| Модел | Микрофон (device-name) | Бележка |
|---|---|---|
| Z30 | `/dev/snd/pcmPreferredc` | mic fix нужен (asr_z30.cfg) |
| Passport | `/dev/snd/pcmpreferred` (фабричен /base/etc/asr.cfg) | диагностика продължава; стоковият конфиг е отправна точка |
| други | започни от фабричния `/base/etc/asr.cfg` | deploy.sh го дърпа автоматично |

Стъпки (виж deploy.sh): patch binary → качи файлове → slay услуги → PPS settings → старт през asr_boot.sh → verify.

### PPS settings (задължителни)
```
MaluubaEnabled:n:1          # БЕЗ това върви embedded vocon и не вди)
RecognitionMode::Network
MaluubaHost::http://<IP>:8080
```
**PPS quirk**: /pps/services/asr/settings се презаписва от UI приложението и io-asr. Пиши САМО след slay и веднага преди старт. Persistence файлът е `/var/pps/services@0.0.775/asr@109.109.750/settings@109.109.660`.

### Dragon prompts overlay
`ln -sPf /accounts/1000/sysdata/asr/my_prompts.cfg /base/usr/asr/recognition/en-US/dragon/prompts.cfg`

## 6. Дистанционно управление (root ssh)

```bash
# Събуждане на екрана (симулация на power бутон):
echo "bid_power::b_down" >> /pps/system/buttons/status; sleep 1
echo "bid_power::b_up"   >> /pps/system/buttons/status

# Задържане на power (рестарт при забиване): b_down, изчакай ~10s, b_up

# Пускане на слушане (само при отворена сесия/екран):
echo "strobe::on" >> /pps/services/asr/control

# Състояние на ASR:
cat /pps/services/asr/control        # state::idle/listening/processing/prompting
cat /pps/services/asr/audio_energy   # level:n:<0-100> - живи нива на микрофона

# Пущане на звук от говорителя на телефона:
audio_out -i /var/rootdata/file.wav
```

**Ограничения на strobe**: игнорира се при изгаснал/заключен екран или без отворена assistant сесия („in_session“). Надеждният път е физически бутон в UI.

## 7. Troubleshooting

| Симптом | Причина | Решение |
|---|---|---|
| „voice not understood“ веднага, без да слуша | върви стоков io-asr (сочи мъртвия Nuance) | pidin: трябва `asr-patched-final`, не `io-asr-bb10`; пусни asr_boot.sh |
| Отваря браузъра при всяка команда | транскриптът е празен/боклук | гледай server_live.log `[dragon] transcript:` |
| query=' .' в лога | JSON \u-escape чупи кирилицата | ensure_ascii=False в dragon_response |
| „audio dump failed: buffer size...“ | нечетен PCM размер | подрязване на последния байт (вградено) |
| „no recogniser available“ | python без faster_whisper | рестартирай сървъра с правилния venv |
| strobe не прави нищо | екранът е изгаснал/няма сесия | събуди с bid_power; отвори асистента |
| Силен клипинг (peak=1.0) при тест с друг телефон | говорителят е прекалено силен за микрофона | намали силата на източника ~50% |
| ssh към телефона умира след буут | телефонът заспива/услуги закъсняват | Wi-Fi връзка е по-стабилна от USB |
| Settings черен екран след загуба на root | rcfs overlays счупени | префлаш с root autoloader |

## 8. Тестов тракт без присъствие (доказан)

- Unihertz/Android до BB10: adb през Wi-Fi/USB → Termux + `pkg i sox` → `play /sdcard/test_voice.wav repeat 5` (гласов файл в /sdcard).
- PC с HDMI към изключен телевизор НЕ върши работа за звук — ползвай втори телефон.
- scrcpy за жив екран на Android: `scrcpy -s <serial> --stay-awake --turn-screen-on`.

## 9. Whisper модели за български (стара CPU машина)

| Модел | BG качество | Латентност (3s команда) |
|---|---|---|
| base (текущ) | средно; бърка при тих/клипнат запис | ~6 s |
| small | видимо по-добро | ~20-45 s |
| large-v3-turbo | най-добро | непрактично на този CPU |

Реализирано: имената от контактите се добавят автоматично към whisper prompt-а. Сървърът пази `contacts.txt` (до 60 имена) до server.py; при всяка реплика prompt-ът се допълва с „Контакти: …“. Източници: (1) ръчно редактируем файл; (2) автоматично — телефонът праща контакт-синк (NVC_DATA_UPLOAD_CMD), сървърът вади само display имената (JSON или vCard) и ги добавя в contacts.txt. Суровият sync се пише в contact_upload_<ts>.bin за изучаване. Забележка: след флаш телефоните нямат контакти — файлът се пълни при първия реален sync.

## 10. Език на отговора (EN/BG)

- Whisper познава езика на всяка реплика (`BB10_WHISPER_LANG=auto`) и го логва: `[whisper] detected lang=bg p=0.93`.
- **Езиците са ограничени до bg/en.** Whisper често бърка българска реч с ru/uk/hr/mk (близка кирилска фонетика). Ако detection-ът каже нещо различно от bg/en → задължителен втори прогон с форсиран `bg`. Ако каже `en`, а текстът е с кирилица (prompt bias) → втори прогон с форсиран `en`. Лог: `[whisper] detected ru (unsupported) -> forcing bg`.
- Търсенето следва езика автоматично: SEARCHQUERY е точно чутият текст — питаш на български → търси на български; на английски → на английски. Не се превежда нищо.
- Текстът на екрана (транскриптът) също е на чутия език.
- Говорим отговор (TTS): BB10 няма български TTS глас — българският текст си остава само на екрана. Английски отговори телефонът може да изговори, но само през пачнатия Wolfram plugin (`libWolframPlugin.so`, docs/08 в оригиналния protocol repo) — все още НЕ е деплойнат на телефоните.
- Потвърждения: „да/ок/окей/кей/сейв/съхрани/давай/действай" = yes; „кенсъл/кенсел/кансъл/кансел/откажи" = cancel (фонетични варианти за шепота на whisper).

## 11. Wolfram plugin patch — говорими/текстови отговори (доказано на живо)

**Какво дава**: въпрос („кой е…", „who is…") → картичка с отговор на екрана + TTS глас на английски. На български — само картичка (BB10 няма BG TTS глас), както е по дизайн. Отговорите идват от Wikipedia (bg/en според езика на въпроса), fallback: DuckDuckGo. Докосване на картичката → Google търсене.

**Файл**: `/apps/sys.search.gYABgPp5WMkB_07CE6wzbflslRQ/native/lib/libWolframPlugin.so`
Стокова версия: 116 272 bytes, cksum **2029093847** (еднаква на Z30-1, Z30-2, Q10). Пачната: cksum **1580988818**.

**Пач (patch_wolfram.py)** — офсети от docs/08 на protocol repo, верифицирани байт по байт:
| Офсет | Оригинал | Става |
|---|---|---|
| 0xe0a1 | `http://www.wolframalpha.com/queryrecognizer/query.jsp` (54B място) | `http://192.168.0.157:8080/wa/recognizer` |
| 0xde1d | `http://api.wolframalpha.com/v2/query` (37B) | `http://192.168.0.157:8080/wa/q` |
| 0xb170 | `55 d0` (beq — accept проверка) | `00 bf` (NOP) |
| 0xb1ec | `7d b1` (cbz — accept проверка) | `00 bf` (NOP) |
| 0xd8ab | `http://m.wolframalpha.com/input/` (32B) | `https://www.google.com/search` |

**Деплой** (root ssh): backup в /var/rootdata/libWolframPlugin.so.orig → `cat > $LIB/libWolframPlugin.so.tmp` → `chmod 644` → `mv -f tmp → .so` → проверка cksum. Plugin-ът се зарежда при следващо отваряне на асистента (ако върви: убий процеса `app/native/libUniversalSearch.so.1.0.0`).

**Сървърни endpoint-и** (server.py + wa_engine.py):
- `/wa/recognizer?i=<въпрос>` → queryrecognizer XML, accepted=true
- `/wa/q?input=<въпрос>` → queryresult XML: 2 пода (картичка 684px + 1px spacer — задължителни два, иначе „No pods found"), BG без `spokenresult`, EN с `spokenresult` (първите 2 изречения)
- `/wa/img/<id>.png` → генерираната картичка (PIL, DejaVuSans — пълна кирилица); `/wa/img/spacer.png` → 1×1 прозрачен
- Plugin-ът чака до 30 s за отговор — Wikipedia отговаря за 1-3 s

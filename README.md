# SwaMedia → MP4 → Hugging Face PRIVATE

Converter tofauti na ya Absalom/Ngwele (haigusi app ya sasa). **Haipo** payment/app mpya hapa — angalia sehemu 7.

## 1. Kuweka GitHub
1. Weka folda hii kwenye repo mpya (private inapendekezwa), mfano `swamedia-converter`.
2. Settings → Secrets and variables → Actions:
   - **Secrets**: `HF_TOKEN` (write, dataset husika tu), `GDRIVE_SERVICE_ACCOUNT_JSON` (service account; shiriki folders na barua pepe yake kama Viewer).
   - **Variables**: `HF_REPO` (dataset **PRIVATE**, hakuna default), `ENABLE_CONVERSION` = `false` (anza hivyo).
3. Tengeneza dataset private HF mwenyewe. Kama ni public, kazi **inasimama** na haibadilishi chochote.

## 2. Hatua (usiruke)
1. **discover** (Actions → swamedia-converter → mode=discover). Inasoma API ya orodha tu; haidownload. Pata artifact `review_manifest.json` + `discovery_report.json`. Linganisha `reported_total` na `collected`; pengo likiwepo ni warning, usiseme "zote zimepatikana".
2. Mmiliki anahakiki items, kisha anaunda `approved_items.json` kwenye root ya repo (angalia `schemas/approved_items.example.json`; nakili `source_version` kutoka review manifest). Metadata ikibadilika, idhini inakuwa batili na item inarudi review.
3. **dry-run** (inahitaji Drive credentials): inatembelea folders na kutoa idadi ya tasks (`approved`/`needs_review`/`excluded`), conflicts, permission_denied. Hakuna download.
4. Weka `ENABLE_CONVERSION=true`, kisha **convert** na `only_item_id=<id>` `max_files=1`. Hakiki read-back ya faili kwenye HF + playback ya byte-range. Ndipo ufanye `max_files=5`.

## 3. Sheria za maudhui (fail closed)
`/adult`, `/wakubwa` hazifunguliwi. Kila rekodi inakaguliwa kwa metadata yake (type, category/genre, age_rating, flags, title, URL); navigation haihesabiwi. 18+/adult → `excluded` (hakuna force include; inashinda approval yoyote). Rating tupu/inayokinzana/isiyotambulika → `needs_review`. Salama = `source_regular`, **si** `content_verified`. Majina ya faili/folder zenye adult marker huzuiwa. Item ikiwa excluded baadaye: inaondolewa kwenye `manifest/serving.json` mara moja na `reports/purge_report.json` inaorodhesha MP4 zilizopo; **hakuna kufuta bila idhini yako tofauti**.

## 4. Muundo wa data kwenye dataset ya private
`videos/swamedia/{item_id}/{drive_file_id}.mp4`, `state/tasks.json`, `manifest/serving.json` (media_id ya opaque, bila Drive/HF URLs), `reports/purge_report.json`. Resume key = item + Drive file + checksum/modifiedTime + toleo la settings. Faili 5 zikishindwa: cooldown ya exponential (juu 24h), si kufungwa milele; 401/403 → `blocked` mpaka `--manual-retry`/ruhusa mpya.

## 5. Amri za ndani
```bash
pip install -r requirements-dev.txt   # na ffmpeg
python -m pytest -q tests
python -m swamedia.cli --mode discover --out out
```

## 6. Mapungufu yanayojulikana
- Endpoint ya episodes za SwaMedia haijathibitishwa; tunategemea Drive folders. Episodes za details-page (`.episode-card`) hazijaunganishwa bado.
- Links zisizo Google Drive → `unsupported_source` (hakuna kujaribu kuzifungua).
- `age_rating=16+` kwa rekodi zote inaweza kuwa default; ndiyo maana review ya mmiliki ni lazima.
- Faili za video kubwa sana: runner ya GitHub ina diski ndogo; disk guard itakataa faili isiyotosha.

## 7. App ya malipo (hatua inayofuata, haijajengwa)
Server ithibitishe session + entitlement ya **item sahihi** kabla ya bytes; HF_TOKEN/Drive/HF URLs zisimfikie mtazamaji. Playback ya private HF inahitaji proxy yenye `Range`→`206`, `Content-Range`, `Accept-Ranges`, `Content-Length`, streaming bila kupakia faili kwenye memory. Hakiki limits za host (Lovable/TanStack Start: server routes/functions zinazoungwa mkono), masharti ya HF, egress. Bei/gateway zithibitishwe na mmiliki.

## 8. Cloudflare D1
Schema: `d1/schema.sql` (tables `items`, `source_folders`, `media` + views `public_items`/`public_media`). Endesha mode `sync-d1` (inahitaji `CF_ACCOUNT_ID`, `D1_DATABASE_ID` kama variables na `CF_API_TOKEN` kama secret). `items` ina title, dj, poster/image url, watch/source url, status na approved; `source_folders` ina Drive folder; `media` ina `hf_repo`, `hf_folder`, `hf_path`, ready. Excluded zinaingia bila links/poster. Worker ya viewers isome `public_*` views tu; Drive na HF paths ni server-side.

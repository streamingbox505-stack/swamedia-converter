-- Cloudflare D1 schema (private backend). Viewers must read ONLY from public_* views.
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, title TEXT, type TEXT, dj TEXT, dj_raw TEXT, age_rating TEXT,
  poster_url TEXT, image_url TEXT, watch_url TEXT, source_url TEXT, source_provider TEXT,
  status TEXT NOT NULL,            -- source_regular | needs_review | excluded
  reasons TEXT, approved INTEGER NOT NULL DEFAULT 0, source_version TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS source_folders (   -- Drive folder/file links (PRIVATE)
  item_id TEXT NOT NULL, drive_kind TEXT NOT NULL, drive_id TEXT NOT NULL, drive_url TEXT,
  PRIMARY KEY (item_id, drive_kind, drive_id)
);
CREATE TABLE IF NOT EXISTS media (            -- converted MP4s in the private HF dataset
  media_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, drive_file_id TEXT, original_name TEXT,
  season INTEGER, episode INTEGER, part INTEGER,
  hf_repo TEXT, hf_folder TEXT, hf_path TEXT, status TEXT, ready INTEGER NOT NULL DEFAULT 0,
  duration REAL, size INTEGER, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_item ON media(item_id);
CREATE VIEW IF NOT EXISTS public_items AS
  SELECT id, title, type, dj, poster_url, image_url, age_rating FROM items WHERE status='source_regular' AND approved=1;
CREATE VIEW IF NOT EXISTS public_media AS
  SELECT m.media_id, m.item_id, m.season, m.episode, m.part, m.duration
  FROM media m JOIN items i ON i.id=m.item_id WHERE m.ready=1 AND i.status='source_regular' AND i.approved=1;
